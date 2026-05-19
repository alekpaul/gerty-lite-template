"""
GERTY Lite — /read command handler.

Standalone (no LLM). Takes an input that is one of:
  - a URL (http/https)           → fetch + extract main article body via trafilatura
  - a vault file path            → read the file
    (e.g. 'obsidian/Organized-notes/CLAUDE.md', 'inbox/note.md', 'drafts/foo.md')
  - raw text                     → speak as-is

Pipes the resulting text through OmniVoice TTS and sends the resulting OGG
to Telegram as a voice message in the given chat.

Usage:
  python read-aloud.py <chat_id> <tg_bot_token> "<input>"
  python read-aloud.py <chat_id> <tg_bot_token> --from-b64 <base64-content>

The --from-b64 form is used by the listener: it passes the raw base64 of the
user's message, this script decodes it, checks if it starts with '/read ',
and only acts if so.

Exit codes:
  0 = success (or /read with empty input — reported to user)
  1 = was /read but failed (already reported to user)
  2 = not a /read command (listener should fall through to the LLM)
"""

import base64 as _b64

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_paths_for_read_aloud() -> tuple[Path, Path]:
    """Minimal duplicate of tools.py's path loader so this script stays
    standalone (it's spawned as a subprocess, not imported)."""
    paths = {}
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
        except Exception:
            pass

    def _r(v: str) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (_REPO_ROOT / p).resolve()
    return (
        _r(paths.get("VAULT_ROOT", "data/vault")),
        _r(paths.get("NOTES_ROOT", "data/notes")),
    )


VAULT_ROOT, OBSIDIAN_ROOT = _load_paths_for_read_aloud()
VOICE_CONFIG = _REPO_ROOT / ".voice-config.json"
OMNIVOICE_URL = "http://127.0.0.1:8883"
KOKORO_TTS = Path(os.environ.get("GERTY_KOKORO_TTS", "D:/Claude/scripts/tts.py"))

DEFAULT_VOICE = "am_echo"
DEFAULT_ENGINE = "kokoro"
MAX_TEXT_CHARS = 30000      # ~25-30 min of audio; protects OmniVoice
FETCH_TIMEOUT = 30
TTS_TIMEOUT = 600           # long articles can take minutes
TG_TIMEOUT = 120


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_vault_path(path: str) -> Path:
    if path == "obsidian":
        return OBSIDIAN_ROOT
    if path.startswith("obsidian/"):
        return OBSIDIAN_ROOT / path[len("obsidian/"):]
    return VAULT_ROOT / path


def _load_voice() -> tuple[str, str]:
    """Return (engine, voice) — falls back to defaults if config missing."""
    if VOICE_CONFIG.exists():
        try:
            d = json.loads(VOICE_CONFIG.read_text(encoding="utf-8"))
            voice = d.get("voice") or DEFAULT_VOICE
            engine = d.get("engine") or DEFAULT_ENGINE
            return engine, voice
        except Exception:
            pass
    return DEFAULT_ENGINE, DEFAULT_VOICE


def _looks_like_url(s: str) -> bool:
    s = s.strip()
    return s.startswith(("http://", "https://"))


def _looks_like_vault_path(s: str) -> bool:
    """Heuristic: starts with a known vault dir prefix, OR resolves to an
    existing file relative to the vault."""
    s = s.strip()
    if "\n" in s or len(s) > 500:
        return False
    if s.startswith(("obsidian/", "inbox/", "drafts/", "published/",
                     "strategy-ideas/", "templates/", "resources/",
                     "archive/", "config/", "products/", "docs/",
                     "gerty-memory/")):
        return True
    return _resolve_vault_path(s).is_file()


# ── input → text ─────────────────────────────────────────────────────────────

def _fetch_url(url: str) -> str:
    """Fetch + extract main article body using trafilatura."""
    import trafilatura
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        html = r.read().decode("utf-8", errors="replace")
    text = trafilatura.extract(html, include_comments=False,
                                include_tables=False, no_fallback=False)
    if not text or not text.strip():
        raise ValueError(
            "trafilatura found no article body — page is likely JS-rendered, "
            "behind a paywall, or has unusual structure."
        )
    return text.strip()


def _read_vault_file(path: str) -> str:
    full = _resolve_vault_path(path)
    if not full.exists():
        raise FileNotFoundError(f"file not found: {path}")
    return full.read_text(encoding="utf-8").strip()


def _resolve_input(arg: str) -> tuple[str, str]:
    """Return (source_label, text) for the input."""
    arg = arg.strip()
    if not arg:
        raise ValueError("empty input")
    if _looks_like_url(arg):
        return f"url: {arg}", _fetch_url(arg)
    if _looks_like_vault_path(arg):
        return f"file: {arg}", _read_vault_file(arg)
    return "text", arg


# ── TTS ──────────────────────────────────────────────────────────────────────

def _tts_omnivoice(text: str, voice: str, speed: float = 1.0) -> str:
    """POST text to OmniVoice, return path to a temp OGG file."""
    voice_id = voice[5:] if voice.startswith("omni_") else voice
    payload = json.dumps({
        "text": text,
        "voice_id": voice_id,
        "format": "ogg",
        "speed": speed,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OMNIVOICE_URL}/v1/speak",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as r:
        audio = r.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    tmp.write(audio)
    tmp.close()
    return tmp.name


def _tts_kokoro(text: str, voice: str) -> str:
    """Kokoro path: shells out to tts.py, returns OGG path."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(KOKORO_TTS), text, "--voice", voice],
        capture_output=True, timeout=TTS_TIMEOUT,
        encoding="utf-8", errors="replace",
    )
    path = result.stdout.strip()
    if not path or not os.path.exists(path):
        raise RuntimeError(
            f"Kokoro produced no audio. stderr: {result.stderr[:300]}"
        )
    return path


# ── Telegram ────────────────────────────────────────────────────────────────

def _tg_post(token: str, endpoint: str, payload: dict) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"[tg_post] {endpoint} error: {e}", file=sys.stderr)
        return False


def _tg_send_voice(token: str, chat_id: int, ogg_path: str,
                   caption: str = "") -> bool:
    with open(ogg_path, "rb") as f:
        audio = f.read()
    boundary = "----GertyReadBoundary"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
    ]
    if caption:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode()
        )
    parts.append((
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"voice\"; filename=\"voice.ogg\"\r\n"
        f"Content-Type: audio/ogg\r\n\r\n"
    ).encode())
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendVoice",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib.request.urlopen(req, timeout=TG_TIMEOUT).read()
        return True
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"[tg_send_voice] HTTP {e.code}: {body_text}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[tg_send_voice] error: {e}", file=sys.stderr)
        return False


# ── main ─────────────────────────────────────────────────────────────────────

def _decode_b64_command(b64: str) -> tuple[bool, str]:
    """Decode a base64 message body and check whether it's a /read command.
    Returns (is_read_command, argument_text)."""
    try:
        text = _b64.b64decode(b64.encode("ascii")).decode("utf-8", "replace").lstrip()
    except Exception:
        return False, ""
    lower = text.lower()
    if lower.startswith("/read "):
        return True, text[len("/read "):].strip()
    if lower == "/read":
        return True, ""
    return False, ""


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: read-aloud.py <chat_id> <tg_token> <input> "
              "| --from-b64 <b64>", file=sys.stderr)
        return 1
    chat_id = int(sys.argv[1])
    token = sys.argv[2]

    if sys.argv[3] == "--from-b64":
        if len(sys.argv) < 5:
            print("--from-b64 requires the b64 payload as arg 4", file=sys.stderr)
            return 1
        is_read, arg_text = _decode_b64_command(sys.argv[4])
        if not is_read:
            return 2     # listener: fall through to LLM
        raw_input = arg_text
        if not raw_input:
            _tg_post(token, "sendMessage", {
                "chat_id": chat_id,
                "text": "usage: /read <url | vault/path | any text>",
            })
            return 0
    else:
        raw_input = " ".join(sys.argv[3:]).strip()

    try:
        source, text = _resolve_input(raw_input)
    except Exception as e:
        msg = f"can't read that — {e}"
        _tg_post(token, "sendMessage", {"chat_id": chat_id, "text": msg})
        print(msg, file=sys.stderr)
        return 1

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        truncated_note = f"\n\n(text was truncated to {MAX_TEXT_CHARS} chars)"
    else:
        truncated_note = ""

    # Heads-up: tell the user what we're about to read
    preview = (text[:80] + "…") if len(text) > 80 else text
    preview = preview.replace("\n", " ")
    _tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "record_voice"})
    _tg_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"reading ({source}, {len(text)} chars):\n{preview}{truncated_note}",
    })

    # Strip markdown/HTML/emojis before TTS — articles often have ** _ etc.
    try:
        from tools import clean_for_tts
        text = clean_for_tts(text) or text
    except Exception:
        pass

    engine, voice = _load_voice()
    try:
        if engine == "omnivoice":
            ogg_path = _tts_omnivoice(text, voice)
        else:
            ogg_path = _tts_kokoro(text, voice)
    except Exception as e:
        msg = f"TTS failed ({engine}/{voice}): {e}"
        _tg_post(token, "sendMessage", {"chat_id": chat_id, "text": msg})
        print(msg, file=sys.stderr)
        return 1

    audio_size = os.path.getsize(ogg_path)
    ok = _tg_send_voice(token, chat_id, ogg_path)
    try:
        os.remove(ogg_path)
    except Exception:
        pass

    if not ok:
        _tg_post(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"generated {audio_size} bytes of audio but failed to send to Telegram",
        })
        return 1

    print(f"read-aloud done: {source} → {len(text)} chars → voice sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
