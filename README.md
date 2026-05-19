# GERTY

A local-first, GPU-resident Telegram bot. You DM it; it replies in your
language with text, voice, reactions, and can read/write your notes,
search the web through a stealth browser, and run a long list of tools.
No cloud LLM. No data leaves the host machine except the messages you
choose to send through Telegram.

> **Status**: works on macOS and Windows. Linux unofficial. One-time setup
> wizard included — first run takes about 60 seconds plus an LM Studio model
> download.

```
                       Telegram (mobile / desktop)
                                │
                                ▼
                ┌──────────────────────────────┐
                │  gerty-lite listener         │
                │  (gemma-listener.sh)         │
                └─────────┬────────────────────┘
                          │
                          ▼
            ┌────────────────────────────────┐
            │   LM Studio (Gemma 26B)        │
            │   OmniVoice or Kokoro (TTS)    │
            │   faster-whisper (ASR)         │
            │   camofox (stealth browser)    │
            └────────────────────────────────┘
                         on your machine
```

## Why this exists

- **No cloud LLM.** The model runs in LM Studio on your own GPU. Telegram is
  the only network egress.
- **One GPU, whole stack.** A 24 GB card fits a 26B model + TTS + ASR + room.
- **Boots on reboot.** Installs a launchd agent (macOS) or Scheduled Task
  (Windows) so the bot is up after every reboot.

## Quick start

```bash
git clone <your-fork-url> gerty-lite
cd gerty-lite
python setup.py
```

The wizard walks you through:

1. Python version check (need 3.10+)
2. LM Studio detection — paste the chat model id from `lms ls`
3. Telegram bot — paste a token from `@BotFather` + your chat id
4. Data folders — defaults to `./data/...`, point them at iCloud/Dropbox if
   you have an existing vault
5. Optional voice engine and stealth browser

When it's done:

```bash
bash gerty-lite.sh start             # boot the listener
python admin/server.py               # admin dashboard at http://127.0.0.1:9090
python scripts/install-autostart.py  # launch on every login
```

Open Telegram, message your bot. First message takes a few seconds while the
model warms up.

## What's in the box

| Path | What it does |
|---|---|
| `setup.py` | Cross-platform first-run wizard |
| `gerty-lite.sh` | start / stop / restart / status manager |
| `scripts/_paths.py`, `scripts/_proc.py` | cross-platform path + process helpers |
| `scripts/gemma-listener.sh` | Long-running poll loop |
| `scripts/gemma_chat.py` | Per-message handler — runs the LLM, tools, replies |
| `scripts/tools.py` | Tool implementations (web, vault, memory, voice, files, screenshot) |
| `scripts/routines.py` | Cron-scheduled background tasks |
| `scripts/install-autostart.py` | Installs launchd (macOS) or Task Scheduler (Windows) entries |
| `admin/` | FastAPI dashboard — system prompt, models, stats, routines, memory, files, MCP, voice |
| `config/` | All configurable settings. Personal/secret files are gitignored. |
| `camofox-browser/` | Optional stealth headless browser (PM2) |
| `data/` | Local vault, notes, memory, files (gitignored content) |

## Documentation

- **[ONBOARDING.md](ONBOARDING.md)** — step-by-step first-run walkthrough for
  Mac and Windows
- **[INSTALL.md](INSTALL.md)** — manual install for power users
- **[REQUIREMENTS.md](REQUIREMENTS.md)** — hardware + software prerequisites
- **[config/gerty-lite-rules.md](config/gerty-lite-rules.md)** — architecture
  + tools + persona reference
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — common issues

## Tools the bot has

After setup the bot can:

- **Web**: `web_search` (DuckDuckGo), `web_fetch` (plain HTTP), `browser_open`
  (camofox stealth browser, used when sites block scrapers)
- **Vault**: `read_file`, `write_file`, `list_folder` against `VAULT_ROOT`
  (and the `obsidian/` prefix → `NOTES_ROOT`)
- **Files sandbox**: `read_pdf`, `send_file`, `move_file`, `delete_file` —
  all guardrailed to `FILES_ROOT`
- **Memory**: `save_memory`, `recall_memory`, `search_memory`, `list_memory`,
  `delete_memory` — persistent across conversations
- **Telegram**: `send_reaction`, `send_sticker`, `send_message`, `send_image`,
  `speak`, `read_aloud`
- **Shell**: `run_shell` — bash command
- **Screenshot**: `take_screenshot` of any URL via camofox
- **Routines**: `create_routine`, `list_routines`, `delete_routine` — bot
  can schedule its own jobs

## License

MIT — see [LICENSE](LICENSE). Fork it, ship it, sell it, just keep the
copyright notice.
