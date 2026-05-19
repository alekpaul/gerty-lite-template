#!/usr/bin/env python3
"""
GERTY Lite — Gemma chat handler
Called by gemma-listener.sh for every message.
Handles: load history → call Gemma → save history → send to Telegram.
All I/O in UTF-8. No text ever passes through a bash variable.

Usage:
  python gemma_chat.py text    <chat_id> <history_path> <system_prompt_file> <llm_api> <model> <max_history> <tg_token>
  python gemma_chat.py image   <chat_id> <history_path> <system_prompt_file> <llm_api> <model> <max_history> <tg_token> <image_path>

User text (for 'text' mode) is read from stdin.
Caption (for 'image' mode) is read from stdin.

Exit codes:
  0 = reply sent successfully
  1 = LLM offline or empty reply
  2 = other error
"""

import sys, os, json, base64, time, re
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

# Add script dir to path so tools.py is importable
sys.path.insert(0, str(Path(__file__).parent))
from tools import TOOL_SCHEMAS, execute_tool, set_context

TOOLS_ENABLED = True   # set False to disable the agentic loop
# Agentic loop limits. Default raised to 50 so longer multi-step tasks (e.g.
# "summarise all my notes for the month" → many read_file calls) don't get cut
# short. Wall-clock safety net at 10 min stops runaway loops. Tune via env.
AGENT_MAX_TURNS   = int(os.environ.get("GERTY_AGENT_MAX_TURNS",   "50"))
AGENT_MAX_SECONDS = int(os.environ.get("GERTY_AGENT_MAX_SECONDS", "600"))

# Auxiliary LLM calls (router + reaction picker) used to go through a small
# qwen3-0.6b to dodge Gemma's forced-reasoning that ate max_tokens. Disabled
# here to free the ~500 MB of VRAM qwen3 was holding. pick_reaction is now
# rule-based; select_tools falls back to keyword heuristics + ALWAYS_ON.
# Flip back to True (and re-add AUX_MODEL = "qwen3-0.6b") if you want semantic
# routing again at the cost of the extra model in memory.
AUX_LLM_ENABLED = False

# Regex that matches emoji + pictographic codepoint ranges. Used to strip
# emojis from TTS input so the synthesizer doesn't read them as garble
# (NeuTTS/OmniVoice can render emoji glyphs as random phonemes).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F780-\U0001F7FF"   # geometric extended
    "\U0001F800-\U0001F8FF"   # supplemental arrows
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess / symbols extended-A
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-B
    "\U0001F000-\U0001F02F"   # mahjong
    "\U0001F0A0-\U0001F0FF"   # playing cards
    "\U0001F100-\U0001F1FF"   # enclosed alphanumerics suppl + flags
    "\U0001F200-\U0001F2FF"   # enclosed CJK
    "\U00002600-\U000026FF"   # misc symbols (☀ ☂ ☁ …)
    "\U00002700-\U000027BF"   # dingbats (✂ ✈ ✏ …)
    "‍"                  # zero-width joiner (compound emoji)
    "️"                  # variation selector
    "]+",
    flags=re.UNICODE,
)


def _clean_for_tts(text: str) -> str:
    """Strip markdown, separators, emojis, etc. before TTS.

    Delegates to tools.clean_for_tts (single source of truth — also used by
    tools.speak so direct tool calls get the same cleanup)."""
    from tools import clean_for_tts
    return clean_for_tts(text)

from _paths import repo_root as _repo_root  # cross-platform path discovery

THINKING_FLAG_FILE = _repo_root() / ".thinking-mode"

def thinking_mode() -> dict:
    """Return LM Studio thinking param based on the toggle file."""
    try:
        if THINKING_FLAG_FILE.read_text().strip() == "on":
            return {"type": "enabled", "budget_tokens": 2048}
    except Exception:
        pass
    return {"type": "disabled"}


# ── /goal: sustained per-chat objective ──────────────────────────────────────
# A single, user-controlled goal that persists across /new resets and gets
# injected into the system prompt every turn until the user clears it. Stored
# per-chat so multi-tenant setups don't cross-contaminate. The model can
# *reference* the goal but cannot change it — only the user via /goal can.
GOAL_DIR = _repo_root() / ".goals"


def _goal_path(chat_id: int) -> Path:
    return GOAL_DIR / f"{chat_id}.json"


def load_goal(chat_id: int) -> dict | None:
    p = _goal_path(chat_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_goal(chat_id: int, text: str) -> dict:
    GOAL_DIR.mkdir(parents=True, exist_ok=True)
    g = {"text": text, "set_at": datetime.now().isoformat(timespec="seconds")}
    _goal_path(chat_id).write_text(
        json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return g


def clear_goal(chat_id: int) -> bool:
    p = _goal_path(chat_id)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False

# Tools that are ALWAYS exposed to the model on every turn, regardless of
# router decision. Use sparingly — every always-on tool adds ~150 chars to the
# system prompt addendum on every call. Notes/vault tools live here because the
# bot needs them reachable on most turns (daily-note reads, quick saves,
# progress updates). Reaction/router calls still skip the LLM router itself,
# but the agent's main run still gets these tools available.
ALWAYS_ON_TOOLS = (
    "read_file", "write_file", "list_folder", "list_memory", "recall_memory",
    "mcp__sqlite__read_query", "mcp__sqlite__write_query",
    "mcp__sqlite__create_table", "mcp__sqlite__list_tables",
    "mcp__sqlite__describe_table", "mcp__sqlite__append_insight",
)

# One-line router descriptions — used by the tool-router LLM call.
# Keep these short; the router is a fast small-token call.
TOOL_ONE_LINERS = {
    "web_search":      "search the web (DuckDuckGo) for current info, news, facts",
    "web_fetch":       "fetch a URL's text content (cheap, fails on JS/anti-scraper sites)",
    "browser_open":    "open URL in stealth headless browser — JS sites, Cloudflare, X/Twitter, LinkedIn, paywalls",
    "read_file":       "read a file from the vault or notes (with 'obsidian/' prefix)",
    "write_file":      "write or overwrite a file in the vault or notes",
    "list_folder":     "list files + subfolders of a vault/notes folder (non-recursive, read-only)",
    "save_memory":     "save a long-term memory entry (when user says 'remember this') — must pick a subject",
    "recall_memory":   "read back one saved memory by name",
    "search_memory":   "search across all saved memories by keyword (optional subject filter)",
    "list_memory":     "show the memory Map of Content grouped by subject (optional subject filter)",
    "delete_memory":   "delete one saved memory by name — ONLY when user explicitly says 'forget'/'забудь'/'delete that memory'",
    "recent_chat":     "return last N user/assistant turns from chat history — used by the dream routine",
    "run_shell":       "run a bash command for system tasks (NOT for vault/notes)",
    "send_sticker":    "send a sticker by mood (laugh, love, hello, thinking, etc.)",
    "speak":           "send a voice message via TTS",
    "read_aloud":      "read URL article / vault file / text aloud as voice (does NOT return content)",
    "take_screenshot": "screenshot a URL — returns image path",
    "send_image":      "send an image file to the user",
    "send_message":    "send a fresh Telegram message (use in routines, not normal replies)",
    "create_routine":  "create a scheduled routine OR a one-shot reminder (cron expression + prompt)",
    "list_routines":   "list all routines / reminders the user has set up",
    "delete_routine":  "delete a routine / reminder by id",
}

# Detailed per-tool guidance — only the selected tools' lines go into the prompt.
_TOOL_DETAILS = {
    "browser_open": "- browser_open(url): stealth browser fallback when web_fetch returns nothing useful (JS-required, Cloudflare, login wall). Use for Twitter/X, Instagram, LinkedIn, SPAs, paywalled news.",
    "web_fetch":    "- web_fetch(url): default for getting page text. Fast and cheap. If it fails or returns junk, escalate to browser_open.",
    "web_search":   "- web_search(query): DuckDuckGo search; returns titles, snippets, urls.",
    "read_file":    "- read_file(path): vault root (e.g. 'inbox/note.md') OR notes vault via 'obsidian/' prefix. Today's daily note: read_file('obsidian/Progress/{today}.md'). NEVER use run_shell for vault/notes.",
    "write_file":   "- write_file(path, content): vault root or 'obsidian/' prefix. Today's daily note path is ALWAYS obsidian/Progress/{today}.md — never invent a different year/path. To update today's note: read_file first, then write_file with the full updated content (write_file replaces; it doesn't append). If read_file returns 'file not found' for the daily note, call list_folder('obsidian/Progress') FIRST and look for any file starting with today's date (e.g. '{today} 2.md', '{today}-work.md' — iCloud sync conflicts often add a ' 2' suffix). Only create a fresh daily note if list_folder shows nothing matching today's date.",
    "list_folder":  "- list_folder(path): non-recursive listing of files + subfolders. Path '' = vault root. 'obsidian' = notes root. 'obsidian/Progress' = the daily notes folder. Cheap call — use it before guessing a path exists, especially when read_file returned 'file not found'.",
    "save_memory":  "- save_memory(name, content, subject, [related=[], description]): save a long-term memory. Use whenever the user says 'remember', 'запам'ятай', 'don't forget', or asks you to keep track of a fact/preference/name. Name is kebab-case (e.g. 'kid-birthday', 'favorite-coffee'). `subject` is required — pick one of: 'health', 'preferences', 'people', 'projects', 'work', 'spiritual', 'general'. Pass `related=['other-name', ...]` to cross-link with existing memories (auto-bidirectional). Lives in the Obsidian vault.",
    "recall_memory": "- recall_memory(name): read one saved memory by exact name. If you don't know the name, use list_memory first or search_memory with a keyword.",
    "search_memory": "- search_memory(query, [subject]): keyword search. Use when the user asks 'what do you remember about X', 'do you know my Y'. Optional subject narrows to one folder.",
    "list_memory":  "- list_memory([subject]): show the memory Map of Content grouped by subject. Cheap call. Use when the user asks 'what do you remember about me' or 'what's in your memory'. Optional subject filter shows only one section.",
    "delete_memory": "- delete_memory(name): permanently delete one memory entry by exact kebab-case slug. ONLY call this when the user explicitly asks to forget/delete a memory ('forget my old address', 'забудь про X', 'delete that memory'). NEVER delete on your own. If the user is vague about which entry, call list_memory or search_memory first and ask them to confirm the name before deleting. Operates ONLY inside the memory vault — your own notes only, never the user's organized notes.",
    "recent_chat": "- recent_chat(limit=50): return last N user/assistant turns from chat history, one per line. Used by the dream routine to scan recent conversation and decide what's worth saving.",
    "run_shell":    "- run_shell(command): bash command for system tasks. Do NOT use for vault/obsidian file access.",
    "send_sticker": "- send_sticker(mood): moods — laugh, love, agree, hello, happy, thinking, sad, surprised, cool, celebrate, clap, bored, wink, strong, angry, question, smug, dance. Use occasionally.",
    "speak":        "- speak(text): voice message via TTS. Use when asked or when audio feels natural.",
    "read_aloud":   "- read_aloud(input): read URL/vault-file/raw-text aloud as Telegram voice. Fire-and-forget — returns 'voice queued', NOT the article body. Use ONLY when user asks to hear/read aloud/voice/прочитати/озвучити something. Never use to learn what's in a URL (use web_fetch instead).",
    "take_screenshot": "- take_screenshot(url) + send_image(path, caption): screenshot a page and send it.",
    "send_image":   "- send_image(path, caption): send a local image file to the user.",
    "send_message": "- send_message(text): send a fresh Telegram message to the user. Routines use this to deliver output; in a normal reply, just return text instead.",
    "create_routine": (
        "- create_routine(prompt, delay_seconds OR schedule, ...): schedule a routine or reminder. Today: {today}.\n"
        "  For 'in N minutes/hours/days' (relative) — ALWAYS use delay_seconds, NEVER cron:\n"
        "    'in 4 minutes' → delay_seconds=240\n"
        "    'in 30 min'    → delay_seconds=1800\n"
        "    'in 2 hours'   → delay_seconds=7200\n"
        "    'in 1 day'     → delay_seconds=86400\n"
        "  For specific clock times or recurring — use schedule (5-field cron):\n"
        "    '0 8 * * *'    daily at 8am\n"
        "    '0 17 * * 2'   every Tuesday at 5pm\n"
        "    '0 9 1 * *'    1st of each month at 9am\n"
        "  In prompt, ALWAYS include 'call send_message(\"...\")' so the reminder pings Telegram.\n"
        "  Example: create_routine(prompt='Call send_message(\"check the toast\") to remind me.', delay_seconds=240)"
    ),
    "list_routines":  "- list_routines(): show all scheduled routines and reminders.",
    "delete_routine": "- delete_routine(id): remove a routine by id. Use list_routines first to find ids.",
}


# ── Merge MCP tools into router catalogs ──────────────────────────────────────
# tools.py already extends TOOL_SCHEMAS with cached MCP schemas at import time.
# The adaptive router builds its catalog from TOOL_ONE_LINERS / _TOOL_DETAILS,
# which are dict literals above — they don't auto-grow. Mirror the MCP entries
# here so select_tools can offer them to the LLM router. No-op if no MCP tools
# are cached.
try:
    for _schema in TOOL_SCHEMAS:
        _fn = _schema.get("function", {}) if isinstance(_schema, dict) else {}
        _name = _fn.get("name", "")
        if _name.startswith("mcp__") and _name not in TOOL_ONE_LINERS:
            _desc = (_fn.get("description") or "").strip()
            # The router uses ONE_LINERS as a short catalog and _TOOL_DETAILS
            # for the per-tool prompt addendum. Keep both populated so the
            # model has enough context to pick AND use these tools.
            TOOL_ONE_LINERS[_name] = (_desc[:160] or f"MCP tool {_name}")
            _TOOL_DETAILS[_name] = f"- {_name}: {_desc[:400]}" if _desc else f"- {_name}: MCP-provided tool"
except Exception as _mcp_merge_e:
    print(f"[mcp] router catalog merge failed: {_mcp_merge_e}", file=sys.stderr)


_TZ_NAME_GL = os.environ.get("GERTY_TZ", "Europe/Budapest")
try:
    from zoneinfo import ZoneInfo as _ZI
    _TZ_GL = _ZI(_TZ_NAME_GL)
except Exception:
    _TZ_GL = None


def _now_block() -> str:
    """Always-injected 'right now' awareness — date, weekday, time, timezone.
    Prevents the bot from saying it doesn't know the date/time."""
    from datetime import datetime, timedelta
    now = datetime.now(_TZ_GL) if _TZ_GL is not None else datetime.now()
    today = now.date()
    return (
        f"\n\nRIGHT NOW: {now.strftime('%A')}, {today.isoformat()} at "
        f"{now.strftime('%H:%M')} ({_TZ_NAME_GL if _TZ_GL else 'system local'}). "
        f"Tomorrow is {(today + timedelta(days=1)).isoformat()}. "
        f"Yesterday was {(today - timedelta(days=1)).isoformat()}. "
        "Always trust this 'now' — never say you don't know the date or time."
    )


def _tool_addendum_for(selected: list[str]) -> str:
    """Build a minimal addendum describing only the selected tools."""
    from datetime import date
    if not selected:
        return ""
    lines = [_TOOL_DETAILS[name] for name in selected if name in _TOOL_DETAILS]
    if not lines:
        return ""
    today_iso = date.today().isoformat()
    body = "\n".join(lines).replace("{today}", today_iso)
    names = ", ".join(selected)
    always_on = ", ".join(ALWAYS_ON_TOOLS)
    return (
        f"\n\nToday's date: {today_iso}. "
        f"Tools available this turn: {names}.\n{body}\n"
        f"Note: {always_on} are always available — use them whenever the user "
        "mentions notes/save/log/progress, or to verify a previous write. "
        "Other tools should only be used when the request clearly calls for them. "
        "One tool at a time. "
        "After a tool call, DO NOT claim something was done if the tool returned an error — "
        "say what went wrong. If the user asks whether a save worked, verify with read_file "
        "instead of guessing."
    )


def select_tools(api: str, model: str, user_text: str,
                 prev_user: str = "", prev_assistant: str = "") -> list[str]:
    """Router: pick relevant tools for this user message.

    Now context-aware: also takes the previous user message and the previous
    assistant reply. That lets the router resolve short follow-ups like "do it"
    or "fix it" against what was just being discussed.

    Fast, low-token LLM call. Returns a list of tool names. On any error/timeout/
    empty, returns [] (no tools — keeps context small).
    """
    text = (user_text or "").strip()
    if not text:
        return []
    catalog = "\n".join(f"- {n}: {d}" for n, d in TOOL_ONE_LINERS.items())
    context_block = ""
    if prev_user or prev_assistant:
        context_block = (
            "Recent conversation context (so you can resolve short follow-ups "
            "like 'do it' or 'fix it'):\n"
            f"  prev user: {(prev_user or '')[:200]}\n"
            f"  prev assistant: {(prev_assistant or '')[:200]}\n\n"
        )
    prompt = (
        "Pick the tools (if any) this user message needs. Reply ONLY with a "
        "comma-separated list of tool names from the catalog, or the word 'none'.\n\n"
        f"Catalog:\n{catalog}\n\n"
        f"{context_block}"
        f"Current message: {text[:400]}\n\n"
        "Examples:\n"
        "  'search for X' → web_search,web_fetch\n"
        "  'check what's on example.com' → web_fetch,browser_open\n"
        "  'what's the news on jw.org' → web_fetch,browser_open\n"
        "  'screenshot example.com' → take_screenshot,send_image\n"
        "  'read today's note' → read_file\n"
        "  'do it' (after asst said 'I'll save to your note') → read_file,write_file\n"
        "  'fix it' (after asst wrote a note) → read_file,write_file\n"
        "  'how are you' → none\n\n"
        "Rules:\n"
        "- Any message mentioning a URL or domain → web_fetch, browser_open.\n"
        "- Short follow-ups ('do it', 'go ahead', 'fix it', 'now', 'again', "
        "'ok', 'yes do it') → infer tools from prev assistant message. If "
        "prev assistant talked about saving/writing/updating notes/vault, "
        "include read_file AND write_file. If it talked about searching, "
        "include web_search.\n\n"
        "Your answer:"
    )
    picked: list[str] = []
    # Skip the LLM router call for very short messages — they almost never have
    # enough signal for the LLM to route correctly, and the heuristics below
    # (URL / keyword / follow-up) catch the important cases. Saves a round-trip.
    if AUX_LLM_ENABLED and len(text) >= 3:
        # Semantic LLM router. Off by default (see AUX_LLM_ENABLED comment) —
        # the keyword heuristics below + ALWAYS_ON_TOOLS cover the common cases
        # without any LLM round-trip.
        payload = {
            "model": "qwen3-0.6b",
            "messages": [{"role": "user", "content": prompt + "\n\n/no_think"}],
            "temperature": 0.0,
            "max_tokens": 64,
            "thinking": {"type": "disabled"},
        }
        result = call_llm(api, payload, timeout=20)
        if result:
            low = result.strip().lower()
            if not ("none" in low and "," not in low):
                for token in re.split(r"[,\s]+", low):
                    token = token.strip(" .;:`'\"")
                    if token in TOOL_ONE_LINERS and token not in picked:
                        picked.append(token)

    # Safety-net heuristics — router LLM is fast but unreliable. These force
    # the obvious tools when keywords are present, even if the LLM said none.
    low_text = text.lower()
    url_like = re.search(
        r'\bhttps?://\S+|\b[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|app|dev|ai|co|us|uk|de|fr|ru|ua|info|me|so|xyz|tech|cloud|store)\b',
        low_text,
    )
    if url_like:
        for t in ("web_fetch", "browser_open"):
            if t not in picked:
                picked.append(t)
    if any(kw in low_text for kw in ("screenshot", "скріншот", "зніми ", "сфоткай")):
        for t in ("take_screenshot", "send_image"):
            if t not in picked:
                picked.append(t)
    # Investigation / lookup intent. Catches both explicit ("search for X")
    # and indirect framing ("go find out", "check what's...", "дізнайся",
    # "перевір" ) — the indirect framing was missing and caused the model
    # to *promise* a search and then do nothing because the tool wasn't on
    # its menu. Expanded UA coverage especially.
    research_kw = (
        # English explicit
        "search", "google", "find ", "look up", "lookup", "research ",
        "investigate", "browse for",
        # English indirect ("you should X for me")
        "find out", "check what", "check who", "check if", "check whether",
        "go and find", "go find", "go check", "go look", "look into",
        "dig into", "see what's", "tell me what's", "what's the latest",
        "what's happening with", "what does the internet",
        # Ukrainian explicit
        "знайди", "пошук", "погугли", "загугли", "знайти",
        # Ukrainian indirect
        "дізнайся", "дізнатись", "дізнатися", "перевір", "перевірити",
        "перевір що", "подивись", "глянь", "глянь що", "розкажи мені про",
        "пошукай", "пошукати", "пошукай в", "пошукай інформ",
        "знайди інформ", "що там з", "що нового",
    )
    if any(kw in low_text for kw in research_kw):
        for t in ("web_search", "web_fetch", "browser_open"):
            if t not in picked:
                picked.append(t)
    # Routine / reminder management — when user wants to schedule, remind,
    # or list/cancel existing routines.
    routine_kw = (
        "remind me", "reminder", "schedule", "every day", "every morning",
        "every monday", "every tuesday", "every wednesday", "every thursday",
        "every friday", "every saturday", "every sunday", "every week",
        "each day", "each morning", "daily", "weekly", "tomorrow at",
        "at 8am", "at 9am", "at 10am", "at 7pm", "at 8pm", "at noon",
        "every hour", "every 30 min", "at midnight",
        "routine", "list routines", "cancel routine", "delete routine",
        "нагадай", "нагадування", "щодня", "щоранку", "розклад",
    )
    if any(kw in low_text for kw in routine_kw):
        for t in ("create_routine", "list_routines", "delete_routine"):
            if t not in picked:
                picked.append(t)
    # "read me this / speak this aloud / voice it / прочитай / озвуч" →
    # user wants TTS read-aloud of EXISTING content, not a text reply. Force read_aloud.
    read_aloud_kw = (
        "read me ", "read this aloud", "read aloud", "read it aloud",
        "speak this", "speak it aloud", "voice this", "voice it",
        "say it aloud", "tts this", "read out loud",
        "прочитай", "озвуч", "озвучити", "прочитати вголос",
        "скажи вголос", "озвуч мені",
    )
    if any(kw in low_text for kw in read_aloud_kw):
        if "read_aloud" not in picked:
            picked.append("read_aloud")

    # "send voice / send audio / голосом / аудіо" → user wants the bot to
    # GENERATE a voice reply (compose text + TTS). Force `speak`. Distinct
    # from read_aloud, which reads back EXISTING text/URL/file.
    speak_kw = (
        # explicit "voice" / "audio" mentions
        "voice message", "voice msg", "voice reply", "send voice", "send a voice",
        "send audio", "send the audio", "audio message", "audio reply", "as audio",
        "in audio", "answer with voice", "reply with voice", "in voice", "as voice",
        "as a voice", "say it aloud", "say it", "say this", "tell me out loud",
        "tell it", "speak the", "speak it",
        # Ukrainian / Russian — "аудіо" alone is the strongest cue
        "аудіо", "audio", "голосове повідомлення", "голосове", "голосовим", "голосом",
        "відправ голос", "надішли голос", "розкажи голосом", "скажи голосом",
        "відповідь голосом", "озвуч", "наговори", "наспівай",
        # "скинь аудіо / запиши голос" — common Ukrainian asks
        "скинь аудіо", "скинь голос", "запиши голос", "запиши аудіо",
        # slang + bare verbs the user actually uses
        "голосовуха", "голосовуху", "голосовухой", "голосовою",
        "запиши", "скинь",
    )
    if any(kw in low_text for kw in speak_kw):
        if "speak" not in picked:
            picked.append("speak")

    # Memory ops — when the user asks to remember, recall, or "what do you know"
    # about them, force the memory tools onto the menu so the model doesn't
    # answer from imagination.
    memory_save_kw = (
        "remember this", "remember that", "don't forget", "do not forget",
        "keep this in mind", "save this for later", "запам'ятай", "запамятай",
        "не забудь",
    )
    memory_recall_kw = (
        "do you remember", "what do you remember", "what do you know about me",
        "what did i tell you", "you should know", "you know my",
        "ти пам'ятаєш", "що ти пам'ятаєш", "що ти знаєш про мене",
    )
    if any(kw in low_text for kw in memory_save_kw):
        for t in ("save_memory", "list_memory"):
            if t not in picked:
                picked.append(t)
    if any(kw in low_text for kw in memory_recall_kw):
        for t in ("list_memory", "search_memory", "recall_memory"):
            if t not in picked:
                picked.append(t)

    # Explicit "forget" / "delete memory" — only path that exposes delete_memory
    # to the model. Without this gate the tool is invisible, so the model
    # physically cannot delete on a misread.
    memory_forget_kw = (
        "forget my", "forget that", "forget the", "forget this",
        "delete that memory", "delete this memory", "delete the memory",
        "remove that memory", "remove this memory", "remove the memory",
        "забудь", "видали з памят", "видали з пам'ят", "видали цю памят",
        "видали цю пам'ят",
    )
    if any(kw in low_text for kw in memory_forget_kw):
        for t in ("list_memory", "search_memory", "delete_memory"):
            if t not in picked:
                picked.append(t)

    # Dream routine — sleeping-hours pass that scans chat history and structures
    # memory. Triggered by "dream", "scan chat", "consolidate memory", etc.
    dream_kw = (
        "dream", "consolidate memor", "structure memor", "scan chat",
        "scan recent", "recent chat", "chat history", "review conversation",
    )
    if any(kw in low_text for kw in dream_kw):
        for t in ("recent_chat", "list_memory", "search_memory", "save_memory"):
            if t not in picked:
                picked.append(t)

    # "did it save / is it there / did you actually" → user is verifying a prior
    # action. Force read_file so the bot can check the vault instead of guessing.
    verify_kw = (
        "did it save", "did it work", "did you save", "did you write",
        "is it there", "did it show", "did you really", "still missing",
        "did it actually", "did you actually", "не зберіг", "не записав",
        "не з'явилось", "не з'явилося", "не показ",
    )
    if any(kw in low_text for kw in verify_kw):
        if "read_file" not in picked:
            picked.append("read_file")

    # Short follow-ups like "do it", "fix it", "ok", "go ahead", "now" —
    # combined with a previous assistant message that talked about saving/
    # writing — force-include the file tools. This catches the case where the
    # router's LLM call misses the implied intent.
    short_followup_re = re.compile(
        r'^\s*(ok|okay|yes|yep|sure|do it|go ahead|now|again|fix it|fix this|'
        r'try again|try it|давай|зроби|спробуй ще|зроби це)\b',
        re.IGNORECASE,
    )
    if short_followup_re.match(low_text) and prev_assistant:
        prev_low = prev_assistant.lower()
        if any(kw in prev_low for kw in (
            "save", "write", "wrote", "note", "vault", "obsidian", "progress",
            "збережу", "записав", "записати", "збереж", "нотатк",
        )):
            for t in ("read_file", "write_file"):
                if t not in picked:
                    picked.append(t)

    # ALWAYS-ON tools — appended last so duplicates are no-ops. These are the
    # tools the user wants available on every turn (notes/vault access).
    for t in ALWAYS_ON_TOOLS:
        if t not in picked:
            picked.append(t)
    return picked

# Telegram-supported reaction emojis (ordered by usefulness)
REACTION_EMOJIS = [
    "🔥","🎉","👍","❤️","😁","🤔","🤯","😱","🥰","👏","🤩","😢","💯","🤣",
    "⚡","🏆","😎","🤝","✍","🤗","🫡","👨‍💻","😇","😐","💔","😈","😴","😭",
    "🤓","👻","👀","🎃","🤪","🗿","🆒","🦄","😘","💊","👾","🤷","😡","👎",
    "💩","🙏","👌","🤡","🥱","😍","🐳","💋","😨","🎅","💅","💘",
]


# Keyword → emoji rules for the rule-based reaction picker. Each (regex, emoji)
# is tried in order; first match wins. Only fires on strong signals so most
# messages get no reaction — same conservative behavior the LLM picker had.
_REACTION_RULES: list[tuple[str, str]] = [
    # User already used a strong emoji in their message — mirror the vibe
    (r'🔥',                                                                  '🔥'),
    (r'[❤️🥰😘💋💘]|<3',                                                      '❤️'),
    (r'[😂🤣]',                                                              '🤣'),
    (r'[🎉🎊🥳]',                                                            '🎉'),
    (r'[🤯😱]',                                                              '🤯'),
    (r'[😢😭]',                                                              '😢'),
    # English keywords
    (r'\b(i love (it|you|this)|love it|loved it|loving|adore)\b',           '❤️'),
    (r'\b(lol|lmao|haha+|hilarious|so funny|laughing|cracking up)\b',       '🤣'),
    (r'\b(fire|amazing|incredible|epic|legendary|insane)\b',                '🔥'),
    (r'\b(ship(ped|ping)?|done|finished|congrats|congratulations|nailed)\b', '🎉'),
    (r'\b(crying|so sad|miserable|heartbroken|devastat)',                   '😢'),
    (r'\b(wtf|omg|holy ?sh|whoa|no way|mind blown)\b',                      '🤯'),
    (r'\b(thank(s|\s+you)?|appreciate|grateful)\b',                         '🤝'),
    # Ukrainian / Russian
    (r'(люблю|обожнюю|кохаю|кохана|любий|сердечк)',                          '❤️'),
    (r'(круто|вогонь|топ\b|клас|прикольно)',                                 '🔥'),
    (r'(смішно|сміюся|ахах|ха-ха|ор[оу])',                                   '🤣'),
    (r'(сум(но|ую)|плачу|жал[ьі]ко|дуже сумно)',                             '😢'),
    (r'(дяк|спасиб|велике дякую|вдячн)',                                     '🤝'),
    (r'(\bого\b|нічого собі|капец|нереально|жесть)',                         '🤯'),
]


def pick_reaction(api: str = "", model: str = "", text: str = "") -> str | None:
    """Rule-based reaction emoji picker — no LLM call, no VRAM cost.

    Returns one emoji from REACTION_EMOJIS when the message has a strong
    emotional / celebratory / acknowledgment signal, else None (no reaction).
    Keeps the api/model parameters so callers don't need to change.
    """
    if not text:
        return None
    low = text.lower()
    for pattern, emoji in _REACTION_RULES:
        if re.search(pattern, low):
            if emoji in REACTION_EMOJIS:
                return emoji
    return None

# History compaction: when history exceeds this many messages, summarize the oldest
# portion into a short paragraph, keeping only COMPACT_KEEP_RECENT messages verbatim.
COMPACT_THRESHOLD = 50   # trigger when history >= this many messages
COMPACT_KEEP_RECENT = 20 # keep this many recent messages uncompressed


def tg_send(token: str, chat_id: int, text: str):
    """Send a Telegram message. Splits on |||, renders markdown as HTML.

    If Telegram rejects the HTML (malformed conversion edge case), retries
    once as plain text so messages never silently fail."""
    from tools import markdown_to_telegram_html
    parts = [p.strip() for p in text.split("|||") if p.strip()]
    if not parts:
        parts = [text.strip()]
    for i, part in enumerate(parts):
        html = markdown_to_telegram_html(part)
        for attempt_html in (True, False):
            payload = {"chat_id": chat_id, "text": html if attempt_html else part}
            if attempt_html:
                payload["parse_mode"] = "HTML"
                payload["disable_web_page_preview"] = True
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                break  # success
            except urllib.error.HTTPError as e:
                if attempt_html and e.code == 400:
                    # Bad HTML — fall through to plain-text retry
                    body = e.read().decode("utf-8", errors="replace")[:160] if e.fp else ""
                    print(f"[tg_send] HTML rejected ({body}); retrying plain", file=sys.stderr)
                    continue
                print(f"[tg_send] error: {e}", file=sys.stderr)
                break
            except Exception as e:
                print(f"[tg_send] error: {e}", file=sys.stderr)
                break
        if i < len(parts) - 1:
            time.sleep(0.3)


def load_history(path: str) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert isinstance(data, list)
        return data
    except Exception:
        return []


def save_history(path: str, user_text: str, reply: str, max_items: int,
                 api: str = "", model: str = ""):
    history = load_history(path)
    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": reply})
    # Compact before hard-truncating so old context is preserved as a summary
    if api and model:
        history = compact_history(history, api, model)
    history = history[-max_items:]
    tmp = path + ".tmp"
    Path(tmp).write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def compact_history(history: list, api: str, model: str) -> list:
    """
    When history is long, summarize the oldest messages into a single synthetic
    exchange and prepend it to the kept recent messages.

    Returns the (possibly compacted) history list.
    """
    if len(history) < COMPACT_THRESHOLD:
        return history

    to_summarize = history[:-COMPACT_KEEP_RECENT]
    to_keep      = history[-COMPACT_KEEP_RECENT:]

    # Only summarize turns with plain-text content (skip image content blocks)
    lines = []
    for m in to_summarize:
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"{m['role'].upper()}: {content}")

    if not lines:
        return to_keep

    convo_text = "\n".join(lines)
    summary_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a conversation summarizer. "
                    "Summarize the following conversation into 2-5 compact sentences, "
                    "preserving every important fact, decision, name, and context detail. "
                    "Output only the summary — no preamble, no commentary."
                ),
            },
            {"role": "user", "content": convo_text},
        ],
        "temperature": 0.3,
        "max_tokens": 300,
        "thinking": {"type": "disabled"},
    }

    summary = call_llm(api, summary_payload, timeout=60)
    if not summary:
        # Summarizer failed — fall back to simple truncation
        print("[compact] summary call failed, truncating oldest messages", file=sys.stderr)
        return to_keep

    print(
        f"[compact] compressed {len(to_summarize)} messages → {len(summary)} chars",
        file=sys.stderr,
    )

    summary_pair = [
        {"role": "user",      "content": f"[Summary of our earlier conversation: {summary}]"},
        {"role": "assistant", "content": "Got it — I have that context."},
    ]
    return summary_pair + to_keep


_USAGE_FILE = _repo_root() / ".usage-stats.json"


def _record_usage(usage: dict, model: str) -> None:
    """Append latest LLM usage to a stats file the context-bar daemon reads.

    Tracks BOTH last_prompt_tokens (most recent call) and peak_prompt_tokens
    (high-watermark since last reset). The bar displays peak, since "context
    fullness" should reflect the largest single prompt the model has seen —
    a small reaction/router call shouldn't make the bar drop.

    Cheap and non-blocking — failures are silent.
    """
    if not usage:
        return
    try:
        prev = {}
        if _USAGE_FILE.exists():
            try:
                prev = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total = prompt_tokens + completion_tokens
        prev_peak = int(prev.get("peak_prompt_tokens", 0) or 0)
        new_data = {
            "model": model or prev.get("model"),
            "last_prompt_tokens": prompt_tokens,
            "last_completion_tokens": completion_tokens,
            "last_total_tokens": total,
            "peak_prompt_tokens": max(prev_peak, prompt_tokens),
            "cumulative_input": int(prev.get("cumulative_input", 0)) + prompt_tokens,
            "cumulative_output": int(prev.get("cumulative_output", 0)) + completion_tokens,
            "call_count": int(prev.get("call_count", 0)) + 1,
            "last_update": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # Atomic write
        tmp = _USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
        os.replace(tmp, _USAGE_FILE)
    except Exception:
        pass  # never let usage tracking break the call


def _reset_usage_stats() -> None:
    """Wipe the peak/cumulative stats — called by /new to start fresh."""
    try:
        if _USAGE_FILE.exists():
            _USAGE_FILE.unlink()
    except Exception:
        pass


def _post_llm(api: str, payload: dict, timeout: int = 120) -> dict | None:
    """Raw POST to /v1/chat/completions, returns parsed JSON dict."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
            # Capture usage for the context-bar daemon to consume
            _record_usage(result.get("usage", {}), payload.get("model", ""))
            return result
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        print(f"[llm] error: HTTP Error {e.code}: {e.reason} | body: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[llm] error: {e}", file=sys.stderr)
        return None


def call_llm(api: str, payload: dict, timeout: int = 120) -> str | None:
    """Simple single-turn call — returns text content or None."""
    d = _post_llm(api, payload, timeout)
    if not d:
        return None
    msg = d["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    return text or None


# Human-friendly progress labels for tool use. Tools omitted from this map
# are "silent" (already user-visible: send_message, send_reaction, send_sticker,
# send_image, speak, read_aloud).
_TOOL_PROGRESS = {
    "web_search":      "🔍 searching the web…",
    "web_fetch":       "🌐 fetching the page…",
    "browser_open":    "🌐 opening in stealth browser…",
    "read_file":       "📖 reading your notes…",
    "list_folder":     "📂 browsing folders…",
    "write_file":      "✍️ updating your notes…",
    "run_shell":       "💻 running a command…",
    "save_memory":     "🧠 saving to memory…",
    "recall_memory":   "🧠 recalling that memory…",
    "search_memory":   "🔎 searching memory…",
    "list_memory":     "🧠 checking what I remember…",
    "delete_memory":   "🧠 forgetting that…",
    "recent_chat":     "📜 reviewing our recent chat…",
    "take_screenshot": "📸 taking a screenshot…",
    "create_routine":  "⏰ scheduling that…",
    "list_routines":   "⏰ checking your routines…",
    "delete_routine":  "⏰ removing that routine…",
}


def _progress_send_or_edit(state: dict, tool_name: str) -> None:
    """Send a 'working on X' status to Telegram, editing the same message as
    later tools fire. state holds the message_id across the agent loop."""
    label = _TOOL_PROGRESS.get(tool_name)
    if not label:
        return  # silent tool — no status needed
    from tools import _CTX
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return
    try:
        if state.get("msg_id"):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/editMessageText",
                data=json.dumps({
                    "chat_id": chat_id, "message_id": state["msg_id"],
                    "text": label,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        else:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({
                    "chat_id": chat_id, "text": label,
                    "disable_notification": True,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                state["msg_id"] = json.loads(r.read())["result"]["message_id"]
    except Exception as e:
        print(f"[progress] error: {e}", file=sys.stderr)


def _progress_delete(state: dict) -> None:
    """Remove the progress message so the final reply is clean."""
    mid = state.get("msg_id")
    if not mid:
        return
    from tools import _CTX
    chat_id = _CTX.get("chat_id")
    token   = _CTX.get("tg_token")
    if not chat_id or not token:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            data=json.dumps({"chat_id": chat_id, "message_id": mid}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    state["msg_id"] = None


def run_agent(api: str, model: str, messages: list,
              temperature: float = 0.7, max_tokens: int = 4096,
              tools_subset: list[dict] | None = None,
              show_progress: bool = True,
              ttl: int | None = None) -> str | None:
    """
    Agentic loop with tool use.

    tools_subset: list of tool schema dicts to expose this turn. If None, exposes
    all of TOOL_SCHEMAS (back-compat). Pass [] to disable tools entirely for this call.

    Tries native OpenAI function-calling first. If the model responds with
    plain text containing TOOL_CALL: {...} or gemma's <|tool_call|> markers,
    those are parsed as fallbacks. Loops until the model produces a final
    text answer or max turns is hit.
    """
    active_tools = TOOL_SCHEMAS if tools_subset is None else tools_subset
    tools_available = bool(TOOLS_ENABLED and active_tools)

    tm = thinking_mode()
    # Thinking tokens count against max_tokens in LM Studio — boost budget further when enabled
    effective_max = 8192 if tm.get("type") == "enabled" else max_tokens
    base = {
        "model": model,
        "temperature": temperature,
        "max_tokens": effective_max,
        "thinking": tm,
    }
    if ttl is not None:
        # LM Studio: tells the server to auto-unload this JIT-loaded model after
        # `ttl` seconds of idle. Used for the on-demand vision model so it
        # doesn't permanently squat VRAM next to the main LLM.
        base["ttl"] = ttl
    msgs = list(messages)
    last_content: str | None = None
    loop_start = time.time()
    progress: dict = {"msg_id": None}  # progress-message state

    def _ret(value):
        if show_progress:
            _progress_delete(progress)
        return value

    for turn in range(AGENT_MAX_TURNS):
        if time.time() - loop_start > AGENT_MAX_SECONDS:
            print(f"[agent] wall-clock limit hit ({AGENT_MAX_SECONDS}s) after "
                  f"turn {turn} — returning last content", file=sys.stderr)
            return _ret(last_content)
        # Try with tools when available. If it fails (timeout, 400, model
        # still loading), retry the SAME tools-payload after a short pause.
        # Falling straight back to a no-tools call gives gemma rope to emit a
        # text-format tool call which we then have to parse.
        raw = None
        if tools_available:
            raw = _post_llm(api, {**base, "messages": msgs,
                                   "tools": active_tools, "tool_choice": "auto"})
            if not raw:
                time.sleep(2)
                raw = _post_llm(api, {**base, "messages": msgs,
                                       "tools": active_tools, "tool_choice": "auto"},
                                timeout=180)
        if not raw:
            # No tools available, or with-tools path failed — plain call.
            # Model may still emit a text-format tool call (gemma's native
            # <|tool_call|> chat-template tokens). The parsers below catch it.
            raw = _post_llm(api, {**base, "messages": msgs})
        if not raw:
            return _ret(last_content)  # use whatever we had

        choice = raw["choices"][0]
        msg    = choice["message"]
        content = (msg.get("content") or "").strip()
        last_content = content

        # ── Native tool calls ────────────────────────────────────────────────
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            # Append assistant message (may include partial content/thinking)
            msgs.append(msg)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except Exception:
                    args = {}
                print(f"[agent] tool={name} args={args}", file=sys.stderr)
                if show_progress:
                    _progress_send_or_edit(progress, name)
                result = execute_tool(name, args)
                print(f"[agent] result: {result[:120]}", file=sys.stderr)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue  # next turn

        # ── Text-based TOOL_CALL fallback (for models without native calling) ─
        tc_match = re.search(r'TOOL_CALL:\s*(\{[^}]+\})', content, re.DOTALL)
        if tc_match:
            try:
                tc_data = json.loads(tc_match.group(1))
                name = tc_data.get("tool", "")
                args = tc_data.get("args", {})
                print(f"[agent-text] tool={name} args={args}", file=sys.stderr)
                if show_progress:
                    _progress_send_or_edit(progress, name)
                result = execute_tool(name, args)
                print(f"[agent-text] result: {result[:120]}", file=sys.stderr)
                msgs.append({"role": "assistant", "content": content})
                msgs.append({"role": "user", "content": f"Tool result:\n{result}"})
                continue
            except Exception as e:
                print(f"[agent-text] parse error: {e}", file=sys.stderr)

        # ── Gemma special-token tool-call format ──────────────────────────────
        # When LM Studio rejects the tools-payload (e.g. 400 / Model unloaded)
        # and we fall back to a plain call, gemma still tries to call a tool by
        # emitting one of these as plain text content:
        #   <|tool_call>call:NAME(key="val", ...)<tool_call|>   -- python style
        #   <|tool_call>call:NAME{key:"val", ...}<tool_call|>   -- object style
        gemma_tc = re.search(
            r'<\|tool_call\|?>\s*call:\s*([A-Za-z_][A-Za-z0-9_]*)\s*'
            r'[(\{](.*?)[)\}]\s*<\|?tool_call\|?>',
            content,
            re.DOTALL,
        )
        if gemma_tc:
            name = gemma_tc.group(1)
            arg_str = gemma_tc.group(2)
            args: dict = {}
            # Match key=val, key:val, with optional double/single-quoted values
            for m in re.finditer(
                r'([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*'
                r'(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([^,()\{\}]+))',
                arg_str,
            ):
                key = m.group(1)
                val = m.group(2) if m.group(2) is not None else (
                    m.group(3) if m.group(3) is not None else (m.group(4) or "").strip()
                )
                args[key] = val
            print(f"[agent-gemma-text] tool={name} args={args}", file=sys.stderr)
            if show_progress:
                _progress_send_or_edit(progress, name)
            result = execute_tool(name, args)
            print(f"[agent-gemma-text] result: {result[:120]}", file=sys.stderr)
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": f"Tool result:\n{result}"})
            continue

        # ── Final answer ─────────────────────────────────────────────────────
        return _ret(content or None)

    print(f"[agent] turn-count limit hit ({AGENT_MAX_TURNS}) — returning last content",
          file=sys.stderr)
    return _ret(last_content)


def detect_lang(text: str) -> str:
    """Return 'uk' if majority of alpha chars are Cyrillic, else 'en'."""
    import unicodedata
    cyrillic = sum(1 for c in text if "CYRILLIC" in unicodedata.name(c, ""))
    latin = sum(1 for c in text if c.isalpha() and ord(c) < 256)
    return "uk" if cyrillic > latin else "en"


def main():
    if len(sys.argv) < 9:
        print("usage: gemma_chat.py <mode> <chat_id> <history_path> <system_file> "
              "<llm_api> <model> <max_history> <tg_token> [message_id] [image_path]", file=sys.stderr)
        sys.exit(2)

    mode         = sys.argv[1]          # "text" or "image"
    chat_id      = int(sys.argv[2])
    history_path = sys.argv[3]
    system_file  = sys.argv[4]
    llm_api      = sys.argv[5]
    model        = sys.argv[6]
    max_history  = int(sys.argv[7])
    tg_token     = sys.argv[8]
    message_id   = int(sys.argv[9]) if len(sys.argv) > 9 and sys.argv[9].isdigit() else 0

    # Hard allowlist — read from config/.allowed-chats (one chat_id per line)
    allowed_file = Path(__file__).parent.parent / "config" / ".allowed-chats"
    try:
        allowed = {int(l.strip()) for l in allowed_file.read_text().splitlines() if l.strip().isdigit()}
    except Exception:
        allowed = {165548659}
    if chat_id not in allowed:
        sys.exit(0)
    # voice_reply: "1" means auto-speak the reply (set when user sent a voice message)
    # text mode:  argv[10] = voice_reply
    # image mode: argv[10] = image_path, argv[11] = voice_reply
    if mode == "image":
        voice_reply = (sys.argv[11] == "1") if len(sys.argv) > 11 else False
    else:
        voice_reply = (sys.argv[10] == "1") if len(sys.argv) > 10 else False

    # Inject context so tools (send_reaction, speak, send_image) can reach Telegram
    set_context(chat_id, message_id, tg_token)

    system = Path(system_file).read_text(encoding="utf-8").strip()
    # Always inject current date/weekday/time so the bot never has to guess.
    system = system + _now_block()
    user_input = sys.stdin.buffer.read().decode("utf-8")

    # ── TEXT mode ────────────────────────────────────────────────────────────
    if mode == "text":
        user_text = user_input

        # The listener prepends '[replying to: "<context>"]\n' when the user
        # replies to another message. Slash-command detection and the clone
        # keyboard follow-up need to look past that prefix to match against
        # the user's actual text, but the LLM downstream MUST see the full
        # prefix so it knows what the user is referring to.
        # Two-variable split: user_text keeps the full input (for the LLM
        # messages payload + history), cmd_text is the stripped version used
        # only for command/keyboard matching and reaction picking.
        cmd_text = user_text
        _stripped = user_text.lstrip()
        if _stripped.startswith('[replying to:'):
            _m = re.match(r'\[replying to: ".*?"\]\n', _stripped, re.DOTALL)
            if _m:
                cmd_text = _stripped[_m.end():]

        # ── Clone keyboard-pick follow-up ────────────────────────────────────
        # The clone flow is two keyboard prompts: language, then gender. The
        # pending file ends in one of three states for a given chat:
        #   awaiting_lang=True   → next message should pick EN / UK / Both
        #   awaiting_gender=True → next message should pick Male / Female
        #   neither              → lang+gender set, audio capture window open
        # Both keyboard handlers live here so they short-circuit before the
        # slash-command / LLM routing below.
        try:
            _pending_path = _repo_root() / ".clone-pending.json"
            if _pending_path.exists():
                _p = json.loads(_pending_path.read_text(encoding="utf-8"))
                _now = int(time.time())
                _expired = _now > int(_p.get("expires_at", 0))
                _chat_match = str(_p.get("chat_id")) == str(chat_id)
                if _expired and _chat_match:
                    # Stale window — clean up and let the message fall through.
                    try: _pending_path.unlink()
                    except Exception: pass
                elif _chat_match and _p.get("awaiting_lang"):
                    _t = cmd_text.strip().lower()
                    _picked = None
                    if "english" in _t or _t == "en" or "🇬🇧" in _t:
                        _picked = "en"
                    elif "ukrainian" in _t or _t == "uk" or "🇺🇦" in _t or "укр" in _t:
                        _picked = "uk"
                    elif "both" in _t or "🌐" in _t or _t == "both":
                        _picked = "both"
                    if _picked:
                        _p["lang"] = _picked
                        _p.pop("awaiting_lang", None)
                        # Always ask for gender next. Bot can't know it from the
                        # audio alone — the user has to tell us so Ukrainian
                        # past-tense / predicate adjectives match the voice.
                        _p["awaiting_gender"] = True
                        _p["expires_at"] = _now + 300
                        _pending_path.write_text(json.dumps(_p, indent=2), encoding="utf-8")
                        _kb = {
                            "keyboard": [[{"text": "👨 Male"}, {"text": "👩 Female"}]],
                            "one_time_keyboard": True,
                            "resize_keyboard": True,
                            "selective": True,
                        }
                        _payload = json.dumps({
                            "chat_id": chat_id,
                            "text": f"🧬 and the gender of '{_p['name']}'? "
                                    "(used for Ukrainian past-tense grammar)",
                            "reply_markup": _kb,
                        }).encode("utf-8")
                        _req = urllib.request.Request(
                            f"https://api.telegram.org/bot{tg_token}/sendMessage",
                            data=_payload,
                            headers={"Content-Type": "application/json"},
                        )
                        try: urllib.request.urlopen(_req, timeout=10)
                        except Exception as e: print(f"[clone] kb-reply lang: {e}", file=sys.stderr)
                        sys.exit(0)
                    # Otherwise: ignore and let the message fall through normally.
                    # We keep awaiting_lang on the pending so the user can still tap.
                elif _chat_match and _p.get("awaiting_gender"):
                    _t = cmd_text.strip().lower()
                    _picked = None
                    if "female" in _t or "жінк" in _t or "👩" in _t or _t in ("f", "fem"):
                        _picked = "feminine"
                    elif "male" in _t or "чолов" in _t or "👨" in _t or _t in ("m", "masc"):
                        _picked = "masculine"
                    if _picked:
                        _p["gender"] = _picked
                        _p.pop("awaiting_gender", None)
                        _p["expires_at"] = _now + 300
                        _pending_path.write_text(json.dumps(_p, indent=2), encoding="utf-8")
                        _lang_label = {"en": "English", "uk": "Ukrainian",
                                       "both": "both languages (EN first, then UK)"}\
                                       .get(_p.get("lang", "en"), _p.get("lang", "en"))
                        _msg = (
                            f"🎙 ok — '{_p['name']}' ({_lang_label}, {_picked}).\n"
                            "send a 5–15s voice message OR upload an audio file now."
                        )
                        _payload = json.dumps({
                            "chat_id": chat_id, "text": _msg,
                            "reply_markup": {"remove_keyboard": True},
                        }).encode("utf-8")
                        _req = urllib.request.Request(
                            f"https://api.telegram.org/bot{tg_token}/sendMessage",
                            data=_payload,
                            headers={"Content-Type": "application/json"},
                        )
                        try: urllib.request.urlopen(_req, timeout=10)
                        except Exception as e: print(f"[clone] kb-reply gender: {e}", file=sys.stderr)
                        sys.exit(0)
                    # Unrecognized — keep awaiting_gender, let user tap again.
        except Exception as _e:
            print(f"[clone] pending-precheck: {_e}", file=sys.stderr)

        # ── Slash commands (handled before LLM) ──────────────────────────────
        cmd = re.sub(r'@\w+$', '', cmd_text.strip()).strip().lower()
        if cmd == "/new":
            Path(history_path).write_text("[]", encoding="utf-8")
            _reset_usage_stats()
            tg_send(tg_token, chat_id, "fresh session")
            sys.exit(0)
        elif cmd == "/status":
            try:
                req = urllib.request.Request(f"{llm_api}/v1/models")
                with urllib.request.urlopen(req, timeout=5) as r:
                    model_id = json.loads(r.read())["data"][0]["id"]
            except Exception:
                model_id = "unknown"
            thinking_state = "off"
            try:
                thinking_state = THINKING_FLAG_FILE.read_text().strip()
            except Exception:
                pass
            tg_send(tg_token, chat_id, f"online — {model_id}\nthinking: {thinking_state}")
            sys.exit(0)
        elif cmd == "/thinking":
            try:
                current = THINKING_FLAG_FILE.read_text().strip()
            except Exception:
                current = "off"
            if current == "on":
                THINKING_FLAG_FILE.write_text("off")
                tg_send(tg_token, chat_id, "thinking off")
            else:
                THINKING_FLAG_FILE.write_text("on")
                tg_send(tg_token, chat_id, "thinking on")
            sys.exit(0)
        elif cmd_text.strip().lower().startswith("/goal"):
            # /goal              → show current goal (or "no active goal")
            # /goal <text>       → set/replace goal (survives /new resets)
            # /goal clear|done|drop → clear it
            raw = re.sub(r'^/goal(?:@\w+)?\b', '', cmd_text.strip(), count=1).strip()
            if not raw:
                g = load_goal(chat_id)
                if g and g.get("text"):
                    tg_send(tg_token, chat_id,
                        f"current goal: {g['text']}\n"
                        f"set at: {g.get('set_at', '?')}\n\n"
                        f"/goal <new text> — replace\n"
                        f"/goal clear — drop it")
                else:
                    tg_send(tg_token, chat_id,
                        "no active goal.\n\n"
                        "/goal <text> — set one. it stays in context across "
                        "every reply (and survives /new) until you /goal clear it.")
                sys.exit(0)
            if raw.lower() in ("clear", "done", "drop", "reset"):
                if clear_goal(chat_id):
                    tg_send(tg_token, chat_id, "goal cleared.")
                else:
                    tg_send(tg_token, chat_id, "no goal was set.")
                sys.exit(0)
            save_goal(chat_id, raw)
            tg_send(tg_token, chat_id,
                f"goal set: {raw}\n\n"
                f"i'll thread responses toward this until you /goal clear it.")
            sys.exit(0)
        elif cmd == "/help":
            tg_send(tg_token, chat_id,
                "/new — reset context\n"
                "/status — show model + thinking mode\n"
                "/thinking — toggle thinking mode on/off\n"
                "/goal [text|clear] — show/set/clear a sustained objective\n"
                "/voice — show/set TTS voice (test with samples)\n"
                "/read <url | vault/path | text> — TTS read-aloud (no LLM)\n"
                "/restart — kill and restart the listener\n"
                "/help — this list")
            sys.exit(0)
        elif cmd.startswith("/clone"):
            # /clone <name> [en|uk|both] [male|female]  →  arm a 5-min window to
            # capture the next voice msg / audio file as an OmniVoice reference.
            # Missing fields are collected via reply keyboards.
            import re as _re
            import time as _time
            raw = re.sub(r'^/clone\b', '', cmd_text.strip(), count=1).strip()
            parts = raw.split()
            name_raw = parts[0] if parts else ""
            lang_arg = (parts[1].lower() if len(parts) > 1 else "").strip()
            gender_arg = (parts[2].lower() if len(parts) > 2 else "").strip()
            # Accept several gender spellings on the CLI (the keyboard sends
            # 👨/👩 emoji + label; CLI users may type any of these).
            if gender_arg in ("m", "male", "masc", "masculine"):
                gender_arg = "masculine"
            elif gender_arg in ("f", "female", "fem", "feminine"):
                gender_arg = "feminine"
            else:
                gender_arg = ""
            name = _re.sub(r"[^a-z0-9-]+", "-", name_raw.lower()).strip("-")[:30]
            if not name:
                tg_send(tg_token, chat_id,
                    "usage: /clone <name> [en|uk|both] [male|female]\n"
                    "then send me a 5–15 second voice message OR upload an audio "
                    "file (mp3/m4a/wav/ogg). I'll clone it as omni_<name>.")
                sys.exit(0)

            pending = _repo_root() / ".clone-pending.json"
            if lang_arg in ("en", "uk", "both") and gender_arg:
                # Both fields explicit — arm and prompt for audio.
                try:
                    pending.write_text(
                        json.dumps({
                            "chat_id": chat_id,
                            "name": name,
                            "lang": lang_arg,
                            "gender": gender_arg,
                            "expires_at": int(_time.time()) + 300,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    lang_label = {"en": "English", "uk": "Ukrainian",
                                  "both": "both languages"}[lang_arg]
                    msg = (
                        f"🎙 ready to clone '{name}' ({lang_label}, {gender_arg}).\n"
                        f"send a 5–15s voice message OR upload an audio file now. "
                        f"window: 5 min."
                    )
                    # remove any open reply keyboard
                    payload = json.dumps({
                        "chat_id": chat_id, "text": msg,
                        "reply_markup": {"remove_keyboard": True},
                    }).encode("utf-8")
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{tg_token}/sendMessage",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    try: urllib.request.urlopen(req, timeout=10)
                    except Exception as e: print(f"[clone] tg_send: {e}", file=sys.stderr)
                except Exception as e:
                    tg_send(tg_token, chat_id, f"failed to arm clone: {e}")
                sys.exit(0)

            # CLI gave language but no gender → save lang, ask gender via keyboard.
            if lang_arg in ("en", "uk", "both") and not gender_arg:
                try:
                    pending.write_text(
                        json.dumps({
                            "chat_id": chat_id,
                            "name": name,
                            "lang": lang_arg,
                            "awaiting_gender": True,
                            "expires_at": int(_time.time()) + 300,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    kb = {
                        "keyboard": [[{"text": "👨 Male"}, {"text": "👩 Female"}]],
                        "one_time_keyboard": True,
                        "resize_keyboard": True,
                        "selective": True,
                    }
                    payload = json.dumps({
                        "chat_id": chat_id,
                        "text": f"🧬 gender of '{name}'? "
                                "(used for Ukrainian past-tense grammar)",
                        "reply_markup": kb,
                    }).encode("utf-8")
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{tg_token}/sendMessage",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    try: urllib.request.urlopen(req, timeout=10)
                    except Exception as e: print(f"[clone] tg_send: {e}", file=sys.stderr)
                except Exception as e:
                    tg_send(tg_token, chat_id, f"failed to arm clone: {e}")
                sys.exit(0)

            # No language — ask via reply keyboard. Pending stays in awaiting_lang state.
            # The lang handler above will chain into the gender keyboard automatically.
            try:
                pending.write_text(
                    json.dumps({
                        "chat_id": chat_id,
                        "name": name,
                        "awaiting_lang": True,
                        "expires_at": int(_time.time()) + 300,
                    }, indent=2),
                    encoding="utf-8",
                )
                kb = {
                    "keyboard": [
                        [{"text": "🇬🇧 English"}, {"text": "🇺🇦 Ukrainian"}],
                        [{"text": "🌐 Both (EN + UK)"}],
                    ],
                    "one_time_keyboard": True,
                    "resize_keyboard": True,
                    "selective": True,
                }
                payload = json.dumps({
                    "chat_id": chat_id,
                    "text": f"🎙 cloning '{name}' — pick a language for the sample:",
                    "reply_markup": kb,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                try: urllib.request.urlopen(req, timeout=10)
                except Exception as e: print(f"[clone] tg_send: {e}", file=sys.stderr)
            except Exception as e:
                tg_send(tg_token, chat_id, f"failed to arm clone: {e}")
            sys.exit(0)
        elif cmd.startswith("/voice"):
            # /voice                        → show current + brief list
            # /voice list [filter]          → full list (optionally filtered, e.g. 'british')
            # /voice sample <name>          → speak a preview without saving
            # /voice <name>                 → set as default + speak a confirmation
            from tools import (
                get_voice_config, set_voice_config, list_voices,
                speak as _speak, ALL_VOICES,
            )
            # parse args from cmd_text (case-preserved, reply prefix stripped)
            raw = re.sub(r'^/voice\b', '', cmd_text.strip(), count=1).strip()
            parts = raw.split(None, 1)
            sub = parts[0].lower() if parts else ""

            if not sub:
                cur = get_voice_config()
                tg_send(tg_token, chat_id,
                    f"current voice: {cur['voice']}\n"
                    f"engine: {cur['engine']}\n\n"
                    "commands:\n"
                    "/voice list          — show all voices\n"
                    "/voice list british  — filter by keyword\n"
                    "/voice sample am_michael  — preview without saving\n"
                    "/voice am_michael    — set as default")
                sys.exit(0)

            if sub == "list":
                filt = parts[1] if len(parts) > 1 else ""
                tg_send(tg_token, chat_id, list_voices(filt))
                sys.exit(0)

            if sub == "sample":
                if len(parts) < 2:
                    tg_send(tg_token, chat_id, "usage: /voice sample <name>")
                    sys.exit(0)
                name = parts[1].strip()
                if name not in ALL_VOICES:
                    tg_send(tg_token, chat_id, f"unknown voice: {name}\nuse /voice list to see options")
                    sys.exit(0)
                tg_send(tg_token, chat_id, f"preview: {name}")
                _speak(f"This is a preview of voice {name.replace('_', ' ')}.", voice=name)
                sys.exit(0)

            # /voice <name>  → set + speak confirmation
            name = sub  # already lowercased
            # Try the original case too (voice ids are already all lowercase though)
            if name not in ALL_VOICES:
                tg_send(tg_token, chat_id, f"unknown voice: {name}\nuse /voice list to see options")
                sys.exit(0)
            tg_send(tg_token, chat_id, set_voice_config(name))
            _speak(f"Voice set to {name.replace('_', ' ')}. How does this sound?", voice=name)
            sys.exit(0)
        elif cmd == "/restart":
            tg_send(tg_token, chat_id, "restarting...")
            # Spawn a detached bash to do the restart AFTER this process exits,
            # so the "restarting..." reply lands before nuke_all kills our parent.
            bash_exe = r"C:\Program Files\Git\usr\bin\bash.exe"
            if not os.path.exists(bash_exe):
                bash_exe = "bash"
            import subprocess as _sp
            _sp.Popen(
                [bash_exe, "-c", "sleep 3 && bash /d/gerty-lite/gerty-lite.sh restart"],
                stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                close_fds=True,
            )
            sys.exit(0)

        # Inject language directive into system prompt — not into user content.
        # Putting an English prefix in the user message confuses the LLM into
        # thinking the user writes English.
        lang = detect_lang(cmd_text)
        if lang == "uk":
            system = system + "\n\n[Directive: The user writes in Ukrainian. You MUST reply exclusively in Ukrainian. Never switch to English.]"
        else:
            # Force English when current message is English — without this, the
            # model drifts back to Ukrainian due to recent UA history.
            system = system + "\n\n[Directive: The user wrote THIS message in English. You MUST reply exclusively in English in this turn, regardless of how recent conversation history looked. Never switch to Ukrainian unless the user explicitly does so first.]"

        # Active /goal — user-set, persists across /new. Inject every turn so
        # the model keeps the objective in working context. Phrasing tells the
        # model to reference it when relevant rather than mention it on every
        # turn (avoids annoying "still working toward goal X" preambles).
        active_goal = load_goal(chat_id)
        if active_goal and active_goal.get("text"):
            system = system + (
                f"\n\n[Active goal (user-set via /goal, persists until cleared): "
                f"{active_goal['text']}\n"
                f"Thread your work toward this objective when it's relevant to "
                f"the current message. Don't mention it preambly on every turn — "
                f"reference it only when it materially affects your response or "
                f"the user is asking about progress.]"
            )

        # Current voice + gender. Only injected when gender is known; kept
        # short — one line, one anchoring example. The model already knows
        # Ukrainian grammar, it just needs to be told which gender to use.
        try:
            from tools import current_voice_info
            _vi = current_voice_info()
            _g = _vi.get("gender", "unknown")
            if _g in ("feminine", "masculine"):
                _ex = "я зробила" if _g == "feminine" else "я зробив"
                system = system + (
                    f"\n\n[Voice: {_vi['voice']} ({_g}). In Ukrainian, use "
                    f"{_g} first-person forms (e.g. \"{_ex}\").]"
                )
        except Exception as _e:
            print(f"[voice] system-prompt injection skipped: {_e}", file=sys.stderr)

        # Load history early so the router can see the previous turn.
        history = load_history(history_path)

        # Find the last user message and last assistant reply (for router context).
        prev_user = ""
        prev_assistant = ""
        for m in reversed(history):
            if m.get("role") == "assistant" and not prev_assistant:
                prev_assistant = str(m.get("content", ""))
            elif m.get("role") == "user" and not prev_user:
                prev_user = str(m.get("content", ""))
            if prev_user and prev_assistant:
                break

        # Adaptive tool routing — pick only the tools this message likely needs.
        selected_tools: list[str] = []
        if TOOLS_ENABLED:
            selected_tools = select_tools(
                llm_api, model, cmd_text,
                prev_user=prev_user, prev_assistant=prev_assistant,
            )
            print(f"[router] selected={selected_tools}", file=sys.stderr)
            if selected_tools:
                system = system + _tool_addendum_for(selected_tools)
        tools_subset = [t for t in TOOL_SCHEMAS
                        if t["function"]["name"] in selected_tools]

        # Tighter history when tools are in play — keeps total context small.
        hist_window = max_history if selected_tools else (max_history * 2)
        history = history[-hist_window:]
        messages = (
            [{"role": "system", "content": system}]
            + history
            + [{"role": "user", "content": user_text}]
        )

        # Reaction — fast separate call, fires before the main reply
        print(f"[pick_reaction] msg_id={message_id} text={cmd_text[:40]!r}", file=sys.stderr)
        reaction = pick_reaction(llm_api, model, cmd_text)
        print(f"[pick_reaction] result={reaction!r}", file=sys.stderr)
        if reaction:
            execute_tool("send_reaction", {"emoji": reaction})
            print(f"[reaction] sent {reaction}", file=sys.stderr)

        reply = run_agent(llm_api, model, messages, tools_subset=tools_subset)
        if not reply:
            sys.exit(1)

        print(f"[gemma] reply: {reply[:80]}", file=sys.stderr)

        save_history(history_path, user_text, reply, max_history * 2 + 20,
                     api=llm_api, model=model)

        if voice_reply:
            from tools import speak
            clean = _clean_for_tts(reply)
            print(f"[voice_reply] auto-speaking (text suppressed): {clean[:80]!r}", file=sys.stderr)
            speak(clean)
        else:
            tg_send(tg_token, chat_id, reply)

    # ── IMAGE mode ───────────────────────────────────────────────────────────
    elif mode == "image":
        if len(sys.argv) < 11:
            print("image mode requires message_id and image_path arguments", file=sys.stderr)
            sys.exit(2)

        image_path = sys.argv[10]
        caption = user_input.strip() or "what's in this image?"

        # Two-stage pipeline:
        #   1. Vision model JIT-described the image (no tools, no persona, no
        #      agent loop). Its single job is to produce a plain-text caption.
        #   2. The MAIN model receives that caption as a user message and runs
        #      the normal agent loop — it owns tools, persona, history. This
        #      keeps the conversation in main-model voice; vision is just a
        #      describer the user never directly hears from.
        vision_model = os.environ.get("GERTY_VISION_MODEL", "").strip()
        vision_ttl = int(os.environ.get("GERTY_VISION_TTL", "300") or "300")

        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[image] read error: {e}", file=sys.stderr)
            sys.exit(2)
        suffix = Path(image_path).suffix.lower().lstrip(".")
        mime = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                "png":"image/png","webp":"image/webp"}.get(suffix, "image/jpeg")

        # ── STEP 1: vision describe (no tools, neutral system prompt) ──────
        description = ""
        if vision_model:
            print(f"[image] vision describe via {vision_model} (ttl={vision_ttl}s)",
                  file=sys.stderr)
            describe_sys = (
                "You are an image describer for another AI assistant who cannot see the image. "
                "Produce ONE plain-text description. Cover: objects, people, visible text "
                "(transcribe verbatim if readable), colors, setting, mood, any unusual or notable "
                "details. Be factual; do not interpret intent, do not advise, do not greet, do "
                "not speak as an assistant or use first person. Output ONLY the description."
            )
            vision_messages = [
                {"role": "system", "content": describe_sys},
                {"role": "user", "content": [
                    {"type": "text", "text": "Describe this image in detail."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ]},
            ]
            vision_payload = {
                "model": vision_model,
                "messages": vision_messages,
                "temperature": 0.3,
                "max_tokens": 800,
            }
            if vision_ttl:
                vision_payload["ttl"] = vision_ttl
            try:
                description = call_llm(llm_api, vision_payload, timeout=120) or ""
            except Exception as e:
                print(f"[image] vision call failed: {e}", file=sys.stderr)
            print(f"[image] description ({len(description)} chars): {description[:200]}",
                  file=sys.stderr)

        # ── STEP 2: hand off to MAIN model as a text turn ──────────────────
        cap = caption.strip()
        default_caps = ("what's in this image?", "what is in this image?")
        if description:
            if not cap or cap.lower() in default_caps:
                relayed = (
                    "[the user sent you a photo. another model already looked at it "
                    "and described what it shows — use this as your visual context, "
                    "then react/reply naturally as yourself.]\n\n"
                    f"image description: {description}"
                )
            else:
                relayed = (
                    "[the user sent you a photo with a caption. another model already "
                    "looked at the image and described it. use the description as "
                    "visual context, then answer the caption.]\n\n"
                    f"image description: {description}\n\n"
                    f"user's caption: {cap}"
                )
        else:
            # No vision model OR vision call failed — degrade gracefully.
            relayed = (
                "[the user sent a photo, but no image description is available "
                f"(vision model unavailable). caption from the user: {cap or '(none)'}]"
            )

        # Language directive — based on the user's caption, not the vision output.
        lang = detect_lang(caption)
        if lang == "uk":
            system = system + "\n\n[Directive: The user writes in Ukrainian. You MUST reply exclusively in Ukrainian. Never switch to English.]"
        else:
            system = system + "\n\n[Directive: The user wrote THIS message in English. You MUST reply exclusively in English in this turn, regardless of how recent conversation history looked. Never switch to Ukrainian unless the user explicitly does so first.]"

        # Adaptive tool routing on the caption (image content can't influence
        # tool selection because the main model only sees the description text).
        selected_tools: list[str] = []
        if TOOLS_ENABLED:
            selected_tools = select_tools(llm_api, model, caption)
            cap_low = caption.lower()
            web_intent = any(kw in cap_low for kw in (
                "search", "знайди", "пошук", "look up", "find out", "google",
                "погугли", "перевір в інтернеті", "check online",
            ))
            if not web_intent:
                selected_tools = [t for t in selected_tools
                                  if t not in ("web_search", "web_fetch", "browser_open")]
            print(f"[router-image] selected={selected_tools}", file=sys.stderr)
            if selected_tools:
                system = system + _tool_addendum_for(selected_tools)
        tools_subset = [t for t in TOOL_SCHEMAS
                        if t["function"]["name"] in selected_tools]

        # Build the main-model messages: system + recent history + the relayed turn.
        history = load_history(history_path)
        messages = [{"role": "system", "content": system}]
        for h in history[-max_history * 2:]:
            messages.append(h)
        messages.append({"role": "user", "content": relayed})

        # Main model owns the agent loop, the tools, the reply, the history.
        reply = run_agent(llm_api, model, messages, tools_subset=tools_subset)
        if not reply:
            sys.exit(1)

        print(f"[gemma-image] reply: {reply[:80]}", file=sys.stderr)

        # Save history with a human-readable photo tag so future context shows
        # "the user sent a photo with caption X" — not the long vision description.
        save_history(history_path, f"[sent a photo] {caption}", reply,
                     max_history * 2 + 20, api=llm_api, model=model)
        tg_send(tg_token, chat_id, reply)

    # ── ROUTINE mode ─────────────────────────────────────────────────────────
    elif mode == "routine":
        # No incoming user message — the routine prompt drives the agent.
        # The agent decides what to send (via send_message) or write (via write_file).
        # History is not saved for routines — they're stateless.
        routine_prompt_preview = user_input.strip()
        if TOOLS_ENABLED:
            # Route on the routine prompt itself; always include send_message + write_file
            # since most routines need at least one of them for delivery.
            selected = select_tools(llm_api, model, routine_prompt_preview)
            for must_have in ("send_message", "write_file"):
                if must_have not in selected and must_have in TOOL_ONE_LINERS:
                    selected.append(must_have)
            print(f"[router-routine] selected={selected}", file=sys.stderr)
            system = system + _tool_addendum_for(selected)
            tools_subset = [t for t in TOOL_SCHEMAS
                            if t["function"]["name"] in selected]
        else:
            tools_subset = []
        system = (
            system
            + "\n\n[Routine mode] You are running a scheduled routine, not replying to a "
            "live message. The user did NOT just write to you. Follow the routine "
            "instructions exactly. If the routine says to message the user, call "
            "send_message(text). If it says save to vault, call write_file(path, content). "
            "Don't ramble — do the task and stop. Your final text answer is NOT auto-sent "
            "to Telegram, so use tools to deliver output."
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": routine_prompt_preview},
        ]

        # Routines often forward long fetched text (daily verses, news, calendar
        # dumps) via send_message — that text is part of the model's generation,
        # so it competes with Gemma's reasoning tokens against this budget.
        # 4096 leaves room for ~1–2 K of reasoning plus ~2 K of tool-call args.
        reply = run_agent(llm_api, model, messages, max_tokens=4096,
                          tools_subset=tools_subset, show_progress=False)
        print(f"[routine] final-text: {(reply or '')[:120]}", file=sys.stderr)
        # Final-text is intentionally NOT auto-sent; routine controls delivery via tools.

    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
