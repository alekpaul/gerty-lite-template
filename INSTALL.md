# Install — manual path (no wizard)

This guide is for power users who want to set GERTY up by hand or who hit
a wall with `python setup.py`. For most people, the wizard is faster —
see [ONBOARDING.md](ONBOARDING.md).

## Prerequisites

Hardware and software, see [REQUIREMENTS.md](REQUIREMENTS.md). In short:

- **macOS 12+** or **Windows 10/11**
- **Python 3.10+**
- **LM Studio** with `lms` CLI
- **A Telegram bot token** from `@BotFather`
- **An NVIDIA GPU** if you want fast inference (Apple Silicon also works
  through LM Studio's Metal backend, but expect lower throughput)
- **Optional**: Node.js 18+ + PM2 for camofox stealth browser, ffmpeg for
  voice messages

## 1. Clone

```bash
# Pick a path that's comfortable for you — GERTY no longer requires a
# specific drive letter or location. The repo finds itself via __file__.
git clone <your-fork-url> ~/gerty-lite           # macOS / Linux
git clone <your-fork-url> D:\gerty-lite          # Windows (PowerShell)
cd gerty-lite
```

## 2. Install Python dependencies

The bot uses only the standard library + `psutil` (cross-platform process
helpers) and `fastapi + uvicorn` (admin dashboard). Install into a venv
of your choice:

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install psutil fastapi uvicorn pydantic pypdf
```

## 3. Configure secrets

Telegram token + chat id go in `config/.telegram-config` (gitignored):

```bash
cp config/.telegram-config.template config/.telegram-config
# Edit and fill in:
#   TELEGRAM_BOT_TOKEN=123456:ABCdef…
#   TELEGRAM_CHAT_ID=987654321
```

The allowlist (one chat id per line):

```bash
echo "987654321" > config/.allowed-chats
```

## 4. Pick a model

Open LM Studio, download a chat model (e.g. `gemma-4-26b-a4b-it-uncensored`)
and optionally a vision model (`gemma-3-4b-it`). Then point GERTY at the
chat model:

```bash
cp config/.paths.template config/.paths     # data folders
# Edit config/.model:
#   export GERTY_MODEL="gemma-4-26b-a4b-it-uncensored"
#   export GERTY_CONTEXT=32768
#   export GERTY_VISION_MODEL="gemma-3-4b-it"
#   export GERTY_VISION_TTL=300
```

## 5. Seed routines + data folders

```bash
cp config/routines.example.json config/routines.json
# Edit chat_id at the top of config/routines.json to match your TELEGRAM_CHAT_ID

mkdir -p data/vault/inbox data/vault/drafts data/vault/published \
         data/notes/Progress data/memory/entries data/files/inbox
```

## 6. Boot

```bash
bash gerty-lite.sh start          # macOS, Linux, Windows (Git Bash)
bash gerty-lite.sh status         # verify it's up
```

DM your bot on Telegram. Reply should arrive in ~5 seconds.

## 7. Admin dashboard (optional)

```bash
python admin/server.py
# Open http://127.0.0.1:9090
```

## 8. Autostart on login (optional)

```bash
python scripts/install-autostart.py
# Use `uninstall` to remove.
```

- macOS: writes `~/Library/LaunchAgents/com.gerty.lite.*.plist` and loads
  them via `launchctl`.
- Windows: registers `Gerty Autostart` (logon trigger) + `Gerty Health
  Check` (every 5 min) in Task Scheduler.

## Manual control

```bash
bash gerty-lite.sh start          # start the listener
bash gerty-lite.sh stop           # stop
bash gerty-lite.sh restart        # restart
bash gerty-lite.sh status         # check
bash gerty-lite.sh log            # tail last 40 lines

# Admin dashboard
python admin/server.py            # http://127.0.0.1:9090

# Re-run setup wizard for missing values
python setup.py
```

## Optional: stealth browser (camofox)

Used by the `browser_open` and `take_screenshot` tools. Skip if you don't
care about web tools.

```bash
cd camofox-browser
npm ci
pm2 start server.js --name camofox
pm2 save
cd ..
```

## Optional: voice TTS

Voice engines live outside this repo by design — they're heavy and
optional. Two supported:

- **Kokoro** (fast English, light): set `GERTY_KOKORO_TTS=/path/to/kokoro-server.py`
  in `config/.env`
- **OmniVoice** (cloned voices, GPU-friendly): start its server, then
  pick a voice from the admin **Voice** tab

The bot's `speak()` / `read_aloud()` tools degrade to text-only if no
voice engine is reachable.

## Optional: gerty-live (realtime voice WS)

Sibling repo — clone next to gerty-lite:

```bash
cd ..
git clone <your-fork-url-of-gerty-live> gerty-live
```

Set `GERTY_LIVE_DIR=/path/to/gerty-live` in `config/.env` if you don't
keep it at the default `../gerty-live`. The admin Overview tab will then
show its health alongside the other components.

## Where to look when things break

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Logs:

```
gerty-lite.log    # listener
admin.log         # FastAPI dashboard
routines.log      # cron daemon
health.log        # health check
autostart.log     # boot sequence
watchdog.log      # offline wake-on-message
```

All in the repo root.
