#!/bin/bash
# GERTY Lite — Gemma 4 listener
# Polls Telegram, routes messages to local LLM at http://127.0.0.1:1234

set -uo pipefail

# Force UTF-8 locale so bash read handles Cyrillic/Ukrainian correctly
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export PYTHONIOENCODING=utf-8

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$BASE_DIR/config/.telegram-config"
LOG="$BASE_DIR/gerty-lite.log"
PID_FILE="$BASE_DIR/.gerty-lite.pid"
OFFSET_FILE="$BASE_DIR/.gemma-offset"
HISTORY_DIR="$BASE_DIR/.history"
PROCESSED_FILE="$BASE_DIR/.processed-ids"
TMP_DIR="$BASE_DIR/.tmp"

# Python-friendly path helper. On Windows + Git Bash we run a Windows Python
# binary that can't open MSYS-style /d/... paths, so cygpath converts to
# D:\... → forward-slash form. On Mac/Linux cygpath doesn't exist and Python
# uses the same POSIX paths bash does — pass through unchanged.
if command -v cygpath >/dev/null 2>&1; then
  to_pypath() { cygpath -w "$1" 2>/dev/null | tr '\\' '/'; }
else
  to_pypath() { echo "$1"; }
fi
HISTORY_DIR_WIN=$(to_pypath "$HISTORY_DIR")
TMP_DIR_WIN=$(to_pypath "$TMP_DIR")

# ── Config ───────────────────────────────────────────────────────────────────
# Source the single-source-of-truth model file when invoked manually (autostart
# already sources it and re-exports, but `bash gerty-lite.sh restart` won't).
[[ -f "$BASE_DIR/config/.model" ]] && source "$BASE_DIR/config/.model"
LLM_API="${GERTY_LLM_API:-http://127.0.0.1:1234}"
MODEL="${GERTY_MODEL:-gemma-4-26b-a4b-it-uncensored}"
# Re-export so spawned daemons (routines.py, context_bar.py) inherit it.
export GERTY_MODEL="$MODEL"
MAX_HISTORY=30
# Portable Python detection: env override → python3 in PATH → python in PATH.
# Skip the Microsoft Store python.exe stub (under WindowsApps), which exists on
# fresh Windows installs and exits immediately with a "Python was not found"
# message — silently killing the routines + context-bar daemons. Falls back to
# the Windows uv install path for back-compat with the original Oleh install.
# On Mac / Linux set GERTY_PYTHON3 or just rely on PATH.
_find_python() {
  local p
  for p in python3 python; do
    local resolved
    resolved="$(command -v "$p" 2>/dev/null)" || continue
    [[ "$resolved" == *WindowsApps* ]] && continue
    echo "$resolved"
    return 0
  done
  echo "/c/Users/alekp/AppData/Roaming/uv/python/cpython-3.12.11-windows-x86_64-none/python.exe"
}
PYTHON3="${GERTY_PYTHON3:-$(_find_python)}"
# Bundled transcribe-audio.py ships in scripts/. Override via env if you want
# a custom STT script.
TRANSCRIBE="${GERTY_TRANSCRIBE:-$SCRIPT_DIR/transcribe-audio.py}"

[[ ! -f "$CONFIG" ]] && { echo "Error: $CONFIG not found"; exit 1; }
source "$CONFIG"

TG_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
OFFSET=0
[[ -f "$OFFSET_FILE" ]] && OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)

mkdir -p "$HISTORY_DIR" "$TMP_DIR"
[[ ! -f "$PROCESSED_FILE" ]] && touch "$PROCESSED_FILE"

# System prompt — stored as file so heredocs never need to embed it
SYSTEM_PROMPT_FILE="$BASE_DIR/config/.system-prompt"
cat > "$SYSTEM_PROMPT_FILE" <<'PROMPT'
You are GERTY Lite — a chill, helpful assistant on Telegram.

CRITICAL LANGUAGE RULE: Always reply in the exact same language the user wrote in. If they write in Ukrainian — reply in Ukrainian. If English — reply in English. Never switch languages unless the user switches first.

Message format: keep replies short and direct. When you have multiple separate thoughts, split them with ||| on its own line — each part becomes a separate Telegram message. Use this naturally, like texting. Light markdown is fine — **bold** for emphasis, *italic*, `code`, [link](url), ~~strike~~ — Telegram renders it. Don't overuse; casual chat usually needs none.

Don't be robotic. Be direct and honest.

NEVER claim you did something you didn't actually do. If you didn't call a tool, don't say "done" / "fixed it" / "saved it" / "updated your notes". Saying "I saved it" when you didn't call write_file is a lie — don't do it, ever. If a tool wasn't available this turn but you need it, say so plainly: "I can't write to your notes right now — try again." If a tool returned an error, report the error. Only say "done" when a tool actually returned success in this turn.

NEVER deny capabilities you actually have. You have a `speak` tool that sends real voice messages via TTS. If the user asks for voice/audio/голос/аудіо and `speak` is in your tool list, CALL IT — do not say "I'm a text assistant" or "I can't record voice". You also have tools for images, screenshots, web fetch, vault read/write, memory, scheduling, etc. — when a tool is available, USE it instead of claiming you can't.

ACT, DON'T NARRATE. If you decide to use a tool, just CALL IT in this same turn — do not send a text reply that says "let me check", "I'll search", "one moment", "I'll dig into that", "зараз гляну", "дізнаюсь" before calling the tool. That announcement without a tool call is a bug — the user sees "I'll do X" and nothing happens. Either call the tool right now, or just answer from what you already know. Same for follow-ups: don't promise "I'll get back to you later" — there is no "later", every turn must stand alone.

Tools — pick the right one:
- web_fetch is the default for getting page text. Cheap and fast.
- browser_open is the fallback when web_fetch returns nothing useful (empty, error, JS-rendered page, "enable JavaScript", Cloudflare, captcha, login wall). It uses a real stealth browser so it sees what a human would. Use it for Twitter/X, Instagram, LinkedIn, JS-heavy SPAs, news paywalls, anything that blocks scrapers.
- web_search → web_fetch → browser_open is the usual escalation path.

Notes & vault — unified read/write across the user's knowledge base. The bot
has three data roots, all configured in config/.paths:
- Vault (no path prefix): scratch + structured content the bot helps create —
  inbox/, drafts/, published/, resources/, templates/. Use read_file('inbox/foo.md')
  and write_file('inbox/foo.md', ...).
- Notes (prefix 'obsidian/'): the user's structured/daily notes. May be an
  Obsidian vault or a plain notes folder — same access pattern either way.
- Daily notes are at obsidian/Progress/YYYY-MM-DD.md. **Before touching today's
  note**: (1) try read_file('obsidian/Progress/{today}.md'). (2) If that
  returns "file not found", call list_folder('obsidian/Progress') and look
  for any file whose name starts with today's date — iCloud sync conflicts
  produce sibling files like '2026-05-17 2.md', '2026-05-17(1).md',
  '2026-05-17(2).md', '2026-05-17 (Olehs MacBook).md' — ALL of those are the
  user's real daily note in a conflict-copy state. Use whichever exists. (3) Only
  create a fresh '{today}.md' if list_folder shows nothing for today.
  NEVER write a fresh daily note without that list_folder check first — you'll
  create a duplicate the user will have to merge by hand.
  **If you do find a conflict-copy AND read/write to it, TELL the user in the
  reply: "heads-up: your daily note is in a conflict copy '2026-05-17(1).md',
  not '2026-05-17.md' — looks like an iCloud sync conflict on your end."**
  Don't silently write to the conflict copy — the user opens Obsidian, looks at
  the normal-named file, sees it empty, and thinks you did nothing.
- To update an existing note: read_file first, then write_file with the FULL
  updated content. write_file replaces; it does not append.
- One file at a time: read_file reads one, write_file writes one. No bulk ops.
- Filenames may be Cyrillic — don't assume Latin when matching.

Memory — your own persistent memory across conversations:
- save_memory(name, content, [description]) when the user says "remember this",
  "don't forget", "запам'ятай", or asks you to keep track of something.
  Pick a kebab-case name (e.g. 'kid-birthday', 'favorite-coffee', 'work-schedule').
- list_memory() to see everything you've saved — one line per entry.
- recall_memory(name) to read one entry back.
- search_memory(query) to find entries by keyword.
- delete_memory(name) ONLY when the user explicitly says "forget X", "забудь про X",
  "delete that memory". Never delete on your own initiative. If the user is vague
  about which entry, call list_memory or search_memory first and confirm the exact
  name before deleting. The tool only touches your own memory — it cannot reach
  the user's organized notes.
- When the user asks "what do you remember about me" / "що ти знаєш про мене",
  call list_memory first, then recall_memory or search_memory for relevant entries.
- Memory is YOURS, not the user's notes. Don't dump random conversation into it —
  save things the user explicitly asks you to remember, or facts that will
  obviously matter in future chats (their kids' names, work, recurring routines).

User files sandbox (FILES_ROOT, prefix 'files/'): a guardrailed folder for the user's own files — PDFs, CVs, documents, attachments they send via Telegram.
- When the user sends ANY file attachment (PDF, docx, txt, zip, etc.), the listener auto-saves it under files/inbox/ and tells you the path. Don't claim you can't accept files — you can, and it's already saved by the time you read the message.
- After auto-save, decide what to do: leave it in inbox/, or use move_file('inbox/<auto-name>', '<category>/<clean-name>') to organize. Categories you create as you go: documents/, cv/, contracts/, receipts/, work/, personal/, etc. — pick whatever fits. The folder structure inside files/ is yours to design.
- read_pdf('files/<path>') to extract text from a PDF (read_file won't work on PDFs).
- read_file('files/<path>'), list_folder('files/<path>'), write_file('files/<path>', ...) for text/md/json — same API as the vault, just under the files/ prefix.
- send_file('files/<path>', caption) when the user asks "send me my CV" / "надішли мені резюме" / any "send me <doc>" request. Use list_folder first if you're not sure which file.
- delete_file('files/<path>') ONLY when the user explicitly asks to delete. Never on your own initiative.
- HARD GUARDRAIL: all file_* tools refuse any path that escapes FILES_ROOT — '..' segments, absolute paths, etc. You cannot touch files outside this sandbox. Don't try; the error is by design.

Routines & reminders: you CAN schedule things. When the user asks "remind me…", "every morning…", "every Tuesday at 5pm…", "do X tomorrow at 9am" — you have create_routine(id, schedule, prompt, one_shot). schedule is a 5-field cron. one_shot=true for single reminders (auto-disables after firing). Write the prompt as instructions for a future agent — include "call send_message(...) to ping me" so the result reaches Telegram. Use list_routines to see what's scheduled, delete_routine(id) to cancel. Never say "I can't schedule that" — you can. Convert natural language to cron yourself.
PROMPT

GEMMA_CHAT="$SCRIPT_DIR/gemma_chat.py"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

# Claim an update_id atomically — returns 0 if new, 1 if already seen
claim_update() {
  local uid="$1"
  local lock="${PROCESSED_FILE}.lock"
  local i=0
  while ! mkdir "$lock" 2>/dev/null; do
    (( i++ )) && [[ $i -gt 20 ]] && return 1
    sleep 0.1
  done
  trap 'rmdir "$lock" 2>/dev/null' RETURN
  if grep -qxF "$uid" "$PROCESSED_FILE" 2>/dev/null; then
    return 1
  fi
  echo "$uid" >> "$PROCESSED_FILE"
  local tmp="${PROCESSED_FILE}.tmp"
  tail -500 "$PROCESSED_FILE" > "$tmp" && mv "$tmp" "$PROCESSED_FILE" || true
  return 0
}

send_message() {
  local chat_id="$1"
  local text="$2"
  [[ -z "$text" ]] && return
  local payload
  payload=$(printf '%s\x00%s' "$chat_id" "$text" | $PYTHON3 -c "
import sys, json
raw = sys.stdin.buffer.read()
nul = raw.index(0)
chat_id = int(raw[:nul])
text = raw[nul+1:].decode('utf-8')
print(json.dumps({'chat_id': chat_id, 'text': text}))
" 2>/dev/null)
  [[ -z "$payload" ]] && return
  curl -s --max-time 10 "$TG_API/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$payload" > /dev/null 2>&1 || true
}

send_typing() {
  curl -s --max-time 5 "$TG_API/sendChatAction" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":$1,\"action\":\"typing\"}" > /dev/null 2>&1 || true
}

# Download a Telegram file by file_id to a local path
# Returns 0 on success, 1 on failure
tg_download() {
  local file_id="$1"
  local dest="$2"
  local file_path
  file_path=$(curl -s --max-time 10 "$TG_API/getFile?file_id=$file_id" | \
    $PYTHON3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('file_path',''))" 2>/dev/null)
  [[ -z "$file_path" ]] && return 1
  curl -s --max-time 60 \
    "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/$file_path" \
    -o "$dest" 2>/dev/null
  [[ -s "$dest" ]]
}

# call_gemma MODE CHAT_ID MESSAGE_ID CONTENT_B64 [REPLY_B64] [IMAGE_PATH] [VOICE_REPLY]
#
# MESSAGE_ID   — Telegram message_id (for reactions and context tools)
# CONTENT_B64  — base64-encoded user text (or caption for images)
# REPLY_B64    — base64-encoded reply-to context (optional, "" to omit)
# IMAGE_PATH   — Windows-style path to image file (image mode only)
# VOICE_REPLY  — "1" to auto-speak the reply as a voice message
#
# All text is decoded from base64 inside Python so no UTF-8 bytes ever
# pass through a bash variable. Returns 0 on success, 1 if LLM offline, 2 on error.
call_gemma() {
  local mode="$1"
  local chat_id="$2"
  local message_id="$3"
  local content_b64="$4"
  local reply_b64="${5:-}"
  local image_path="${6:-}"
  local voice_reply="${7:-0}"
  local history_path="${HISTORY_DIR_WIN}/chat_${chat_id}.json"

  [[ ! -f "${HISTORY_DIR}/chat_${chat_id}.json" ]] && echo "[]" > "${HISTORY_DIR}/chat_${chat_id}.json"

  # Decode base64 fields and combine into final stdin for gemma_chat.py.
  # All string handling happens in Python — bash only holds ASCII base64 chars.
  local tmp_stdin="$TMP_DIR/.stdin.$$.txt"
  $PYTHON3 -c "
import sys, base64
content_b64 = sys.argv[1]
reply_b64   = sys.argv[2] if len(sys.argv) > 2 else ''
content = base64.b64decode(content_b64.encode()).decode('utf-8') if content_b64 else ''
reply   = base64.b64decode(reply_b64.encode()).decode('utf-8').strip() if reply_b64 else ''
final   = ('[replying to: \"' + reply + '\"]\n' + content) if reply else content
sys.stdout.buffer.write(final.encode('utf-8'))
" "$content_b64" "$reply_b64" > "$tmp_stdin" 2>>"$LOG"

  local rc
  if [[ "$mode" == "image" ]]; then
    $PYTHON3 "$GEMMA_CHAT" \
      "image" "$chat_id" "$history_path" "$SYSTEM_PROMPT_FILE" \
      "$LLM_API" "$MODEL" "$MAX_HISTORY" "$TELEGRAM_BOT_TOKEN" \
      "$message_id" "$image_path" "$voice_reply" < "$tmp_stdin" 2>>"$LOG"
    rc=$?
  else
    $PYTHON3 "$GEMMA_CHAT" \
      "text" "$chat_id" "$history_path" "$SYSTEM_PROMPT_FILE" \
      "$LLM_API" "$MODEL" "$MAX_HISTORY" "$TELEGRAM_BOT_TOKEN" \
      "$message_id" "$voice_reply" < "$tmp_stdin" 2>>"$LOG"
    rc=$?
  fi

  rm -f "$tmp_stdin"
  return $rc
}

# b64_encode_file PATH — print base64 of file contents (ASCII-safe for bash vars)
b64_encode_file() {
  $PYTHON3 -c "
import sys, base64
data = open(sys.argv[1], 'rb').read()
sys.stdout.write(base64.b64encode(data).decode('ascii'))
" "$1"
}

# clone_pending_for_chat CHAT_ID
#   Print "<name>\t<lang>\t<gender>" if there's a valid (non-expired,
#   awaiting_audio) clone-pending entry for this chat, else print nothing.
#   "Valid" means: chat_id matches, expires_at is in the future, both lang
#   and gender are set, and neither awaiting_lang nor awaiting_gender is set
#   (we already collected both via reply keyboards).
clone_pending_for_chat() {
  local chat_id="$1"
  local pending_file="$BASE_DIR/.clone-pending.json"
  [[ ! -f "$pending_file" ]] && return 0
  $PYTHON3 -c "
import sys, json, time
chat_id = sys.argv[1]
try:
    p = json.loads(open(sys.argv[2], 'r', encoding='utf-8').read())
except Exception:
    sys.exit(0)
if str(p.get('chat_id')) != chat_id:
    sys.exit(0)
if int(time.time()) > int(p.get('expires_at', 0)):
    sys.exit(0)
if p.get('awaiting_lang') or p.get('awaiting_gender'):
    sys.exit(0)
lang = p.get('lang') or 'en'
gender = p.get('gender') or 'unknown'
name = p.get('name') or ''
if not name:
    sys.exit(0)
print(f'{name}\t{lang}\t{gender}')
" "$chat_id" "$pending_file" 2>/dev/null
}

# ── Single-instance guard (atomic mkdir lock) ─────────────────────────────────
# On Windows bash forks subshells with new PIDs, so PID-tracking is brittle.
# We rely on (1) atomic mkdir of LOCK_DIR (only one process wins), and (2) the
# gerty-lite.sh wrapper's `nuke_all` to clear orphans before starting.
LOCK_DIR="$BASE_DIR/.listener.lock"
MY_WINPID=$(cat /proc/self/winpid 2>/dev/null || echo "$$")
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "Lock dir exists — another listener may be running. Exiting."
  exit 0
fi
echo $$ > "$PID_FILE"
echo "$MY_WINPID" > "$LOCK_DIR/winpid"
echo $$ > "$LOCK_DIR/msyspid"

cleanup() {
  if [[ -f "$PID_FILE" ]] && [[ "$(cat "$PID_FILE" 2>/dev/null)" == "$$" ]]; then
    rm -f "$PID_FILE"
  fi
  if [[ -f "$LOCK_DIR/msyspid" ]] && [[ "$(cat "$LOCK_DIR/msyspid" 2>/dev/null)" == "$$" ]]; then
    rm -rf "$LOCK_DIR"
  fi
  rm -f "$TMP_DIR"/.updates.$$ "$TMP_DIR"/.voice.$$ "$TMP_DIR"/.photo.$$ \
        "$TMP_DIR"/.raw.$$ "$TMP_DIR"/.stdin.$$ "$TMP_DIR"/.trans.$$
  log "GERTY Lite stopped."
}
trap cleanup EXIT INT TERM

log "GERTY Lite (Gemma 4) online. Polling..."

# ── Register Telegram menu commands ──────────────────────────────────────────
# Overwrites whatever stale commands the old Claude channels plugin may have
# left behind at all_private_chats scope. Only registers what gemma_chat.py
# actually handles — anything else just falls through to the LLM.
# Telegram's setMyCommands rejects `<`, `>`, and em-dashes in descriptions
# (they fail a strict regex on the server side and the whole call returns
# {"ok":false}). Earlier versions of this heredoc used em-dashes and angle
# brackets and got silently dropped — Telegram kept stale commands forever
# because curl -s ... > /dev/null hid the rejection. Stick to plain ASCII
# punctuation here, and check the response body below instead of relying
# on the HTTP exit code.
GEMMA_COMMANDS='{"commands":[
  {"command":"new","description":"Reset conversation context"},
  {"command":"status","description":"Show model + thinking mode"},
  {"command":"thinking","description":"Toggle thinking mode on/off"},
  {"command":"goal","description":"Show/set/clear a sustained objective"},
  {"command":"voice","description":"Show/set TTS voice (try /voice list)"},
  {"command":"clone","description":"Clone a voice (asks lang + gender)"},
  {"command":"restart","description":"Restart the bot listener"},
  {"command":"help","description":"List available commands"}
],"scope":{"type":"all_private_chats"}}'
_CMD_RESP=$(curl -s --max-time 10 "$TG_API/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "$GEMMA_COMMANDS" 2>&1)
if echo "$_CMD_RESP" | grep -q '"ok":true'; then
  log "Menu commands registered (all_private_chats scope)."
else
  log "Menu command registration FAILED: $_CMD_RESP"
fi
# Mirror to default scope so the menu is consistent if Telegram falls back
# (chat scope wins over private, private wins over default — pushing both
# is defensive and cheap).
GEMMA_DEFAULT=$(echo "$GEMMA_COMMANDS" | sed 's/"scope":{"type":"all_private_chats"}/"scope":{"type":"default"}/')
curl -s --max-time 10 "$TG_API/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "$GEMMA_DEFAULT" > /dev/null 2>&1 || true

# Cross-platform liveness check for a python PID. Tries `ps` first (Mac /
# Linux / Git Bash with procps), falls back to tasklist (default Git Bash on
# Windows), returns 0 if the PID is alive and running a python binary.
pid_is_python() {
  local pid="$1"
  [[ -z "$pid" ]] && return 1
  if command -v ps >/dev/null 2>&1; then
    ps -p "$pid" 2>/dev/null | grep -qi "python"
    return $?
  fi
  if command -v tasklist >/dev/null 2>&1; then
    tasklist //FI "PID eq $pid" //NH 2>/dev/null | grep -qi "python"
    return $?
  fi
  # No way to check — assume dead so caller respawns.
  return 1
}

# ── Start context-bar daemon (pinned token-usage display) ────────────────────
CONTEXT_BAR_PID_FILE="$BASE_DIR/.context-bar.pid"
ctx_bar_alive=0
if [[ -f "$CONTEXT_BAR_PID_FILE" ]]; then
  existing_ctx_pid=$(cat "$CONTEXT_BAR_PID_FILE" 2>/dev/null || true)
  if pid_is_python "$existing_ctx_pid"; then
    ctx_bar_alive=1
  fi
fi
if [[ "$ctx_bar_alive" -eq 0 ]]; then
  rm -f "$CONTEXT_BAR_PID_FILE"
  nohup "$PYTHON3" "$SCRIPT_DIR/context_bar.py" >> "$BASE_DIR/context-bar.log" 2>&1 &
  ctx_bar_pid=$!
  disown
  sleep 0.5
  if pid_is_python "$ctx_bar_pid"; then
    log "Context-bar daemon started (PID $ctx_bar_pid)."
  else
    log "WARNING: context-bar daemon died immediately (PYTHON3=$PYTHON3) — see context-bar.log"
  fi
else
  log "Context-bar daemon already running (PID $existing_ctx_pid)."
fi

# ── Start routines daemon if not already running ─────────────────────────────
ROUTINES_PID_FILE="$BASE_DIR/.routines.pid"
routines_alive=0
if [[ -f "$ROUTINES_PID_FILE" ]]; then
  existing_routines_pid=$(cat "$ROUTINES_PID_FILE" 2>/dev/null || true)
  if pid_is_python "$existing_routines_pid"; then
    routines_alive=1
  fi
fi
if [[ "$routines_alive" -eq 0 ]]; then
  rm -f "$ROUTINES_PID_FILE"
  nohup "$PYTHON3" "$SCRIPT_DIR/routines.py" >> "$BASE_DIR/routines.log" 2>&1 &
  routines_pid=$!
  disown
  sleep 0.5
  if pid_is_python "$routines_pid"; then
    log "Routines daemon started (PID $routines_pid)."
  else
    log "WARNING: routines daemon died immediately (PYTHON3=$PYTHON3) — see routines.log"
  fi
else
  log "Routines daemon already running (PID $existing_routines_pid)."
fi

# ── Main poll loop ────────────────────────────────────────────────────────────
UPDATES_FILE="$TMP_DIR/.updates.$$"
RAW_FILE="$TMP_DIR/.raw.$$"

while true; do
  # Write curl response directly to file — never store in a bash variable.
  # Bash command substitution $(curl ...) corrupts multi-byte UTF-8 on Windows git bash.
  curl -s --max-time 15 \
    "$TG_API/getUpdates?offset=$OFFSET&timeout=10" \
    -o "$RAW_FILE" 2>/dev/null || { rm -f "$RAW_FILE"; sleep 1; continue; }

  [[ ! -s "$RAW_FILE" ]] && { rm -f "$RAW_FILE"; continue; }

  # Parse updates to temp file.
  # Text fields (content, reply_text, caption) are base64-encoded so the
  # tab-separated values written here — and later stored in bash variables — are
  # pure ASCII. No multi-byte UTF-8 ever touches a bash variable.
  #
  # Format: update_id TAB chat_id TAB msg_type TAB field4 TAB field5
  #   TEXT  → field4=content_b64,  field5=reply_b64
  #   PHOTO → field4=file_id,      field5=caption_b64
  #   VOICE → field4=file_id,      field5=_
  #   SKIP  → field4=_,            field5=_
  $PYTHON3 -c "
import sys, json, base64

def b64(v):
    return base64.b64encode((v or '').encode('utf-8')).decode('ascii')

data = open(sys.argv[1], 'rb').read()
try:
    d = json.loads(data)
    for u in d.get('result', []):
        uid  = u.get('update_id', 0)
        msg  = u.get('message') or u.get('edited_message')
        if not msg:
            continue
        chat_id = str(msg.get('chat', {}).get('id', ''))
        if not chat_id:
            continue
        text       = msg.get('text', '') or msg.get('caption', '') or ''
        # Forwarded message? Note the original source for context.
        fwd_origin = msg.get('forward_origin') or {}
        fwd_from   = msg.get('forward_from') or {}
        fwd_chat   = msg.get('forward_from_chat') or {}
        fwd_sender = (
            (fwd_origin.get('sender_user') or {}).get('first_name')
            or (fwd_origin.get('sender_chat') or {}).get('title')
            or fwd_origin.get('sender_user_name')
            or fwd_from.get('first_name')
            or fwd_chat.get('title')
            or ''
        )
        if fwd_sender and text:
            text = f'[forwarded from {fwd_sender}]\n{text}'

        # Reply context — describe non-text replies so the model gets a signal
        # about what the user is pointing at (voice/photo/sticker/etc).
        reply_msg = msg.get('reply_to_message') or {}
        reply_text = reply_msg.get('text', '') or reply_msg.get('caption', '') or ''
        if reply_msg and not reply_text:
            if reply_msg.get('voice'):
                dur = reply_msg.get('voice', {}).get('duration', 0)
                reply_text = f'[earlier voice message — {dur}s]'
            elif reply_msg.get('audio'):
                reply_text = '[earlier audio message]'
            elif reply_msg.get('photo'):
                reply_text = '[earlier photo]'
            elif reply_msg.get('sticker'):
                emj = reply_msg.get('sticker', {}).get('emoji', '')
                reply_text = f'[earlier sticker {emj}]'.strip()
            elif reply_msg.get('video'):
                reply_text = '[earlier video]'
            elif reply_msg.get('document'):
                reply_text = '[earlier document]'
            elif reply_msg.get('animation'):
                reply_text = '[earlier animation/GIF]'
        # If reply_to is from the bot itself, note that too
        if reply_msg and reply_msg.get('from', {}).get('is_bot'):
            reply_text = f'[my earlier reply] {reply_text}'.strip()

        voice      = msg.get('voice') or msg.get('audio')
        photos     = msg.get('photo', [])
        # Telegram delivers MP3/M4A uploads as 'document' when sent as a file
        # attachment (not via voice-record). Treat any audio/* document as AUDIO
        # so the clone pipeline can pick it up.
        document   = msg.get('document') or {}
        doc_mime   = document.get('mime_type', '') if document else ''
        is_audio_doc = bool(document) and doc_mime.startswith('audio/')

        msg_id  = str(msg.get('message_id', 0))
        sticker = msg.get('sticker')
        if voice:
            fid  = voice.get('file_id', '')
            # Voice has no caption; smuggle reply_text in the 6th field so the
            # bash side can decode it after transcription.
            line = f'{uid}\t{chat_id}\t{msg_id}\tVOICE\t{fid}\t{b64(reply_text)}\n'
        elif is_audio_doc:
            fid = document.get('file_id', '')
            # Audio doc — only meaningful in clone mode. Pass file_id through.
            line = f'{uid}\t{chat_id}\t{msg_id}\tAUDIO\t{fid}\t_\n'
        elif document:
            # Non-audio attachment (PDF, docx, txt, zip, …). Save to FILES_ROOT
            # via the bash handler; field5 carries filename+mime+caption packed
            # as base64(JSON) so we don't lose any of it across the tab format.
            fid = document.get('file_id', '')
            meta = {
                'filename': document.get('file_name') or 'attachment',
                'mime': doc_mime or 'application/octet-stream',
                'size': document.get('file_size', 0),
                'caption': text,
            }
            meta_b64 = b64(json.dumps(meta, ensure_ascii=False))
            line = f'{uid}\t{chat_id}\t{msg_id}\tDOCUMENT\t{fid}\t{meta_b64}\n'
        elif photos:
            fid  = photos[-1].get('file_id', '')
            # 6th field = base64(reply_text) — caption already inlined via text
            cap = text if text else ''
            if reply_text:
                cap = (cap + '\n\n[reply context] ' + reply_text).strip() if cap else f'[reply context] {reply_text}'
            line = f'{uid}\t{chat_id}\t{msg_id}\tPHOTO\t{fid}\t{b64(cap)}\n'
        elif sticker:
            fid   = sticker.get('file_id', '')
            emoji = sticker.get('emoji', '')
            line  = f'{uid}\t{chat_id}\t{msg_id}\tSTICKER\t{fid}\t{b64(emoji)}\n'
        elif text:
            line = f'{uid}\t{chat_id}\t{msg_id}\tTEXT\t{b64(text)}\t{b64(reply_text)}\n'
        else:
            line = f'{uid}\t{chat_id}\t{msg_id}\tSKIP\t_\t_\n'
        sys.stdout.buffer.write(line.encode('ascii'))
except Exception as e:
    sys.stderr.write(str(e) + '\n')
" "$RAW_FILE" > "$UPDATES_FILE" 2>/dev/null
  rm -f "$RAW_FILE"

  if [[ ! -s "$UPDATES_FILE" ]]; then
    rm -f "$UPDATES_FILE"
    continue
  fi

  # Per-chat deferred TEXTs (Part A — same-poll batching). When a TEXT is followed
  # by VOICE/PHOTO from the same chat in the same poll, we skip the TEXT's LLM
  # call and stash its decoded content here; the media handler prepends it.
  PENDING_DIR="$TMP_DIR/.pending.$$"
  mkdir -p "$PENDING_DIR"

  # Referent-word regex (Part B). If a TEXT contains any of these AND has no
  # follow-up media in this poll batch AND nothing in pending — give the user
  # 4 seconds to fire a quick follow-up via a no-offset secondary poll.
  REFERENT_RE='\b(this|that|it|he|she|here|check)\b|цьог|цієї|перевір|ось|тут|тіє'

  while IFS=$'\t' read -r update_id chat_id message_id msg_type content reply_text; do
    # Validate update_id is numeric
    [[ "$update_id" =~ ^[0-9]+$ ]] || continue
    [[ -z "$chat_id" ]]            && continue

    # Always advance offset
    OFFSET=$((update_id + 1))
    echo "$OFFSET" > "$OFFSET_FILE"

    # Allowlist — check against config/.allowed-chats (one chat_id per line)
    if ! grep -qxF "$chat_id" "$BASE_DIR/config/.allowed-chats" 2>/dev/null; then
      log "[blocked] message from unknown chat $chat_id — ignored"
      continue
    fi

    # Skip unsupported types
    [[ "$msg_type" == "SKIP" ]] && continue

    # Dedup — skip if already processed by another instance
    claim_update "$update_id" || continue

    # ── Handle by type ───────────────────────────────────────────────────────
    case "$msg_type" in

      TEXT)
        # content = content_b64, reply_text = reply_b64 (both pure ASCII base64)
        log "[$chat_id] TEXT (b64 len=${#content})"

        # ── /read <url|path|text> — handled by read-aloud.py, bypasses LLM ─
        "$PYTHON3" "$SCRIPT_DIR/read-aloud.py" \
          "$chat_id" "$TELEGRAM_BOT_TOKEN" --from-b64 "$content" >> "$LOG" 2>&1
        read_rc=$?
        if [[ $read_rc -ne 2 ]]; then
          # 0 = handled OK, 1 = /read failed (already reported); both skip the LLM
          log "[read-aloud] rc=$read_rc — LLM skipped"
          continue
        fi

        # ── Part A: defer if a VOICE/PHOTO from same chat is still in this batch ─
        if awk -v u="$update_id" -v c="$chat_id" -F'\t' '
              $1+0 > u+0 && $2 == c && ($4 == "VOICE" || $4 == "PHOTO") {found=1; exit}
              END {exit !found}
            ' "$UPDATES_FILE"; then
          log "[$chat_id] TEXT deferred — VOICE/PHOTO follows in same poll"
          echo "$content" | base64 -d >> "$PENDING_DIR/$chat_id.txt"
          printf '\n\n' >> "$PENDING_DIR/$chat_id.txt"
          continue
        fi

        # ── Part B: secondary quick-poll if text has a referent + no media here ─
        decoded_for_referent=$(echo "$content" | base64 -d 2>/dev/null || true)
        if [[ "$decoded_for_referent" =~ $REFERENT_RE ]]; then
          # Brief secondary poll WITHOUT advancing offset — peek for new messages
          peek=$(curl -s --max-time 4 \
            "$TG_API/getUpdates?offset=$OFFSET&timeout=3&allowed_updates=%5B%5D")
          if echo "$peek" | grep -q "\"chat\":{\"id\":$chat_id"; then
            if echo "$peek" | grep -q '"voice":\|"photo":\['; then
              log "[$chat_id] TEXT deferred — follow-up media arrived during secondary poll"
              echo "$content" | base64 -d >> "$PENDING_DIR/$chat_id.txt"
              printf '\n\n' >> "$PENDING_DIR/$chat_id.txt"
              continue
            fi
          fi
        fi

        # ── Normal message → LLM (slash commands handled inside gemma_chat.py) ─
        send_typing "$chat_id"
        log "[send] → $chat_id"
        call_gemma "text" "$chat_id" "$message_id" "$content" "$reply_text" || \
          send_message "$chat_id" "не можу відповісти зараз"
        continue
        ;;

      VOICE)
        log "[$chat_id] VOICE file_id=${content}"

        # Clone-mode pre-check: if /clone is armed for this chat, fire the clone
        # pipeline and skip the normal transcribe→LLM flow.
        clone_info=$(clone_pending_for_chat "$chat_id")
        if [[ -n "$clone_info" ]]; then
          IFS=$'\t' read -r clone_name clone_lang clone_gender <<< "$clone_info"
          log "[clone] firing for chat=$chat_id name=$clone_name lang=$clone_lang gender=$clone_gender"
          nohup "$PYTHON3" "$SCRIPT_DIR/clone-voice.py" \
            "$chat_id" "$content" "$clone_name" "$TELEGRAM_BOT_TOKEN" "$clone_lang" "$clone_gender" \
            >> "$LOG" 2>&1 &
          disown
          continue
        fi

        send_typing "$chat_id"
        tmp_voice="$TMP_DIR/.voice.$$.ogg"
        tmp_voice_win=$(to_pypath "$tmp_voice")
        [[ -z "$tmp_voice_win" ]] && tmp_voice_win="$tmp_voice"
        tmp_trans="$TMP_DIR/.trans.$$.txt"

        if tg_download "$content" "$tmp_voice"; then
          # Write transcription to file — never store in bash variable
          $PYTHON3 "$TRANSCRIBE" "$tmp_voice_win" --model large-v3-turbo > "$tmp_trans" 2>>"$LOG" || true
          rm -f "$tmp_voice"

          if [[ ! -s "$tmp_trans" ]]; then
            rm -f "$tmp_trans"
            log "[voice] transcription failed"
            send_message "$chat_id" "не зміг розібрати голосове"
            continue
          fi

          log "[voice] transcription done ($(wc -c < "$tmp_trans") bytes)"

          # Part A: prepend any deferred TEXT from same chat (same-poll batch)
          if [[ -f "$PENDING_DIR/$chat_id.txt" ]]; then
            log "[$chat_id] merging $(wc -c < "$PENDING_DIR/$chat_id.txt") bytes of deferred TEXT into voice context"
            tmp_combined="$TMP_DIR/.combined.$$.txt"
            {
              echo "[user typed first]"
              cat "$PENDING_DIR/$chat_id.txt"
              echo "[then sent voice]"
              cat "$tmp_trans"
            } > "$tmp_combined"
            mv "$tmp_combined" "$tmp_trans"
            rm -f "$PENDING_DIR/$chat_id.txt"
          fi

          # Base64-encode the transcription for safe passing through call_gemma
          trans_b64=$(b64_encode_file "$tmp_trans")
          rm -f "$tmp_trans"

          send_typing "$chat_id"
          log "[send] → $chat_id (voice reply mode)"
          # reply_text holds the base64-encoded reply context from the parser
          # (e.g. "[earlier voice message — 12s]" if user replied to a voice).
          call_gemma "text" "$chat_id" "$message_id" "$trans_b64" "$reply_text" "" "1" || \
            send_message "$chat_id" "не можу відповісти зараз"
        else
          rm -f "$tmp_voice" "$tmp_trans"
          send_message "$chat_id" "не зміг завантажити голосове"
        fi
        continue
        ;;

      AUDIO)
        log "[$chat_id] AUDIO doc file_id=${content:0:40}"
        # Audio files (mp3/m4a/wav uploaded as attachments) are only useful for
        # voice cloning. If /clone is armed, dispatch; otherwise, gently nudge.
        clone_info=$(clone_pending_for_chat "$chat_id")
        if [[ -n "$clone_info" ]]; then
          IFS=$'\t' read -r clone_name clone_lang clone_gender <<< "$clone_info"
          log "[clone] firing for chat=$chat_id name=$clone_name lang=$clone_lang gender=$clone_gender (audio doc)"
          nohup "$PYTHON3" "$SCRIPT_DIR/clone-voice.py" \
            "$chat_id" "$content" "$clone_name" "$TELEGRAM_BOT_TOKEN" "$clone_lang" "$clone_gender" \
            >> "$LOG" 2>&1 &
          disown
        else
          send_message "$chat_id" \
            "got an audio file, but no clone window is open. start with: /clone <name> [en|uk|both] [male|female]"
        fi
        continue
        ;;

      DOCUMENT)
        log "[$chat_id] DOCUMENT file_id=${content:0:40}"
        send_typing "$chat_id"
        tmp_doc="$TMP_DIR/.doc.$$"
        if tg_download "$content" "$tmp_doc"; then
          # reply_text holds b64(JSON{filename, mime, size, caption}). Hand the
          # download off to tools.save_incoming_file (moves it into FILES_ROOT)
          # and build a base64 content payload for gemma describing what arrived.
          content_b64=$($PYTHON3 -c "
import sys, os, json, base64
sys.path.insert(0, sys.argv[1])
from tools import save_incoming_file
meta_b64 = sys.argv[2]
tmp_path = sys.argv[3]
try:
    meta = json.loads(base64.b64decode(meta_b64.encode()).decode('utf-8')) if meta_b64 and meta_b64 != '_' else {}
except Exception:
    meta = {}
filename = meta.get('filename') or 'attachment'
mime     = meta.get('mime')     or 'application/octet-stream'
size     = meta.get('size', 0)
caption  = (meta.get('caption') or '').strip()
saved = save_incoming_file(tmp_path, filename)
if saved.startswith('error:'):
    msg = f'[user sent file: {filename} — could not save it: {saved}]'
else:
    bits = [
        f'[user sent file: {filename}]',
        f'saved at: files/{saved}',
        f'mime: {mime}, size: {size} bytes',
    ]
    if caption:
        bits.append(f'caption: {caption}')
    bits.append(
        'You can: read_pdf(...) for PDFs, read_file(\"files/...\") for text/md, '
        'move_file(...) to organize into a folder, send_file(...) to send back later.'
    )
    msg = '\n'.join(bits)
sys.stdout.write(base64.b64encode(msg.encode('utf-8')).decode('ascii'))
" "$SCRIPT_DIR" "$reply_text" "$tmp_doc" 2>>"$LOG")
          # save_incoming_file moves the temp file — but make sure no leftover
          rm -f "$tmp_doc" 2>/dev/null
          if [[ -n "$content_b64" ]]; then
            call_gemma "text" "$chat_id" "$message_id" "$content_b64" "" || \
              send_message "$chat_id" "got the file but couldn't process it"
          else
            send_message "$chat_id" "не зміг зберегти файл"
          fi
        else
          rm -f "$tmp_doc"
          send_message "$chat_id" "не зміг завантажити файл"
        fi
        continue
        ;;

      PHOTO)
        log "[$chat_id] PHOTO file_id=${content:0:40}"
        send_typing "$chat_id"
        tmp_photo="$TMP_DIR/.photo.$$.jpg"
        tmp_photo_win=$(to_pypath "$tmp_photo")
        [[ -z "$tmp_photo_win" ]] && tmp_photo_win="$tmp_photo"

        if tg_download "$content" "$tmp_photo"; then
          # reply_text holds caption_b64; default caption if empty
          caption_b64="$reply_text"
          if [[ -z "$caption_b64" || "$caption_b64" == "_" ]]; then
            caption_b64=$(printf 'what is in this image?' | \
              $PYTHON3 -c "import sys,base64; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())")
          fi
          # Part A: prepend any deferred TEXT from same chat (same-poll batch)
          if [[ -f "$PENDING_DIR/$chat_id.txt" ]]; then
            log "[$chat_id] merging $(wc -c < "$PENDING_DIR/$chat_id.txt") bytes of deferred TEXT into photo caption"
            decoded_caption=$(echo "$caption_b64" | base64 -d 2>/dev/null || true)
            combined=$({
              echo "[user typed first]"
              cat "$PENDING_DIR/$chat_id.txt"
              echo "[then sent photo with caption]"
              echo "$decoded_caption"
            })
            caption_b64=$(printf '%s' "$combined" | \
              $PYTHON3 -c "import sys,base64; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())")
            rm -f "$PENDING_DIR/$chat_id.txt"
          fi
          log "[$chat_id] PHOTO caption_b64 len=${#caption_b64}"
          call_gemma "image" "$chat_id" "$message_id" "$caption_b64" "" "$tmp_photo_win"
          rc=$?
          rm -f "$tmp_photo"
          [[ $rc -ne 0 ]] && send_message "$chat_id" "не зміг обробити фото"
        else
          rm -f "$tmp_photo"
          send_message "$chat_id" "не зміг завантажити фото"
        fi
        continue
        ;;

      STICKER)
        log "[$chat_id] STICKER file_id=${content:0:40}"
        # Save to user sticker library so the bot can reuse it
        sticker_lib="$BASE_DIR/config/stickers.json"
        if [[ -f "$sticker_lib" && -n "$content" ]]; then
          $PYTHON3 -c "
import sys, json
from pathlib import Path
fid = sys.argv[1]
lib_path = Path(sys.argv[2])
try:
    lib = json.loads(lib_path.read_text(encoding='utf-8'))
    user_stickers = lib.get('_user', [])
    if fid not in user_stickers:
        user_stickers.append(fid)
        lib['_user'] = user_stickers
        lib_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'saved sticker {fid[:30]}', file=sys.stderr)
except Exception as e:
    print(f'sticker save error: {e}', file=sys.stderr)
" "$content" "$sticker_lib" 2>>"$LOG" || true
        fi
        # React with a matching sticker back
        send_typing "$chat_id"
        emoji_b64="$reply_text"
        sticker_text_b64=$(printf 'sent you a sticker' | \
          $PYTHON3 -c "import sys,base64; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())")
        call_gemma "text" "$chat_id" "$message_id" "$sticker_text_b64" || true
        continue
        ;;

      *)
        continue
        ;;
    esac

  done < "$UPDATES_FILE"
  rm -f "$UPDATES_FILE"

  # End-of-batch flush: only flush deferred TEXTs older than ~25 seconds. Newer
  # ones might still be waiting for follow-up media that will arrive in the next
  # poll iteration (10s long-poll). Same-poll deferrals are already handled by
  # the VOICE/PHOTO branches which consume the pending file inline.
  if [[ -d "$PENDING_DIR" ]]; then
    now_ts=$(date +%s)
    for pf in "$PENDING_DIR"/*.txt; do
      [[ -f "$pf" ]] || continue
      mtime=$(stat -c %Y "$pf" 2>/dev/null || echo "$now_ts")
      age=$((now_ts - mtime))
      [[ $age -lt 25 ]] && continue
      chat_id=$(basename "$pf" .txt)
      log "[$chat_id] flushing deferred TEXT (age ${age}s, no follow-up arrived)"
      content_b64=$($PYTHON3 -c "
import base64, sys
with open(sys.argv[1], 'rb') as f: sys.stdout.write(base64.b64encode(f.read()).decode())
" "$pf")
      rm -f "$pf"
      send_typing "$chat_id"
      call_gemma "text" "$chat_id" "0" "$content_b64" "" || \
        send_message "$chat_id" "не можу відповісти зараз"
    done
    # Keep the dir for next iteration if any pending texts remain
    rmdir "$PENDING_DIR" 2>/dev/null || true
  fi
done
