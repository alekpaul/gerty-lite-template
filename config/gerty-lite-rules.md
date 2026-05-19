# GERTY Lite — project context

A local-only Telegram bot powered by a local Gemma-class model running in LM Studio. No cloud LLM, no data leaves the host machine except the messages you DM. Runs on macOS and Windows.

## Architecture

```
Telegram ──getUpdates──> gemma-listener.sh ──spawns──> gemma_chat.py ──HTTP──> LM Studio
                              │                              │
                              ├── registers /menu commands   ├── runs tools (tools.py)
                              └── starts routines.py daemon  └── sends reply via Telegram API
```

Key files:
- `gerty-lite.sh` — manager (start / stop / restart / status).
- `scripts/gemma-listener.sh` — long-running poll loop. Single-instance via `mkdir` lock at `.listener.lock/`. Registers menu commands at `all_private_chats` scope on startup. Spawns the routines daemon. Regenerates `config/.system-prompt` from a heredoc on every startup — edits to `.system-prompt` alone get wiped.
- `scripts/gemma_chat.py` — per-message handler. Modes: `text`, `image`, `routine`. Holds the adaptive tool router (`select_tools`), `pick_reaction`, the agentic loop, and parsers for native function-calling + Gemma's `<|tool_call|>` text-format leak.
- `scripts/tools.py` — tool implementations (web, vault, memory, voice, screenshot, files, send).
- `scripts/routines.py` — cron-scheduled background tasks defined in `config/routines.json`. Single-instance via `.routines.pid` + cross-platform liveness check (`scripts/_proc.py`).
- `scripts/autostart.sh` — wrapper for OS autostart (Task Scheduler on Windows, launchd on macOS). Boots LM Studio → loads the configured model → warms voice servers → starts listener → starts watchdog.
- `scripts/health-check.sh` — re-runs every 5 min via Task Scheduler / launchd; restarts any down component.
- `scripts/watchdog.sh` — polls Telegram when the listener is down; wakes via `gerty-lite.sh start` on incoming message.
- `scripts/_paths.py`, `scripts/_proc.py` — cross-platform path/binary discovery and process management. Every other module imports from here instead of hardcoding paths.

## Tools available to the bot

All tool schemas live in `scripts/tools.py:TOOL_SCHEMAS`. The adaptive router (`scripts/gemma_chat.py:select_tools`) picks a subset per message to keep context small:

| Tool | Purpose |
|---|---|
| `web_search` | DuckDuckGo HTML scrape |
| `web_fetch` | Plain HTTP GET, stripped to text |
| `browser_open` | Stealth headless browser (camofox at `localhost:9377`) — for JS-heavy / scraper-blocked sites |
| `read_file` / `write_file` / `list_folder` | Vault under `VAULT_ROOT` (default `data/vault/`) or notes vault under `NOTES_ROOT` via `obsidian/` prefix. Roots configured in `config/.paths`. |
| `read_pdf` / `send_file` / `move_file` / `delete_file` | Files sandbox under `FILES_ROOT` (default `data/files/`). |
| `save_memory` / `recall_memory` / `list_memory` / `search_memory` / `delete_memory` | Long-term memory under `MEMORY_ROOT` (default `data/memory/`). |
| `run_shell` | Bash command (Git Bash on Windows, /bin/bash elsewhere) |
| `send_reaction` | Telegram emoji reaction |
| `send_message` | Fresh Telegram message (used by routines, not normal replies) |
| `send_sticker` | Sticker by mood (lookup in `config/stickers.json`) |
| `take_screenshot` + `send_image` | Screenshot a URL, send the PNG |
| `speak` / `read_aloud` | TTS → Telegram voice message |
| `create_routine` / `list_routines` / `delete_routine` | Manage scheduled prompts |

## camofox-browser

Located at `camofox-browser/`. Started via PM2 as `camofox`. Listens on `localhost:9377`. The `browser_open` tool drives it via the `/tabs` REST API.

Note: on Windows, PM2 cannot run via `npm.cmd` — use `pm2 start server.js` directly. The plugin loader uses `pathToFileURL()` so Windows ESM accepts the local paths.

## Slash commands

Defined in `scripts/gemma_chat.py:main()` before the LLM call:
- `/new` — wipes history
- `/status` — shows loaded model + thinking mode
- `/thinking` — toggles thinking mode in `.thinking-mode`
- `/help` — lists the above

Anything else falls through to the LLM as plain text.

## Routines

`config/routines.json` schema (gitignored — see `config/routines.example.json` for the template):

```json
{
  "chat_id": 123456789,
  "routines": [
    {
      "id": "morning-brief",
      "schedule": "0 8 * * *",
      "enabled": false,
      "one_shot": false,
      "prompt": "Call send_message(\"good morning\")."
    }
  ]
}
```

The routine prompt instructs the model what to do; delivery is via tools (`send_message` for Telegram, `write_file` for vault, or both). `{today}` is replaced with the ISO date when the routine fires.

## Persona / behaviour

The live system prompt lives in `config/.system-prompt` and is regenerated from a heredoc in `scripts/gemma-listener.sh` on every startup. To change persona, edit the heredoc, not `.system-prompt`.

Default behaviours baked in:

- Telegram-only, lowercase casual, short messages, light markdown rendered as Telegram HTML
- Split multi-thought replies with `|||` (the listener fans these into separate Telegram messages)
- Mirror the user's language — never auto-switch
- Be direct and honest; no filler "okay, on it!"
- Reactions are optional flavor — only when the vibe clearly calls for it. Telegram-safe emoji whitelist enforced.
- Approval flow: ask before publishing, deleting files, big vault changes, or git ops. Just do it for small stuff (creating notes, fetching URLs, editing drafts).

## Things NOT to do
- Don't suggest changing permissions or allowing tools.
- Don't make Telegram API calls for menu management (getMyCommands / setMyCommands / deleteMyCommands) — the listener owns those on startup.
- Don't edit `config/.system-prompt` directly — it's regenerated by `gemma-listener.sh`'s heredoc. Edit the heredoc instead.

## LM Studio gotchas

- Default model auto-unloads after idle → first message after idle is slow (cold start). Disable auto-unload in LM Studio settings for the best experience.
- The newer `lms` CLI rejects `--ttl 0` ("must be at least 1"). Omit `--ttl` entirely for "never auto-unload".

## Repo layout

```
gerty-lite/
├── gerty-lite.sh             # manager script
├── setup.py                  # cross-platform first-run wizard
├── camofox-browser/          # stealth browser service (PM2: camofox)
├── admin/                    # FastAPI dashboard at http://127.0.0.1:9090
├── config/
│   ├── .system-prompt        # regenerated by listener heredoc
│   ├── .telegram-config      # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (gitignored)
│   ├── .allowed-chats        # one chat_id per line (gitignored)
│   ├── .paths                # local path overrides (gitignored)
│   ├── .model                # GERTY_MODEL + context settings
│   ├── commands.json         # menu
│   ├── routines.json         # cron + prompts (gitignored)
│   ├── routines.example.json # template — copied on first run
│   ├── stickers.json         # mood → sticker file_ids
│   ├── mcp.json              # MCP server registry
│   └── gerty-lite-rules.md   # this file
├── data/                     # local user data (vault, notes, memory, files)
├── scripts/
│   ├── _paths.py             # cross-platform path/binary discovery
│   ├── _proc.py              # cross-platform process management
│   ├── autostart.sh
│   ├── gemma-listener.sh
│   ├── gemma_chat.py
│   ├── tools.py
│   ├── routines.py
│   ├── health-check.sh
│   ├── watchdog.sh
│   └── install-autostart.py  # writes Task Scheduler entry (Windows) or launchd .plist (macOS)
├── .history/                 # per-chat JSON history files (gitignored)
├── .listener.lock/           # listener single-instance lock dir
├── .routines.pid             # routines daemon PID
├── .thinking-mode            # "on" or "off"
├── .gemma-offset             # last Telegram update_id processed
├── .processed-ids            # claimed update_ids (dedupe)
└── .tmp/                     # transient files
```

## Path config (`config/.paths`)

All data roots are configured in `config/.paths` (gitignored). Defaults from `.paths.template`:

- `VAULT_ROOT=./data/vault` — free-form vault (inbox, drafts, published, resources, templates)
- `NOTES_ROOT=./data/notes` — your structured notes (optional Obsidian vault)
- `MEMORY_ROOT=./data/memory` — the bot's persistent memory
- `FILES_ROOT=./data/files` — guardrailed sandbox for user-uploaded files (PDFs, attachments)

Override any of these to point at an existing Obsidian vault, iCloud Drive folder, etc. The setup wizard writes a starting `.paths` file for you.
