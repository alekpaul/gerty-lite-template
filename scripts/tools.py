"""
GERTY Lite — tool implementations for the agentic loop.

Tools:
  web_search(query)              — DuckDuckGo search, returns top results
  web_fetch(url)                 — fetch URL, return cleaned text
  browser_open(url)              — open URL in stealth browser (camofox), return page snapshot
  read_file(path)                — read one file (vault or any Obsidian sub-vault)
  list_folder(path)              — list files + folders, non-recursive, read-only
  write_file(path, content)      — write or overwrite one file (no bulk write)
  run_shell(command)             — execute a shell command, return output
  send_reaction(emoji)           — react to the user's current message
  send_message(text)             — send a fresh Telegram message (use in routines)
  speak(text)                    — send a voice message via Kokoro TTS
  read_aloud(input)              — fire-and-forget: read URL/file/text aloud (no content returned)
  take_screenshot(url)           — screenshot a URL via camofox, return local path
  send_image(path, caption)      — send an image to the user on Telegram
  read_pdf(path)                 — extract text from a PDF inside FILES_ROOT
  send_file(path, caption)       — send a file from FILES_ROOT via sendDocument
  move_file(src, dst)            — reorganize files inside FILES_ROOT
  delete_file(path)              — delete one file inside FILES_ROOT
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_paths_config() -> dict:
    """Load path overrides from config/.paths (gitignored) with .paths.template
    as the fallback. Returns absolute Path objects keyed by VAULT_ROOT,
    NOTES_ROOT, MEMORY_ROOT. Relative entries resolve against the repo root,
    so the same config works on Windows, Mac, and Linux."""
    paths: dict[str, str] = {}
    for f in (_REPO_ROOT / "config" / ".paths.template",
              _REPO_ROOT / "config" / ".paths"):
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                paths[k.strip()] = v.strip()
        except Exception as e:
            print(f"[paths] failed to read {f}: {e}", file=sys.stderr)

    def _resolve(val: str) -> Path:
        p = Path(val)
        return p if p.is_absolute() else (_REPO_ROOT / p).resolve()

    return {
        "VAULT_ROOT":  _resolve(paths.get("VAULT_ROOT",  "data/vault")),
        "NOTES_ROOT":  _resolve(paths.get("NOTES_ROOT",  "data/notes")),
        "MEMORY_ROOT": _resolve(paths.get("MEMORY_ROOT", "data/memory")),
        "FILES_ROOT":  _resolve(paths.get("FILES_ROOT",  "data/files")),
    }


_PATHS = _load_paths_config()
VAULT_ROOT = _PATHS["VAULT_ROOT"]
# OBSIDIAN_ROOT keeps its name for back-compat but is now generic "notes root" —
# can point at an Obsidian vault, a plain notes folder, or anything else.
OBSIDIAN_ROOT = _PATHS["NOTES_ROOT"]
MEMORY_ROOT = _PATHS["MEMORY_ROOT"]
# FILES_ROOT is the guardrailed sandbox for user files (PDFs, docs, Telegram
# attachments). All file_* tools refuse paths that resolve outside this root.
FILES_ROOT = _PATHS["FILES_ROOT"]
FILES_ROOT.mkdir(parents=True, exist_ok=True)
OBSIDIAN_SUB_VAULTS = ("Organized-notes", "Personal", "Work", "Survive", "MealPlan")
STICKER_LIB = _REPO_ROOT / "config" / "stickers.json"
ROUTINES_CONFIG = _REPO_ROOT / "config" / "routines.json"
SEARCH_TIMEOUT = 15
FETCH_TIMEOUT  = 20
SHELL_TIMEOUT  = 30
# Tool-result truncation caps. The old 3000-char cap chopped the JW daily-text
# routine mid-sentence (page chrome + privacy banner ate most of the budget
# before reaching the actual article). 12 KB ≈ 3–4 K tokens — fits comfortably
# alongside system prompt + history within LM Studio's 8 K loaded context.
# Browser snapshots include the accessibility tree, so they need the extra room.
MAX_RESULT_CHARS = 12000    # untrusted: web / shell / browser output
MAX_FILE_CHARS   = 20000    # trusted: vault + obsidian reads (indexes can be long)
MAX_LIST_ENTRIES = 200

# Optional external scripts — env var wins, else auto-discover common
# locations, else empty (in which case speak() degrades to OmniVoice and
# take_screenshot uses camofox directly).
def _normalize_path(p: str) -> str:
    """Accept POSIX-style `/d/foo/bar` paths on Windows by mapping them to
    `D:/foo/bar`. No-op on macOS/Linux."""
    if os.name == "nt" and re.match(r"^/[a-zA-Z]/", p):
        return p[1].upper() + ":" + p[2:]
    return p

def _discover_optional_script(env_var: str, candidates: list[str]) -> str:
    val = os.environ.get(env_var, "").strip()
    if val:
        v = _normalize_path(val)
        if os.path.exists(v):
            return v
    for c in candidates:
        cn = _normalize_path(c)
        if os.path.exists(cn):
            return cn
    return ""

TTS_SCRIPT = _discover_optional_script(
    "GERTY_KOKORO_TTS",
    [
        # Common locations carried over from the original install layout
        "/d/Claude/scripts/tts.py",
        os.path.expanduser("~/Claude/scripts/tts.py"),
        os.path.expanduser("~/gerty-tts/tts.py"),
        str(_REPO_ROOT / "scripts" / "kokoro-tts.py"),
    ],
)
SCREENSHOT_SCRIPT = _discover_optional_script(
    "GERTY_SCREENSHOT_JS",
    [
        "/d/Claude/scripts/fetch-url.js",
        os.path.expanduser("~/Claude/scripts/fetch-url.js"),
    ],
)
CAMOFOX_URL = "http://localhost:9377"
CAMOFOX_USER = "gerty-lite"
VOICE_CONFIG = _REPO_ROOT / ".voice-config.json"


def _resolve_bash() -> str:
    """Find a usable bash on any OS: explicit override, Git Bash on Windows,
    or whatever's first in PATH."""
    return (
        os.environ.get("GERTY_BASH")
        or shutil.which("bash")
        or (r"C:\Program Files\Git\usr\bin\bash.exe"
            if os.path.exists(r"C:\Program Files\Git\usr\bin\bash.exe")
            else "bash")
    )


BASH_EXE = _resolve_bash()
DEFAULT_VOICE = "am_echo"

# Catalog of available voices. Engine is the TTS backend (currently only kokoro;
# fish_speech / cosyvoice slots reserved for future engines).
KOKORO_VOICES = {
    # American English (female / male)
    "af_alloy":   ("Kokoro · American Female · Alloy",   "kokoro"),
    "af_aoede":   ("Kokoro · American Female · Aoede",   "kokoro"),
    "af_bella":   ("Kokoro · American Female · Bella",   "kokoro"),
    "af_heart":   ("Kokoro · American Female · Heart",   "kokoro"),
    "af_jessica": ("Kokoro · American Female · Jessica", "kokoro"),
    "af_kore":    ("Kokoro · American Female · Kore",    "kokoro"),
    "af_nicole":  ("Kokoro · American Female · Nicole",  "kokoro"),
    "af_nova":    ("Kokoro · American Female · Nova",    "kokoro"),
    "af_river":   ("Kokoro · American Female · River",   "kokoro"),
    "af_sarah":   ("Kokoro · American Female · Sarah",   "kokoro"),
    "af_sky":     ("Kokoro · American Female · Sky",     "kokoro"),
    "am_adam":    ("Kokoro · American Male · Adam",      "kokoro"),
    "am_echo":    ("Kokoro · American Male · Echo",      "kokoro"),
    "am_eric":    ("Kokoro · American Male · Eric",      "kokoro"),
    "am_fenrir":  ("Kokoro · American Male · Fenrir",    "kokoro"),
    "am_liam":    ("Kokoro · American Male · Liam",      "kokoro"),
    "am_michael": ("Kokoro · American Male · Michael",   "kokoro"),
    "am_onyx":    ("Kokoro · American Male · Onyx",      "kokoro"),
    "am_puck":    ("Kokoro · American Male · Puck",      "kokoro"),
    # British English
    "bf_alice":     ("Kokoro · British Female · Alice",    "kokoro"),
    "bf_emma":      ("Kokoro · British Female · Emma",     "kokoro"),
    "bf_isabella":  ("Kokoro · British Female · Isabella", "kokoro"),
    "bf_lily":      ("Kokoro · British Female · Lily",     "kokoro"),
    "bm_daniel":    ("Kokoro · British Male · Daniel",     "kokoro"),
    "bm_fable":     ("Kokoro · British Male · Fable",      "kokoro"),
    "bm_george":    ("Kokoro · British Male · George",     "kokoro"),
    "bm_lewis":     ("Kokoro · British Male · Lewis",      "kokoro"),
    # Other languages (less common, included for reference)
    "ef_dora":      ("Kokoro · Spanish Female · Dora",     "kokoro"),
    "em_alex":      ("Kokoro · Spanish Male · Alex",       "kokoro"),
    "ff_siwis":     ("Kokoro · French Female · Siwis",     "kokoro"),
    "if_sara":      ("Kokoro · Italian Female · Sara",     "kokoro"),
    "im_nicola":    ("Kokoro · Italian Male · Nicola",     "kokoro"),
    "jf_alpha":     ("Kokoro · Japanese Female · Alpha",   "kokoro"),
    "jm_kumo":      ("Kokoro · Japanese Male · Kumo",      "kokoro"),
    "pf_dora":      ("Kokoro · Portuguese Female · Dora",  "kokoro"),
    "pm_alex":      ("Kokoro · Portuguese Male · Alex",    "kokoro"),
    "zf_xiaoxiao":  ("Kokoro · Mandarin Female · Xiaoxiao","kokoro"),
    "zm_yunxi":     ("Kokoro · Mandarin Male · Yunxi",     "kokoro"),
}
# OmniVoice voices — discovered dynamically from .tts-refs/<name>.wav files.
# Each pair (name.wav + name.txt) becomes a cloneable voice. For Ukrainian
# output, drop a <name>-uk.wav + <name>-uk.txt alongside (server auto-picks
# based on Cyrillic detection). To register a new voice, save the files +
# POST /v1/refresh to the omnivoice server.
TTS_REFS_DIR = _REPO_ROOT / ".tts-refs"
OMNIVOICE_URL = "http://127.0.0.1:8883"


# OmniVoice clone catalog. Each entry: (display_label, gender). Gender drives
# Ukrainian grammar in the model's prompt — past-tense verbs and predicate
# adjectives that refer to the assistant should match the voice gender the
# user actually hears. Add new clones here when you /clone them; unknown
# gender means we just don't include the directive (model defaults apply).
_OMNI_LABELS: dict[str, tuple[str, str]] = {
    "jo":       ("Jo (female, energetic)",        "feminine"),
    "dave":     ("Dave (male, warm)",             "masculine"),
    "greta":    ("Greta (female)",                "feminine"),
    "juliette": ("Juliette (female, French-ish)", "feminine"),
    "mateo":    ("Mateo (male)",                  "masculine"),
    "princess": ("Princess (female)",             "feminine"),
}


def _discover_omni_voices() -> dict[str, tuple[str, str]]:
    """Scan .tts-refs/ for base voice names (ignoring -uk / -en suffix variants).
    Each base becomes one omni_<name> entry in the voice catalog."""
    out: dict[str, tuple[str, str]] = {}
    if not TTS_REFS_DIR.exists():
        return out
    bases: set[str] = set()
    for wav_path in TTS_REFS_DIR.glob("*.wav"):
        stem = wav_path.stem
        txt = wav_path.with_suffix(".txt")
        if not txt.exists():
            continue
        if stem.endswith("-uk"):
            bases.add(stem[:-3])
        elif stem.endswith("-en"):
            bases.add(stem[:-3])
        else:
            bases.add(stem)
    for name in sorted(bases):
        label, _gender = _OMNI_LABELS.get(name, (f"{name.capitalize()} (custom)", "unknown"))
        out[f"omni_{name}"] = (f"OmniVoice · {label}", "omnivoice")
    return out


# User-editable gender overrides. Written by the /clone flow when the user
# picks a gender, and by `set_voice_gender()` if exposed via another path.
# Survives restarts. Takes precedence over the hardcoded _OMNI_LABELS and
# Kokoro's id-based detection — so the user can override anything.
VOICE_GENDERS_FILE = _REPO_ROOT / ".voice-genders.json"


def _load_gender_overrides() -> dict[str, str]:
    if not VOICE_GENDERS_FILE.exists():
        return {}
    try:
        data = json.loads(VOICE_GENDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v in ("feminine", "masculine")}
    except Exception:
        pass
    return {}


def set_voice_gender(voice_id: str, gender: str) -> str:
    """Persist a gender override for one voice. Returns a human-friendly
    status string. Validates that `gender` is binary feminine/masculine —
    the model only knows those two for Ukrainian grammar purposes."""
    if gender not in ("feminine", "masculine"):
        return f"voice gender error: must be 'feminine' or 'masculine', got {gender!r}"
    if not voice_id:
        return "voice gender error: missing voice id"
    overrides = _load_gender_overrides()
    overrides[voice_id] = gender
    try:
        VOICE_GENDERS_FILE.write_text(
            json.dumps(overrides, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:
        return f"voice gender error: failed to save ({e})"
    return f"voice gender saved: {voice_id} → {gender}"


def _voice_gender(voice_id: str) -> str:
    """Return 'feminine', 'masculine', or 'unknown' for a voice id.

    Resolution order:
    1. User override in .voice-genders.json (set during /clone or explicitly).
    2. OmniVoice hardcoded defaults from _OMNI_LABELS.
    3. Kokoro id convention — 2nd char is f|m (e.g. af_bella → feminine).
    4. 'unknown' — no Ukrainian gender directive will be injected."""
    if not voice_id:
        return "unknown"
    overrides = _load_gender_overrides()
    if voice_id in overrides:
        return overrides[voice_id]
    if voice_id.startswith("omni_"):
        name = voice_id[5:]
        if name in _OMNI_LABELS:
            return _OMNI_LABELS[name][1]
        return "unknown"
    # Kokoro convention. First letter is locale, second is f|m.
    if len(voice_id) >= 2 and voice_id[1] == "f":
        return "feminine"
    if len(voice_id) >= 2 and voice_id[1] == "m":
        return "masculine"
    return "unknown"


def current_voice_info() -> dict:
    """Return {voice, engine, label, gender} for the active voice. Used by
    gemma_chat.py to inject a 'current speaker identity' block into the
    system prompt every turn."""
    cur = get_voice_config()
    voice = cur.get("voice") or DEFAULT_VOICE
    engine = cur.get("engine") or ALL_VOICES.get(voice, ("", "kokoro"))[1]
    label = ALL_VOICES.get(voice, (voice, engine))[0]
    return {
        "voice":  voice,
        "engine": engine,
        "label":  label,
        "gender": _voice_gender(voice),
    }


OMNIVOICE_VOICES = _discover_omni_voices()

ALL_VOICES = {**KOKORO_VOICES, **OMNIVOICE_VOICES}

# Runtime context — set by gemma_chat.py before each run_agent call.
# Safe: gemma_chat.py is a single-threaded process per message.
_CTX: dict = {}


def set_context(chat_id: int, message_id: int, tg_token: str) -> None:
    global _CTX
    _CTX = {"chat_id": chat_id, "message_id": message_id, "tg_token": tg_token}


# ── Tool schemas (OpenAI function-calling format) ─────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information, news, facts, or anything "
                "you don't know from training. Returns titles, snippets, and URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the full text content of a URL. Use after web_search to read a page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": (
                "Open a URL in a stealth headless browser (camofox) and return the page's "
                "accessibility snapshot — works for JS-heavy sites, sites that block scrapers, "
                "or pages where plain web_fetch returns nothing useful. Use this when web_fetch "
                "fails or you need to see what's actually rendered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one file. Path forms:\n"
                "  'inbox/note.md'                              → vault (VAULT_ROOT from .paths)\n"
                "  'obsidian/<sub-vault>/...'                   → notes vault (NOTES_ROOT from .paths)\n"
                "  'files/<path>'                                → user files sandbox (FILES_ROOT from .paths)\n"
                "Read indexes first to discover specific notes instead of guessing paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path. See description for the path forms supported.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Character offset to start reading from. Default 0. "
                            "If a previous read returned '[truncated — ... offset=N]', "
                            "call again with offset=N to get the next chunk."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folder",
            "description": (
                "List files and immediate subfolders of a vault/Obsidian folder (non-recursive, read-only). "
                "Use this to discover what's available before deciding what to read. "
                "Path examples: '' (vault root), 'inbox', 'obsidian' (shows the 5 sub-vaults), "
                "'obsidian/Organized-notes', 'obsidian/Personal/Spiritual'. "
                "After listing, follow up with read_file on a specific file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Folder path. Empty string = vault root. 'obsidian' = the Obsidian root (5 sub-vaults).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or overwrite ONE file. Creates parent directories as needed. "
                "Paths: 'inbox/note.md' for vault, 'obsidian/<sub-vault>/...' for Obsidian. "
                "One file at a time — there is no bulk write."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the vault root (e.g. 'inbox/note.md'), or 'obsidian/<sub-vault>/...' for the notes vault.",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": (
                "Extract text from a PDF inside the user files sandbox (FILES_ROOT). "
                "Use this when the user sends a PDF or asks to read one — read_file "
                "won't work on PDFs (they're binary). Path is sandbox-relative, "
                "e.g. 'inbox/2026-05-18_153022_resume.pdf' or 'files/cv/cv.pdf'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside FILES_ROOT (with or without 'files/' prefix)."},
                    "max_pages": {"type": "integer", "description": "Cap pages extracted (default 50)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "Send a file from the user files sandbox (FILES_ROOT) to the user on "
                "Telegram as a document attachment. Use when the user asks 'send me my "
                "CV', 'надішли мені резюме', or any 'send me <doc>' request. Path is "
                "sandbox-relative. Optional caption goes under the file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside FILES_ROOT."},
                    "caption": {"type": "string", "description": "Optional caption."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Move or rename a file inside the user files sandbox. Use to organize "
                "incoming Telegram attachments into a sensible folder structure — e.g. "
                "move_file('inbox/2026-05-18_resume.pdf', 'documents/cv/resume.pdf'). "
                "Both src and dst are sandbox-relative; cannot escape FILES_ROOT."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source path inside FILES_ROOT."},
                    "dst": {"type": "string", "description": "Destination path inside FILES_ROOT."},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete one file inside the user files sandbox. Use ONLY when the user "
                "explicitly asks to delete/remove a stored file. Refuses to delete "
                "directories or anything outside FILES_ROOT. Confirm with the user first "
                "if you're not sure which file they mean."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside FILES_ROOT."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a long-term memory the bot can recall in future conversations. "
                "Use whenever the user says 'remember this', 'запам'ятай', 'don't forget', "
                "or asks the bot to keep track of a fact, preference, name, or routine. "
                "Memory persists across restarts and lives in the Obsidian vault, grouped "
                "by subject folder. Pick a kebab-case name (e.g. 'favorite-coffee') and a "
                "subject category (e.g. 'health', 'preferences', 'people', 'projects'). "
                "Pass `related` with existing memory names to cross-link — the link is "
                "auto-added in both directions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short kebab-case slug identifying this memory (letters/numbers/hyphens only).",
                    },
                    "content": {
                        "type": "string",
                        "description": "The actual thing to remember — plain text or Markdown.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Category folder: 'health', 'preferences', 'people', 'projects', 'work', 'spiritual', 'general', etc. Lowercase kebab-case.",
                    },
                    "related": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of existing memory names to cross-link bidirectionally.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional one-line summary for the index. If empty, the first line of content is used.",
                    },
                },
                "required": ["name", "content", "subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Read back one saved memory by its exact name. Returns the full entry. "
                "Use list_memory first if you don't know the name, or search_memory to "
                "find by keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The kebab-case slug used at save time."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search across saved memories for a keyword (case-insensitive). "
                "Returns matching entry names with their subject folder and short snippets. "
                "Use when the user asks 'what do you remember about X' or 'do you know my Y'. "
                "Pass `subject` to narrow search to one category."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for."},
                    "subject": {"type": "string", "description": "Optional subject folder to limit search to."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memory",
            "description": (
                "Show the memory Map of Content — entries grouped by subject. "
                "Cheap call. Use this to see what's stored before deciding whether to recall_memory "
                "or search_memory. Pass `subject` to show only that one section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Optional subject folder to filter the listing."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": (
                "Delete one saved memory entry by exact name. Use ONLY when the user has "
                "explicitly asked to forget something (e.g. 'forget my old address', 'забудь про X', "
                "'delete that memory'). Never delete on your own initiative. If unsure which entry "
                "they mean, call list_memory or search_memory first and confirm. Operates only "
                "inside the memory vault — cannot touch the user's regular notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The kebab-case slug of the memory to delete."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_chat",
            "description": (
                "Return the last N user/assistant turns from the main chat history. "
                "Primarily for the dream routine: scan recent conversation, decide what's "
                "worth saving as a memory. Format: one line per turn, prefixed with [u] or [a]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many recent turns to return (default 50)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command and return stdout+stderr. "
                "Use to check system state, run scripts, list files, check processes, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to execute",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_reaction",
            "description": (
                "Send a reaction emoji to the user's message. "
                "Use when the vibe clearly calls for it: excitement → 🔥, "
                "agreement → 👍 or 🤝, question → 🤔, win → 🎉, coding → 👨‍💻. "
                "Don't react to every message — only when it genuinely fits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": (
                            "Reaction emoji. Must be a supported Telegram reaction: "
                            "👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 🎉 🤩 💩 🙏 👌 🤡 😢 🥱 "
                            "😍 🐳 💯 🤣 ⚡ 🏆 💔 😐 🍾 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 "
                            "😇 🤝 ✍ 🤗 🫡 🎅 🎄 💅 🤪 🗿 🆒 💘 🦄 😘 💊 😎 👾 🤷 😡"
                        ),
                    }
                },
                "required": ["emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a fresh Telegram message to the user (not a reply to an existing message). "
                "Use this in routines or when you want to proactively ping the user. "
                "In a normal conversation reply, just return your text — don't use this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_routine",
            "description": (
                "Create a scheduled routine OR a one-shot reminder.\n"
                "TWO ways to time it — use exactly one:\n"
                "  (a) delay_seconds=N — for 'in N minutes / hours / days' relative reminders. "
                "Tool computes the absolute time itself. ALWAYS use this for relative timing. "
                "Examples: 'in 4 minutes' → delay_seconds=240, 'in 2 hours' → delay_seconds=7200, "
                "'in 30 minutes' → delay_seconds=1800.\n"
                "  (b) schedule=<cron> — for specific clock times or recurring schedules. "
                "5-field cron in local time: '0 8 * * *' (daily 8am), '0 17 * * 2' (Tue 5pm), "
                "'*/30 * * * *' (every 30 min). NEVER use cron for 'in N minutes' — use delay_seconds.\n"
                "The prompt is what the model will be told to do when the routine fires. "
                "ALWAYS include 'call send_message(...)' in the prompt so the result reaches Telegram. "
                "Inside the prompt, {today} is replaced with the fire-day's ISO date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Short stable identifier. Auto-generated from prompt if omitted.",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "description": "Fire N seconds from now (one-shot). USE THIS for 'in N minutes/hours/days'. Examples: 240 = 4 minutes, 3600 = 1 hour, 86400 = 1 day. Omit (or 0) when using schedule.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "5-field cron expression. Examples: '0 8 * * *' (daily 8am), '0 17 * * 2' (Tue 5pm), '*/30 9-18 * * 1-5' (every 30min, 9-18 weekdays). Omit when using delay_seconds.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Instructions for the future agent. ALWAYS include 'call send_message(...) with the result' so the user gets pinged. Example: 'Call send_message to remind the user to drink water.'",
                    },
                    "one_shot": {
                        "type": "boolean",
                        "description": "Auto-disable after firing once. Auto-true when delay_seconds is used. Default false for cron-based routines.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_routines",
            "description": "List all routines and reminders, showing id / schedule / enabled state / type / short prompt preview.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_routine",
            "description": "Delete (or disable) a routine by its id. Use list_routines first to find the id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The routine id to remove"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_aloud",
            "description": (
                "Read content aloud to the user as a Telegram voice message (TTS via OmniVoice). "
                "Use ONLY when the user explicitly asks to hear / listen to / read aloud / voice / "
                "озвучити / прочитати something. "
                "Input is one of: a URL (article body is fetched + cleaned), a vault file path "
                "(e.g. 'obsidian/Organized-notes/CLAUDE.md', 'inbox/note.md', 'drafts/foo.md'), "
                "or raw text to speak as-is. "
                "IMPORTANT — this tool does NOT return the article content to you, only a short status. "
                "Do NOT call it as a way to learn what's in a URL or file. "
                "If you want to read the content yourself, use web_fetch or read_file instead. "
                "This runs in the background — the voice message arrives in chat shortly after; "
                "don't wait for it, don't repeat the content in your text reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "URL, vault file path, or raw text to speak.",
                    }
                },
                "required": ["input"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": (
                "Send a voice message to the user using TTS. "
                "Use when asked to reply in voice, or when audio feels more natural than text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to speak aloud",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a screenshot of a URL and return the local file path. Follow with send_image to show it to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_image",
            "description": "Send an image file to the user on Telegram.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the image file",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption to show under the image",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_sticker",
            "description": (
                "Send a sticker to the user. Pick a mood that fits the vibe of the conversation. "
                "Available moods: laugh, love, agree, hello, happy, thinking, sad, surprised, "
                "cool, celebrate, clap, bored, wink, strong, angry, question, smug, dance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "description": (
                            "Mood/vibe for the sticker. One of: "
                            "laugh, love, agree, hello, happy, thinking, sad, surprised, "
                            "cool, celebrate, clap, bored, wink, strong, angry, question, smug, dance"
                        ),
                    },
                },
                "required": ["mood"],
            },
        },
    },
]


# ── MCP tools (lazy-merged) ──────────────────────────────────────────────────
# Optional: if config/mcp.json has any enabled servers AND their schemas are
# cached on disk (scripts/mcp_client.py refresh), those tools get appended
# to TOOL_SCHEMAS here. Failure to import mcp_client is a no-op — the bot
# runs identically without MCP.
try:
    import mcp_client as _mcp_client
    _mcp_extra = _mcp_client.cached_tool_schemas()
    if _mcp_extra:
        TOOL_SCHEMAS.extend(_mcp_extra)
        print(f"[mcp] merged {len(_mcp_extra)} MCP tool(s) into TOOL_SCHEMAS", file=sys.stderr)
except Exception as _mcp_e:
    _mcp_client = None
    print(f"[mcp] disabled ({_mcp_e})", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _posix_to_win(path: str) -> str:
    """Convert /d/foo or /c/foo → D:/foo for Windows file ops."""
    if re.match(r'^/[a-zA-Z]/', path):
        return path[1].upper() + ":" + path[2:]
    return path


def _tg_post_json(endpoint: str, payload: dict) -> bool:
    """POST JSON to Telegram API. Returns True on success."""
    token = _CTX.get("tg_token", "")
    if not token:
        print(f"[tg_post] {endpoint}: no token in context", file=sys.stderr)
        return False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[tg_post] {endpoint} HTTP {e.code}: {body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[tg_post] {endpoint} error: {e}", file=sys.stderr)
        return False


def _tg_send_file(endpoint: str, field: str, path: str,
                  mime: str, extra_fields: dict | None = None) -> bool:
    """Send a file to Telegram via multipart/form-data. Returns True on success."""
    token = _CTX.get("tg_token", "")
    chat_id = _CTX.get("chat_id")
    if not token or not chat_id:
        return False
    try:
        with open(path, "rb") as f:
            file_data = f.read()
    except Exception as e:
        print(f"[tg_send_file] read error: {e}", file=sys.stderr)
        return False

    boundary = "----GertyBoundary"
    parts: list[bytes] = []

    def field_part(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    parts.append(field_part("chat_id", str(chat_id)))
    for k, v in (extra_fields or {}).items():
        parts.append(field_part(k, v))

    fname = os.path.basename(path)
    parts.append((
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8"))
    parts.append(file_data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{endpoint}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib.request.urlopen(req, timeout=60)
        return True
    except Exception as e:
        print(f"[tg_send_file] {endpoint} error: {e}", file=sys.stderr)
        return False


# ── Tool implementations ──────────────────────────────────────────────────────

def web_search(query: str) -> str:
    """DuckDuckGo HTML search — no API key required."""
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")

        def strip_tags(s: str) -> str:
            return re.sub(r"<[^>]+>", "", s).strip()

        titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        raw_urls = re.findall(r'class="result__url"[^>]*>(.*?)</span>', html, re.DOTALL)

        lines = []
        for i, (t, s) in enumerate(zip(titles[:6], snippets[:6])):
            u = strip_tags(raw_urls[i]).strip() if i < len(raw_urls) else ""
            lines.append(f"{strip_tags(t)}\n{strip_tags(s)}\n{u}")

        result = "\n\n".join(lines) if lines else "no results found"
        return result[:MAX_RESULT_CHARS]

    except Exception as e:
        return f"search error: {e}"


def web_fetch(url: str) -> str:
    """Fetch URL and return stripped plain text."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")

        html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:MAX_RESULT_CHARS]

    except Exception as e:
        return f"fetch error: {e}"


def browser_open(url: str) -> str:
    """Open URL in camofox stealth browser, return page snapshot. Stateless: opens, snapshots, closes."""
    tab_id = None
    try:
        # Create tab + navigate
        payload = json.dumps({
            "userId": CAMOFOX_USER,
            "sessionKey": "default",
            "url": url,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode("utf-8"))
        tab_id = data.get("tabId")
        if not tab_id:
            return f"browser_open: no tabId in response: {data}"

        # Snapshot
        snap_url = f"{CAMOFOX_URL}/tabs/{tab_id}/snapshot?userId={CAMOFOX_USER}"
        with urllib.request.urlopen(snap_url, timeout=30) as r:
            snap = json.loads(r.read().decode("utf-8"))

        page_url = snap.get("url", url)
        snapshot = snap.get("snapshot", "")
        return f"URL: {page_url}\n\n{snapshot}"[:MAX_RESULT_CHARS]

    except urllib.error.URLError as e:
        return (
            f"browser_open: camofox unreachable at {CAMOFOX_URL} ({e}). "
            "Check: pm2 list, pm2 logs camofox"
        )
    except Exception as e:
        return f"browser_open error: {e}"
    finally:
        if tab_id:
            try:
                req = urllib.request.Request(
                    f"{CAMOFOX_URL}/tabs/{tab_id}?userId={CAMOFOX_USER}",
                    method="DELETE",
                )
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                pass


def _resolve_vault_path(path: str) -> Path:
    """Resolve a relative path against the vault, Obsidian root, or files sandbox.
    'obsidian/<sub-vault>/...' resolves under that sub-vault. If the first
    segment after 'obsidian/' isn't a known sub-vault, assume Organized-notes
    (the only sub-vault in active use). This catches bot-side path mistakes
    like 'obsidian/Progress/2026-05-11.md' → 'obsidian/Organized-notes/...'.
    Bare 'obsidian' resolves to the Obsidian root (useful for list_folder).
    'files' / 'files/...' resolves to FILES_ROOT (the user-files sandbox)."""
    if path == "obsidian":
        return OBSIDIAN_ROOT
    if path.startswith("obsidian/"):
        rest = path[len("obsidian/"):]
        first = rest.split("/", 1)[0]
        if first in OBSIDIAN_SUB_VAULTS:
            return OBSIDIAN_ROOT / rest
        return OBSIDIAN_ROOT / "Organized-notes" / rest
    if path == "files":
        return FILES_ROOT
    if path.startswith("files/"):
        return _resolve_files_path(path[len("files/"):])
    return VAULT_ROOT / path


def _resolve_files_path(rel: str) -> Path:
    """Resolve a path inside the FILES_ROOT sandbox. Raises ValueError if the
    resolved path would escape the sandbox (e.g. '../etc/passwd' or an absolute
    path). Use this for every read/write/move/delete in the files sandbox."""
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        return FILES_ROOT
    candidate = (FILES_ROOT / rel).resolve()
    root = FILES_ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes files sandbox: {rel}")
    return candidate


def _sanitize_filename(name: str) -> str:
    """Make a Telegram-supplied filename safe for the local FS. Strips path
    separators and characters Windows forbids; preserves extension."""
    name = (name or "").strip()
    # Drop any directory components a user-controlled filename might carry.
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(" .") or "file"
    return name[:120]


def read_file(path: str, offset: int = 0) -> str:
    full = _resolve_vault_path(path)
    try:
        content = full.read_text(encoding="utf-8")
        total = len(content)
        if offset < 0:
            offset = 0
        if offset >= total and total > 0:
            return f"[end of file — file is {total} chars, offset {offset} is past end]"
        chunk = content[offset:offset + MAX_FILE_CHARS]
        end = offset + len(chunk)
        if end < total:
            remaining = total - end
            return (
                chunk +
                f"\n\n[truncated — file is {total} chars, showing {offset}-{end}. "
                f"{remaining} chars remain. To continue, call read_file with "
                f"path='{path}', offset={end}.]"
            )
        if offset > 0:
            return chunk + f"\n\n[end of file — final chunk, {offset}-{end} of {total} chars]"
        return chunk
    except FileNotFoundError:
        return f"file not found: {path}"
    except UnicodeDecodeError:
        return f"read error: {path} is not a text file (likely binary — image, pdf, audio)"
    except Exception as e:
        return f"read error: {e}"


def write_file(path: str, content: str) -> str:
    full = _resolve_vault_path(path)
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"written {len(content)} chars to {path}"
    except Exception as e:
        return f"write error: {e}"


def list_folder(path: str = "") -> str:
    """Read-only listing of files + immediate subfolders. Non-recursive.

    Path examples:
      ''                       → vault root (VAULT_ROOT)
      'inbox'                  → <vault>/inbox/
      'obsidian'               → notes vault root (NOTES_ROOT)
      'obsidian/<sub-vault>'   → subfolder of NOTES_ROOT
      'files'                  → user files sandbox (FILES_ROOT)
    """
    full = _resolve_vault_path(path) if path else VAULT_ROOT
    if not full.exists():
        return f"folder not found: {path or '(vault root)'}"
    if not full.is_dir():
        return f"not a folder: {path}"
    try:
        entries = sorted(full.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as e:
        return f"list error: {e}"
    folders, files = [], []
    for p in entries:
        name = p.name
        if name.startswith("."):
            continue
        if p.is_dir():
            folders.append(f"{name}/")
        else:
            files.append(name)
    total = len(folders) + len(files)
    truncated = total > MAX_LIST_ENTRIES
    lines = [f"folder: {path or '(vault root)'} — {len(folders)} folders, {len(files)} files"]
    if folders:
        lines.append("\nfolders:")
        lines.extend(f"  {f}" for f in folders[:MAX_LIST_ENTRIES])
    if files:
        remaining = max(0, MAX_LIST_ENTRIES - len(folders))
        lines.append("\nfiles:")
        lines.extend(f"  {f}" for f in files[:remaining])
    if truncated:
        lines.append(f"\n[truncated — {total} total entries, showing first {MAX_LIST_ENTRIES}]")
    return "\n".join(lines)


# ─── User files sandbox (FILES_ROOT) ─────────────────────────────────────────
# Everything under data/files/ (or whatever FILES_ROOT points to in .paths).
# Tools below refuse to touch anything outside that directory — '..' segments
# and absolute paths raise ValueError in _resolve_files_path. Use the 'files/'
# prefix with the generic read_file / write_file / list_folder tools for text
# files; binary helpers below cover PDFs and Telegram sendDocument.

PDF_MAX_PAGES = 50  # cap how much PDF text we extract per call


def save_incoming_file(src_path: str, original_name: str,
                       subfolder: str = "inbox") -> str:
    """Move a downloaded Telegram file into FILES_ROOT/<subfolder>/.
    Returns the relative path (e.g. 'inbox/2026-05-18_153022_resume.pdf')
    or an error string starting with 'error:'. Called by the listener — not
    exposed as an LLM tool."""
    try:
        target_dir = _resolve_files_path(subfolder)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_name = _sanitize_filename(original_name)
        dest = target_dir / f"{stamp}_{safe_name}"
        shutil.move(src_path, str(dest))
        return str(dest.relative_to(FILES_ROOT)).replace("\\", "/")
    except Exception as e:
        return f"error: {e}"


def read_pdf(path: str, max_pages: int = PDF_MAX_PAGES) -> str:
    """Extract text from a PDF inside FILES_ROOT. Requires pypdf (or PyPDF2)
    — if neither is installed, returns a helpful error. Caps pages to keep
    LLM context manageable; pass max_pages to override."""
    try:
        rel = path[len("files/"):] if path.startswith("files/") else path
        full = _resolve_files_path(rel)
    except ValueError as e:
        return f"read_pdf error: {e}"
    if not full.exists():
        return f"file not found: {path}"
    if not full.is_file():
        return f"not a file: {path}"

    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore
    except ImportError:
        return ("read_pdf error: no PDF library installed. "
                "Run: python -m pip install pypdf")

    try:
        reader = PdfReader(str(full))
        n_pages = len(reader.pages)
        out = [f"[pdf: {full.name} — {n_pages} pages]"]
        for i, page in enumerate(reader.pages[:max_pages]):
            try:
                txt = page.extract_text() or ""
            except Exception as e:
                txt = f"[page {i+1}: extract error: {e}]"
            out.append(f"\n--- page {i+1} ---\n{txt.strip()}")
        if n_pages > max_pages:
            out.append(f"\n[truncated — showing {max_pages} of {n_pages} pages]")
        result = "\n".join(out)
        return result[:MAX_FILE_CHARS]
    except Exception as e:
        return f"read_pdf error: {e}"


def send_file(path: str, caption: str = "") -> str:
    """Send a file from FILES_ROOT to the user via Telegram sendDocument.
    Path is relative to the files sandbox (with or without the 'files/' prefix)."""
    try:
        rel = path[len("files/"):] if path.startswith("files/") else path
        full = _resolve_files_path(rel)
    except ValueError as e:
        return f"send_file error: {e}"
    if not full.exists() or not full.is_file():
        return f"file not found: {path}"
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return "no context for sending file"
    extra = {"caption": caption} if caption else {}
    # Telegram infers the mime from the filename; application/octet-stream is
    # a safe generic default for sendDocument.
    ok = _tg_send_file("sendDocument", "document", str(full),
                       "application/octet-stream", extra)
    return f"sent {full.name}" if ok else "failed to send file"


def move_file(src: str, dst: str) -> str:
    """Move/rename a file inside FILES_ROOT. Both paths are sandbox-relative
    (with or without 'files/' prefix). Creates parent directories as needed."""
    try:
        src_rel = src[len("files/"):] if src.startswith("files/") else src
        dst_rel = dst[len("files/"):] if dst.startswith("files/") else dst
        src_full = _resolve_files_path(src_rel)
        dst_full = _resolve_files_path(dst_rel)
    except ValueError as e:
        return f"move_file error: {e}"
    if not src_full.exists():
        return f"file not found: {src}"
    try:
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_full), str(dst_full))
        return f"moved {src} → {dst}"
    except Exception as e:
        return f"move_file error: {e}"


def delete_file(path: str) -> str:
    """Delete a single file inside FILES_ROOT. Refuses to delete directories
    or anything outside the sandbox."""
    try:
        rel = path[len("files/"):] if path.startswith("files/") else path
        full = _resolve_files_path(rel)
    except ValueError as e:
        return f"delete_file error: {e}"
    if not full.exists():
        return f"file not found: {path}"
    if full.is_dir():
        return f"refusing to delete directory: {path} (only single files supported)"
    if full.resolve() == FILES_ROOT.resolve():
        return "refusing to delete the files sandbox root"
    try:
        full.unlink()
        return f"deleted {path}"
    except Exception as e:
        return f"delete_file error: {e}"


# ─── Memory ────────────────────────────────────────────────────────────────────
# Persistent memory lives in an Obsidian vault (MEMORY_ROOT in .paths). Layout:
#   <MEMORY_ROOT>/
#     MOC.md                  Map of Content — auto-grouped by subject
#     <subject>/<name>.md     one entry per file
# Each entry has YAML frontmatter (name, subject, tags, related, saved_at) and
# ends in a ## Related section of Obsidian wikilinks. Saving with `related=[...]`
# auto-creates the reverse link in each related entry, so the graph is bidirectional
# and explorable in Obsidian's graph view.

def _sanitize_memory_name(name: str) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    return safe[:60]


def _find_memory_path(safe_name: str) -> Path | None:
    """Locate a memory file by name, searching every subject subfolder."""
    if not safe_name:
        return None
    direct = MEMORY_ROOT / f"{safe_name}.md"
    if direct.exists():
        return direct
    for f in MEMORY_ROOT.rglob(f"{safe_name}.md"):
        if f.name != "MOC.md":
            return f
    return None


def _rebuild_moc() -> None:
    """Regenerate MOC.md by scanning every entry file. Groups by subject."""
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    by_subject: dict[str, list[tuple[str, str]]] = {}
    for f in MEMORY_ROOT.rglob("*.md"):
        if f.name == "MOC.md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        subject = "general"
        desc = ""
        m_sub = re.search(r"^subject:\s*(.+)$", text, re.MULTILINE)
        if m_sub:
            subject = m_sub.group(1).strip() or "general"
        m_desc = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if m_desc:
            desc = m_desc.group(1).strip()
        by_subject.setdefault(subject, []).append((f.stem, desc))

    lines = [
        "# Gerty Memory — Map of Content\n",
        "Auto-maintained. Open in Obsidian to browse the graph. Edits by hand "
        "are fine — keep the frontmatter intact so the bot can still find entries.\n",
    ]
    for subject in sorted(by_subject):
        lines.append(f"\n## {subject}\n")
        for name, desc in sorted(by_subject[subject]):
            tail = f" — {desc}" if desc else ""
            lines.append(f"- [[{name}]]{tail}")
    (MEMORY_ROOT / "MOC.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _add_back_link(target_name: str, source_name: str) -> None:
    """Append [[source_name]] to the target's Related section so links are
    bidirectional. No-op if the target doesn't exist or already links back."""
    target = _find_memory_path(target_name)
    if not target:
        return
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return
    if f"[[{source_name}]]" in text:
        return
    if "## Related" in text:
        text = text.rstrip() + f"\n[[{source_name}]]\n"
    else:
        text = text.rstrip() + f"\n\n## Related\n[[{source_name}]]\n"
    # Also patch the related: frontmatter list so it stays in sync.
    m = re.search(r"^(related:\s*)\[(.*?)\]\s*$", text, re.MULTILINE)
    if m:
        items = [x.strip() for x in m.group(2).split(",") if x.strip()]
        if source_name not in items:
            items.append(source_name)
            new = f"{m.group(1)}[{', '.join(items)}]"
            text = text[:m.start()] + new + text[m.end():]
    try:
        target.write_text(text, encoding="utf-8")
    except Exception:
        pass


def save_memory(name: str, content: str, subject: str,
                related: list | None = None, description: str = "") -> str:
    """Save a memory entry.

    name:        kebab-case slug (e.g. 'coffee-tolerance').
    content:     plain-text or Markdown body.
    subject:     category folder (e.g. 'health', 'preferences', 'projects',
                 'people'). Lowercased and sanitized.
    related:     optional list of existing memory names to cross-link.
    description: optional one-line summary for the MOC.
    """
    safe = _sanitize_memory_name(name)
    if not safe:
        return "save_memory error: invalid name (use letters/numbers/hyphens)"
    body = (content or "").strip()
    if not body:
        return "save_memory error: empty content"
    subj = _sanitize_memory_name(subject) or "general"
    desc = (description or body.splitlines()[0])[:120].strip()
    rel_list = [_sanitize_memory_name(r) for r in (related or []) if r]
    rel_list = [r for r in rel_list if r and r != safe]

    # Move the entry if subject changed since last save.
    existing = _find_memory_path(safe)
    target_dir = MEMORY_ROOT / subj
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{safe}.md"
    if existing and existing != path:
        try: existing.unlink()
        except Exception: pass

    saved_at = datetime.now().isoformat(timespec="seconds")
    rel_fm = "[" + ", ".join(rel_list) + "]" if rel_list else "[]"
    frontmatter = (
        f"---\nname: {safe}\nsubject: {subj}\ntags: [{subj}]\n"
        f"related: {rel_fm}\ndescription: {desc}\nsaved_at: {saved_at}\n---\n\n"
    )
    related_section = ""
    if rel_list:
        related_section = "\n\n## Related\n" + "\n".join(f"[[{r}]]" for r in rel_list) + "\n"
    path.write_text(frontmatter + body + related_section, encoding="utf-8")

    # Bidirectional links: tell each related entry about this new one.
    for r in rel_list:
        _add_back_link(r, safe)

    _rebuild_moc()
    rel_note = f" (linked: {', '.join(rel_list)})" if rel_list else ""
    return f"saved memory '{safe}' under '{subj}'{rel_note} — {desc[:80]}"


def recall_memory(name: str) -> str:
    """Read one memory entry back. Searches across all subject folders."""
    safe = _sanitize_memory_name(name)
    if not safe:
        return "recall_memory error: invalid name"
    path = _find_memory_path(safe)
    if not path:
        return (f"no memory named '{safe}'. Use list_memory() to see all, "
                "or search_memory('keyword') to find by content.")
    try:
        return path.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
    except Exception as e:
        return f"recall_memory error: {e}"


def search_memory(query: str, subject: str = "") -> str:
    """Keyword search across memory entries. Optional subject filter."""
    q = (query or "").strip().lower()
    if not q:
        return "search_memory error: empty query"
    if not MEMORY_ROOT.exists():
        return "memory is empty"
    subj_filter = _sanitize_memory_name(subject)
    scope = (MEMORY_ROOT / subj_filter) if subj_filter else MEMORY_ROOT
    if subj_filter and not scope.exists():
        return f"no memories under subject '{subj_filter}'"
    hits = []
    for f in sorted(scope.rglob("*.md")):
        if f.name == "MOC.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        idx = content.lower().find(q)
        if idx < 0:
            continue
        start = max(0, idx - 80)
        end = min(len(content), idx + 200)
        snippet = content[start:end].replace("\n", " ").strip()
        hits.append(f"- {f.stem} ({f.parent.name}): ...{snippet}...")
        if len(hits) >= 20:
            break
    if not hits:
        scope_note = f" in '{subj_filter}'" if subj_filter else ""
        return f"no memories matching '{query}'{scope_note}"
    return f"found {len(hits)} match(es) for '{query}':\n" + "\n".join(hits)


def list_memory(subject: str = "") -> str:
    """Show the MOC (subject-grouped). Optional subject filter narrows to one section."""
    moc = MEMORY_ROOT / "MOC.md"
    if not moc.exists():
        return "memory is empty — nothing saved yet"
    try:
        text = moc.read_text(encoding="utf-8")
    except Exception as e:
        return f"list_memory error: {e}"
    subj_filter = _sanitize_memory_name(subject)
    if not subj_filter:
        return text[:MAX_FILE_CHARS]
    m = re.search(rf"^## {re.escape(subj_filter)}\s*$(.*?)(?=^## |\Z)",
                  text, re.MULTILINE | re.DOTALL)
    if not m:
        return f"no memories under subject '{subj_filter}'"
    return f"## {subj_filter}\n{m.group(1).strip()}"


def _strip_back_link(target_name: str, dead_name: str) -> None:
    """Remove [[dead_name]] from a related entry's Related section + frontmatter
    so the graph stays consistent after a delete. No-op if target is gone or
    already clean."""
    target = _find_memory_path(target_name)
    if not target:
        return
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return
    new_text = re.sub(rf"^\[\[{re.escape(dead_name)}\]\]\s*\n?", "", text, flags=re.MULTILINE)
    m = re.search(r"^(related:\s*)\[(.*?)\]\s*$", new_text, re.MULTILINE)
    if m:
        items = [x.strip() for x in m.group(2).split(",") if x.strip() and x.strip() != dead_name]
        new = f"{m.group(1)}[{', '.join(items)}]"
        new_text = new_text[:m.start()] + new + new_text[m.end():]
    if new_text != text:
        try:
            target.write_text(new_text, encoding="utf-8")
        except Exception:
            pass


def delete_memory(name: str) -> str:
    """Delete one memory entry. Strips reverse links from related entries and
    rebuilds the MOC. Only invoke when the user has explicitly asked to forget
    something — never on the model's own initiative.

    Hard scope: target path must resolve inside MEMORY_ROOT. MEMORY_ROOT lives
    under NOTES_ROOT (both in the iCloud Obsidian vault), so without this gate
    a crafted name could in theory escape into the user's Organized-notes.
    Also refuses to touch MOC.md (auto-regenerated, never user-saved memory)."""
    safe = _sanitize_memory_name(name)
    if not safe:
        return "delete_memory error: invalid name"
    if safe.lower() == "moc":
        return "delete_memory error: MOC.md is auto-generated, not a real memory"
    path = _find_memory_path(safe)
    if not path:
        return f"no memory named '{safe}' to delete"
    try:
        resolved = path.resolve()
        memory_root = MEMORY_ROOT.resolve()
        if not resolved.is_relative_to(memory_root):
            return f"delete_memory refused: '{safe}' resolves outside MEMORY_ROOT"
    except Exception as e:
        return f"delete_memory error: path check failed ({e})"
    if resolved.name == "MOC.md":
        return "delete_memory error: refuses to delete MOC.md"
    related: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^related:\s*\[(.*?)\]\s*$", text, re.MULTILINE)
        if m:
            related = [x.strip() for x in m.group(1).split(",") if x.strip()]
    except Exception:
        pass
    try:
        path.unlink()
    except Exception as e:
        return f"delete_memory error: {e}"
    for r in related:
        _strip_back_link(r, safe)
    _rebuild_moc()
    return f"deleted memory '{safe}'"


def recent_chat(limit: int = 50) -> str:
    """Return the last `limit` user/assistant turns from the main chat history.
    Used by the dream routine to know what was talked about. Reads the largest
    chat_*.json in .history/ (assumed to be the primary user)."""
    history_dir = _REPO_ROOT / ".history"
    if not history_dir.exists():
        return "no chat history found"
    candidates = sorted(history_dir.glob("chat_*.json"), key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        return "no chat history found"
    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception as e:
        return f"recent_chat error: {e}"
    if not isinstance(data, list):
        return "chat history malformed"
    tail = data[-max(1, int(limit)):]
    out = []
    for msg in tail:
        role = (msg.get("role") or "?")[:1]
        content = (msg.get("content") or "").strip().replace("\n", " ")
        if len(content) > 400:
            content = content[:400] + "…"
        out.append(f"[{role}] {content}")
    return "\n".join(out)[:MAX_FILE_CHARS]


def run_shell(command: str) -> str:
    """Run a bash command and return output. Blocks obviously destructive ops."""
    _blocked = ["rm -rf /", "format ", "del /f /s", "shutdown", ":(){ :|:& };:"]
    for b in _blocked:
        if b in command.lower():
            return f"blocked: command looks destructive (matched '{b}')"

    try:
        result = subprocess.run(
            [BASH_EXE, "-c", command],
            capture_output=True,
            timeout=SHELL_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        out = (result.stdout + result.stderr).strip()
        return out[:MAX_RESULT_CHARS] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"command timed out after {SHELL_TIMEOUT}s"
    except Exception as e:
        return f"shell error: {e}"


def send_reaction(emoji: str) -> str:
    """Send a Telegram reaction to the user's current message."""
    chat_id    = _CTX.get("chat_id")
    message_id = _CTX.get("message_id")
    print(f"[send_reaction] chat={chat_id} msg={message_id} emoji={emoji}", file=sys.stderr)
    if not chat_id or not message_id:
        return f"no message context for reaction (chat={chat_id} msg={message_id})"

    ok = _tg_post_json("setMessageReaction", {
        "chat_id":    chat_id,
        "message_id": message_id,
        "reaction":   [{"type": "emoji", "emoji": emoji}],
        "is_big":     False,
    })
    return f"reacted {emoji}" if ok else "reaction failed"


def send_message(text: str) -> str:
    """Send a fresh Telegram message to the chat in current context.

    Routes through markdown_to_telegram_html so routines and proactive pings
    render bold/italic/links/code the same way conversational replies do.
    Falls back to plain text if Telegram rejects the HTML payload — matches
    tg_send in gemma_chat.py."""
    chat_id = _CTX.get("chat_id")
    if not chat_id:
        return "no chat_id in context"
    if not text or not text.strip():
        return "empty message"
    parts = [p.strip() for p in text.split("|||") if p.strip()]
    sent = 0
    for part in parts:
        html = markdown_to_telegram_html(part)
        # Try HTML first, then fall back to the original plain text on 400.
        for attempt_html in (True, False):
            payload = {
                "chat_id": chat_id,
                "text": html if attempt_html else part,
            }
            if attempt_html:
                payload["parse_mode"] = "HTML"
                payload["disable_web_page_preview"] = True
            ok = _tg_post_json("sendMessage", payload)
            if ok:
                sent += 1
                break
            # _tg_post_json already logged the error; if HTML failed, retry plain
            if not attempt_html:
                break
        time.sleep(0.3)
    return f"sent {sent}/{len(parts)} message(s)" if sent else "send failed"


def get_voice_config() -> dict:
    """Read the current voice config. Falls back to DEFAULT_VOICE / kokoro."""
    if VOICE_CONFIG.exists():
        try:
            d = json.loads(VOICE_CONFIG.read_text(encoding="utf-8"))
            voice = d.get("voice") or DEFAULT_VOICE
            engine = d.get("engine") or ALL_VOICES.get(voice, ("", "kokoro"))[1]
            return {"engine": engine, "voice": voice}
        except Exception:
            pass
    return {"engine": "kokoro", "voice": DEFAULT_VOICE}


def set_voice_config(voice: str) -> str:
    """Persist the chosen voice. Validates against ALL_VOICES."""
    if voice not in ALL_VOICES:
        return (
            f"unknown voice: {voice!r}. "
            f"Use list_voices() or /voice list to see options."
        )
    engine = ALL_VOICES[voice][1]
    try:
        VOICE_CONFIG.write_text(
            json.dumps({"engine": engine, "voice": voice}, indent=2),
            encoding="utf-8",
        )
        return f"voice set: {voice} ({ALL_VOICES[voice][0]})"
    except Exception as e:
        return f"failed to save voice config: {e}"


def list_voices(filter: str = "") -> str:
    """Return a printable list of available voices, optionally filtered."""
    f = (filter or "").lower().strip()
    cur = get_voice_config()
    lines = [f"current voice: {cur['voice']} ({ALL_VOICES.get(cur['voice'], ('?',))[0]})", ""]
    # Group by prefix label
    groups: dict[str, list[str]] = {}
    for k, (label, _engine) in ALL_VOICES.items():
        if f and f not in k.lower() and f not in label.lower():
            continue
        parts = label.split(" · ")
        group_key = " · ".join(parts[:3]) if len(parts) >= 3 else parts[0]
        groups.setdefault(group_key, []).append(f"  {k}")
    for g in sorted(groups):
        lines.append(g)
        lines.extend(sorted(groups[g]))
        lines.append("")
    return "\n".join(lines).strip()


def _speak_kokoro(text: str, voice: str) -> str | None:
    """Kokoro path: subprocess to tts.py. Returns OGG path or None on error."""
    tts_script_win = _posix_to_win(TTS_SCRIPT)
    try:
        result = subprocess.run(
            [sys.executable, tts_script_win, text, "--voice", voice],
            capture_output=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        ogg_path = result.stdout.strip()
        if not ogg_path:
            print(f"[speak/kokoro] no path: {result.stderr[:300]}", file=sys.stderr)
            return None
        ogg_win = _posix_to_win(ogg_path)
        return ogg_win if os.path.exists(ogg_win) else (ogg_path if os.path.exists(ogg_path) else None)
    except Exception as e:
        print(f"[speak/kokoro] error: {e}", file=sys.stderr)
        return None


def _speak_omnivoice(text: str, voice: str) -> str | None:
    """OmniVoice path: POST to local HTTP server. Server auto-picks the
    right reference (-uk.wav for Cyrillic text, .wav otherwise).
    Voice id is 'omni_<name>'; server expects '<name>'.
    """
    voice_id = voice[5:] if voice.startswith("omni_") else voice
    cfg = get_voice_config()
    speed = float(cfg.get("speed", 1.0) or 1.0)
    payload = json.dumps({
        "text": text, "voice_id": voice_id, "format": "ogg", "speed": speed,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OMNIVOICE_URL}/v1/speak",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            audio = r.read()
    except urllib.error.URLError as e:
        print(f"[speak/omnivoice] server unreachable: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[speak/omnivoice] error: {e}", file=sys.stderr)
        return None
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    tmp.write(audio)
    tmp.close()
    return tmp.name


def read_aloud(input: str) -> str:
    """Spawn read-aloud.py detached and return immediately. The script handles
    fetching/extraction, TTS, and Telegram delivery on its own. We deliberately
    DO NOT capture stdout/stderr — the article body must never re-enter the LLM
    context."""
    chat_id = _CTX.get("chat_id")
    tg_token = _CTX.get("tg_token")
    if not chat_id or not tg_token:
        return "no chat context — cannot send voice"
    if not input or not input.strip():
        return "read_aloud needs an input (URL, vault path, or text)"

    script = Path(__file__).parent / "read-aloud.py"
    if not script.exists():
        return f"read-aloud.py not found at {script}"

    try:
        # Fire-and-forget: detach so the agent loop never waits on TTS.
        # Output goes to DEVNULL so no article text can leak back here.
        subprocess.Popen(
            [sys.executable, str(script), str(chat_id), tg_token, input],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
    except Exception as e:
        return f"failed to launch read-aloud: {e}"

    return "voice message queued — it will arrive in chat shortly"


_TTS_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF" "]+",
    flags=re.UNICODE,
)


def markdown_to_telegram_html(text: str) -> str:
    """Convert the bot's markdown-flavored output to Telegram HTML.

    Telegram doesn't auto-render markdown — without parse_mode=HTML, **bold**
    arrives as literal asterisks. We convert the common markdown patterns to
    the HTML tags Telegram supports (<b>, <i>, <s>, <u>, <code>, <pre>,
    <a href>, <blockquote>, <tg-spoiler>) so the formatting renders cleanly.

    Robust to weird input: any literal <, >, & in the source is HTML-escaped
    so it can't break parsing. If the result is malformed for Telegram,
    tg_send falls back to plain text.
    """
    if not text:
        return ""

    # Stash links before escaping so URLs survive intact.
    urls: list[str] = []
    def _stash_link(m: re.Match) -> str:
        urls.append(m.group(2))
        return f"\x00L{len(urls)-1}\x00{m.group(1)}\x00E\x00"
    s = re.sub(r"!?\[([^\]]+)\]\(([^)\s]+)\)", _stash_link, text)

    # Fenced code blocks → <pre>…</pre>
    s = re.sub(
        r"```([a-zA-Z0-9_+-]*)\n?(.*?)```",
        lambda m: f"\x00P\x00{m.group(2)}\x00/P\x00",
        s, flags=re.DOTALL,
    )

    # HTML-escape the WHOLE thing so any literal < > & in user content is safe.
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Inline code (after escaping — content shouldn't contain backticks anyway)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)

    # Bold / italic / strike — longer wrappers first, word-boundary aware for singles
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__([^_\n]+)__",     r"<b>\1</b>", s)
    s = re.sub(r"~~([^~\n]+)~~",     r"<s>\1</s>", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)",   r"<i>\1</i>", s)

    # Headings → bold (Telegram has no heading tag)
    s = re.sub(r"^\s{0,3}#{1,6}\s*(.+)$", r"<b>\1</b>", s, flags=re.MULTILINE)

    # Blockquotes → <blockquote>
    s = re.sub(r"(?:^\s{0,3}&gt;\s?(.+)\n?)+",
               lambda m: "<blockquote>" + re.sub(r"^\s{0,3}&gt;\s?", "", m.group(0), flags=re.MULTILINE).rstrip() + "</blockquote>\n",
               s, flags=re.MULTILINE)

    # Horizontal rules — drop
    s = re.sub(r"^\s*[-*_~]{3,}\s*$", "", s, flags=re.MULTILINE)

    # Restore fenced code
    s = s.replace("\x00P\x00", "<pre>").replace("\x00/P\x00", "</pre>")

    # Restore links → <a href="url">label</a>  (url escaped, label already processed)
    def _restore_link(m: re.Match) -> str:
        url = urls[int(m.group(1))].replace("&", "&amp;").replace('"', "&quot;")
        return f'<a href="{url}">{m.group(2)}</a>'
    s = re.sub(r"\x00L(\d+)\x00(.*?)\x00E\x00", _restore_link, s, flags=re.DOTALL)

    # Collapse triple+ blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def clean_for_tts(text: str) -> str:
    """Strip markdown/HTML/emojis/separators so TTS reads only natural prose.

    Handles: **bold**, __bold__, *italic*, _italic_, ~~strike~~, ~strike~,
    `code`, ```fenced```, [text](url) → text, # headings, > blockquotes,
    <html> tags, ||| message separators, and stray emojis.
    """
    if not text:
        return ""
    s = text
    # Fenced code blocks (```lang\n...\n```) — drop wrapper, keep content
    s = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", s)
    s = s.replace("```", "")
    # Inline code `x`
    s = re.sub(r"`([^`]*)`", r"\1", s)
    # Markdown links [text](url) → text   (image links ![alt](url) → alt)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # Bold / italic — strip the wrapping characters; do the longer marker first
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__",     r"\1", s)
    s = re.sub(r"~~([^~]+)~~",     r"\1", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)",   r"\1", s)
    s = re.sub(r"(?<!\w)~([^~\n]+)~(?!\w)",   r"\1", s)
    # Headings (# ## ### …) at line start — drop the #'s
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.MULTILINE)
    # Blockquote markers
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.MULTILINE)
    # HTML tags — strip
    s = re.sub(r"<[^>]+>", "", s)
    # ||| sentinel → natural pause
    s = re.sub(r"\s*\|\|\|\s*", ". ", s)
    # Stray pipes (tables) → space
    s = s.replace("|", " ")
    # Backslash escapes (\* \_ etc.) — drop the backslash
    s = re.sub(r"\\([*_~`|\\])", r"\1", s)
    # Horizontal rules and stray markdown punctuation runs (***, ---, ___, ~~~)
    s = re.sub(r"^\s*[-*_~]{3,}\s*$", "", s, flags=re.MULTILINE)
    # Stray runs of 2+ markdown chars left over after the wrappers ran
    s = re.sub(r"[*_~]{2,}", "", s)
    # Emojis
    s = _TTS_EMOJI_RE.sub(" ", s)
    # Collapse whitespace + fix punctuation spacing
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([.,!?;:])", r"\1", s)
    return s


def speak(text: str, voice: str = "") -> str:
    """Generate TTS and send as Telegram voice message.

    Routes to the right backend based on the selected voice's engine.
    Voice resolution: explicit override > saved config > default.
    """
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return "no context for voice"

    # Always sanitize — TTS shouldn't read "asterisk asterisk bold asterisk asterisk".
    text = clean_for_tts(text)
    if not text:
        return "speak error: empty after cleanup"

    if voice and voice in ALL_VOICES:
        active_voice = voice
    else:
        active_voice = get_voice_config().get("voice", DEFAULT_VOICE)
    engine = ALL_VOICES.get(active_voice, ("", "kokoro"))[1]

    _tg_post_json("sendChatAction", {"chat_id": chat_id, "action": "record_voice"})

    if engine == "omnivoice":
        ogg_path = _speak_omnivoice(text, active_voice)
    else:
        ogg_path = _speak_kokoro(text, active_voice)

    if not ogg_path:
        return f"TTS error ({engine}/{active_voice}): backend produced no audio"

    ok = _tg_send_file("sendVoice", "voice", ogg_path, "audio/ogg")
    try:
        os.remove(ogg_path)
    except Exception:
        pass
    return f"voice message sent ({active_voice})" if ok else "failed to send voice"


def take_screenshot(url: str) -> str:
    """Screenshot a URL via the camofox stealth browser (always running on
    localhost:9377). Returns an absolute path to a PNG, or an error string.

    Falls back to the old fetch-url.js (Chrome CDP on :9222) if camofox is
    unreachable AND the legacy script is present — covers users who still
    have Chrome running with --remote-debugging-port=9222.

    The earlier version called fetch-url.js exclusively, which silently
    failed whenever Chrome wasn't running with remote debugging enabled.
    Camofox is started by PM2 on every boot, so this path is reliable."""
    tab_id = None
    out_path = ""
    try:
        # Make sure the destination directory exists
        out_dir = _REPO_ROOT / ".tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"shot_{int(time.time()*1000)}.png")

        # Create a fresh tab pointing at the URL
        payload = json.dumps({
            "userId": CAMOFOX_USER,
            "sessionKey": "default",
            "url": url,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            tab_id = json.loads(r.read().decode("utf-8")).get("tabId")
        if not tab_id:
            raise RuntimeError("camofox returned no tabId")

        # Give JS-heavy pages a moment to render
        time.sleep(2)

        # GET /tabs/{id}/screenshot returns the raw PNG body
        shot_url = (f"{CAMOFOX_URL}/tabs/{tab_id}/screenshot"
                    f"?userId={CAMOFOX_USER}&fullPage=true")
        with urllib.request.urlopen(shot_url, timeout=45) as r:
            data = r.read()
        if not data or not data.startswith(b"\x89PNG"):
            raise RuntimeError("camofox returned non-PNG payload")
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

    except urllib.error.URLError as e:
        # Camofox down — try the legacy CDP-based path as a fallback
        if os.path.exists(_posix_to_win(SCREENSHOT_SCRIPT)):
            try:
                result = subprocess.run(
                    [BASH_EXE, "-c",
                     f'timeout 120 node "{SCREENSHOT_SCRIPT}" "{url}" --screenshot'],
                    capture_output=True, timeout=130,
                    encoding="utf-8", errors="replace",
                )
                path = (result.stdout or "").strip()
                if path:
                    win_path = _posix_to_win(path)
                    if os.path.exists(win_path):
                        return win_path
                    if os.path.exists(path):
                        return path
                return f"screenshot failed (camofox down, CDP fallback also failed): {result.stderr[:200]}"
            except Exception as fb_e:
                return f"screenshot failed (camofox down: {e}; fallback error: {fb_e})"
        return f"screenshot failed: camofox unreachable at {CAMOFOX_URL} ({e}). Check: pm2 list, pm2 logs camofox"
    except Exception as e:
        return f"screenshot error: {e}"
    finally:
        # Always close the tab so we don't leak browser memory
        if tab_id:
            try:
                req = urllib.request.Request(
                    f"{CAMOFOX_URL}/tabs/{tab_id}?userId={CAMOFOX_USER}",
                    method="DELETE",
                )
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                pass


def send_sticker(mood: str) -> str:
    """Send a sticker matching the given mood."""
    import random
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return "no context for sticker"

    try:
        lib = json.loads(STICKER_LIB.read_text(encoding="utf-8"))
    except Exception as e:
        return f"sticker library error: {e}"

    mood = mood.lower().strip()
    stickers = lib.get(mood, [])
    if not stickers:
        # Try user-saved stickers or fallback to happy
        stickers = lib.get("happy", [])
    if not stickers:
        return f"no stickers for mood: {mood}"

    file_id = random.choice(stickers)
    ok = _tg_post_json("sendSticker", {"chat_id": chat_id, "sticker": file_id})
    return f"sent {mood} sticker" if ok else "sticker send failed"


def send_image(path: str, caption: str = "") -> str:
    """Send an image to the user on Telegram."""
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return "no context for sending image"

    win_path = _posix_to_win(path)
    if not os.path.exists(win_path):
        if os.path.exists(path):
            win_path = path
        else:
            return f"image file not found: {path}"

    extra = {"caption": caption} if caption else {}
    ok = _tg_send_file("sendPhoto", "photo", win_path, "image/jpeg", extra)
    return "image sent" if ok else "failed to send image"


# ── Routines management ───────────────────────────────────────────────────────

def _load_routines_config() -> dict:
    """Load routines.json, returning a default-shaped dict on any error."""
    if not ROUTINES_CONFIG.exists():
        return {"chat_id": 165548659, "routines": []}
    try:
        data = json.loads(ROUTINES_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(data.get("routines"), list):
            data["routines"] = []
        return data
    except Exception:
        return {"chat_id": 165548659, "routines": []}


def _save_routines_config(data: dict) -> bool:
    try:
        ROUTINES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = ROUTINES_CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ROUTINES_CONFIG)
        return True
    except Exception as e:
        print(f"[routines] save error: {e}", file=sys.stderr)
        return False


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    return s[:max_len] or "routine"


def create_routine(id: str = "", schedule: str = "", prompt: str = "",
                   one_shot: bool = False, delay_seconds: int = 0) -> str:
    """Create or replace a scheduled routine / one-shot reminder.

    Two ways to schedule:
    1. delay_seconds: fire N seconds from now. Always one-shot. Tool computes
       a concrete clock-time cron. Use for "in X minutes/hours/days" requests.
    2. schedule: a 5-field cron expression. Use for specific clock times or
       recurring schedules ("every Tuesday at 5pm", "0 8 * * *").
    """
    import time as _time
    from datetime import datetime, timedelta
    if not prompt:
        return "create_routine: 'prompt' is required"

    # delay_seconds takes precedence — model uses this for "in N minutes" requests
    if delay_seconds and delay_seconds > 0:
        target = datetime.now() + timedelta(seconds=int(delay_seconds))
        # The routines daemon polls every 30s; round UP to the next minute so we
        # don't accidentally land in the past by the time it checks.
        if target.second > 0:
            target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        else:
            target = target.replace(second=0, microsecond=0)
        schedule = f"{target.minute} {target.hour} {target.day} {target.month} *"
        one_shot = True   # delayed reminders are always one-shot

    if not schedule:
        return "create_routine: either 'schedule' (cron) or 'delay_seconds' is required"

    # Validate cron
    try:
        from croniter import croniter
        croniter(schedule)
    except Exception as e:
        return (
            f"invalid cron expression {schedule!r}: {e}. "
            "Use 5 fields: minute hour day month weekday. "
            "Examples: '0 8 * * *' (daily 8am), '0 17 * * 2' (Tue 5pm). "
            "For 'in N minutes', pass delay_seconds instead."
        )

    rid = (id or "").strip() or f"r-{_slugify(prompt)}"
    data = _load_routines_config()
    # Replace if exists
    data["routines"] = [r for r in data["routines"] if r.get("id") != rid]
    data["routines"].append({
        "id":        rid,
        "schedule":  schedule,
        "enabled":   True,
        "one_shot":  bool(one_shot),
        "prompt":    prompt,
    })
    if not _save_routines_config(data):
        return "failed to write routines.json"
    kind = "one-shot reminder" if one_shot else "recurring routine"
    return f"created {kind} '{rid}': schedule={schedule} — {prompt[:80]}"


def list_routines() -> str:
    data = _load_routines_config()
    rs = data.get("routines", [])
    if not rs:
        return "(no routines defined yet)"
    lines = []
    for r in rs:
        status = "✓" if r.get("enabled") else "✗"
        kind = "once" if r.get("one_shot") else "loop"
        prompt = (r.get("prompt") or "").replace("\n", " ")[:70]
        lines.append(f"{status} [{kind}] {r.get('id')}: {r.get('schedule')} — {prompt}")
    return "\n".join(lines)


def delete_routine(id: str) -> str:
    if not id:
        return "delete_routine: id is required"
    data = _load_routines_config()
    before = len(data["routines"])
    data["routines"] = [r for r in data["routines"] if r.get("id") != id]
    after = len(data["routines"])
    if before == after:
        return f"routine '{id}' not found"
    if not _save_routines_config(data):
        return "failed to write routines.json"
    return f"deleted routine '{id}'"


# ── Dispatcher ────────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    try:
        # MCP tools — route by namespace prefix. Fast no-op if mcp_client failed
        # to import or no servers are enabled.
        if _mcp_client is not None and name.startswith("mcp__"):
            return _mcp_client.call_tool(name, args or {})
        if name == "web_search":
            return web_search(args.get("query", ""))
        elif name == "web_fetch":
            return web_fetch(args.get("url", ""))
        elif name == "browser_open":
            return browser_open(args.get("url", ""))
        elif name == "read_file":
            return read_file(args.get("path", ""), int(args.get("offset", 0) or 0))
        elif name == "list_folder":
            return list_folder(args.get("path", ""))
        elif name == "write_file":
            return write_file(args.get("path", ""), args.get("content", ""))
        elif name == "read_pdf":
            return read_pdf(args.get("path", ""),
                            int(args.get("max_pages", PDF_MAX_PAGES) or PDF_MAX_PAGES))
        elif name == "send_file":
            return send_file(args.get("path", ""), args.get("caption", ""))
        elif name == "move_file":
            return move_file(args.get("src", ""), args.get("dst", ""))
        elif name == "delete_file":
            return delete_file(args.get("path", ""))
        elif name == "save_memory":
            return save_memory(
                args.get("name", ""), args.get("content", ""),
                args.get("subject", "") or "general",
                related=args.get("related") or [],
                description=args.get("description", ""),
            )
        elif name == "recall_memory":
            return recall_memory(args.get("name", ""))
        elif name == "search_memory":
            return search_memory(args.get("query", ""), args.get("subject", ""))
        elif name == "list_memory":
            return list_memory(args.get("subject", ""))
        elif name == "delete_memory":
            return delete_memory(args.get("name", ""))
        elif name == "recent_chat":
            return recent_chat(int(args.get("limit", 50) or 50))
        elif name == "run_shell":
            return run_shell(args.get("command", ""))
        elif name == "send_reaction":
            return send_reaction(args.get("emoji", "👍"))
        elif name == "send_message":
            return send_message(args.get("text", ""))
        elif name == "speak":
            return speak(args.get("text", ""))
        elif name == "read_aloud":
            return read_aloud(args.get("input", ""))
        elif name == "take_screenshot":
            return take_screenshot(args.get("url", ""))
        elif name == "send_image":
            return send_image(args.get("path", ""), args.get("caption", ""))
        elif name == "send_sticker":
            return send_sticker(args.get("mood", "happy"))
        elif name == "create_routine":
            return create_routine(
                id=args.get("id", ""),
                schedule=args.get("schedule", ""),
                prompt=args.get("prompt", ""),
                one_shot=bool(args.get("one_shot", False)),
                delay_seconds=int(args.get("delay_seconds", 0) or 0),
            )
        elif name == "list_routines":
            return list_routines()
        elif name == "delete_routine":
            return delete_routine(args.get("id", ""))
        else:
            return f"unknown tool: {name}"
    except Exception as e:
        return f"tool error ({name}): {e}"
