# Onboarding — your first hour with GERTY

The whole point of `python setup.py` is to make this short. If something
goes wrong, this doc tells you what state the wizard expects and how to
recover.

Estimated time: 15 minutes if LM Studio + a model are already installed,
60 minutes if you're downloading a model from scratch.

---

## Prerequisites (10 minutes)

You need:

- **Python 3.10+**
  - macOS: `brew install python@3.12`, or download from python.org
  - Windows: `winget install Python.Python.3.12`, or download from python.org
  - Verify: `python --version`
- **LM Studio** (free) — https://lmstudio.ai
  - macOS: download the `.dmg`, drag to Applications, open the app once,
    then go to Developer → "Install LM Studio's CLI" (`lms`)
  - Windows: download the `.exe`, install, open the app once, go to
    Developer → "Install LM Studio's CLI"
  - Verify: `lms --version`
- **Git** (or download the zip if you don't have git)
- **A Telegram account** on your phone
- **A GPU with ≥ 8 GB VRAM** (for a Gemma 4B vision model) — 24 GB if you
  want a 26B chat model
- **Optional but recommended**: `ffmpeg` if you'll use voice messages
  - macOS: `brew install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg`

---

## Step 1 — Get the code (1 minute)

```bash
git clone <your-fork-url> gerty-lite
cd gerty-lite
```

Put the repo wherever is convenient. Path is auto-discovered now — no
more `D:/` lock-in. macOS users typically use `~/gerty-lite`; Windows
users any drive works.

---

## Step 2 — Download a model in LM Studio (5–45 minutes)

Open the LM Studio app and download:

- **Chat model**: `gemma-4-26b-a4b-it-uncensored` (16 GB) or any Gemma
  26B-class variant from the LM Studio search. If your VRAM is tighter,
  pick `gemma-3-12b-it` (~7 GB) and accept slower / weaker replies.
- **Vision model**: `gemma-3-4b-it` (~3 GB). Used only when you send the
  bot a photo. JIT-loaded; auto-unloads after 5 minutes of idle.

Wait for both downloads to finish. They live in `~/.lmstudio/models/`.

Load the chat model once (Chat tab → select → load) so it appears in
`lms ls`.

---

## Step 3 — Create a Telegram bot (3 minutes)

1. Open Telegram, search for `@BotFather`, click Start.
2. Send `/newbot`.
3. Pick a name (shown to chatters) and username (must end in `bot`).
4. BotFather replies with a token: `123456:ABCdef…`. **Copy it.**
5. Search for `@userinfobot`, click Start. It replies with your numeric
   ID like `987654321`. **Copy it.**

---

## Step 4 — Run the wizard (2 minutes)

```bash
cd gerty-lite
python setup.py
```

The wizard asks:

| Prompt | Paste this |
|---|---|
| Main chat model id | the id from `lms ls` (e.g. `gemma-4-26b-a4b-it-uncensored`) |
| Context length | `32768` is safe; bigger if your GPU has the room |
| TELEGRAM_BOT_TOKEN | the BotFather token |
| TELEGRAM_CHAT_ID | your numeric ID from `@userinfobot` |
| Vault, notes, memory, files paths | press Enter to accept defaults (`./data/...`) |
| Enable Kokoro TTS? | `n` (you can add it later) |
| Enable camofox? | `n` (you can add it later) |

The wizard writes:

```
config/.telegram-config     # secret — gitignored
config/.allowed-chats       # your chat_id — gitignored
config/.paths               # data folder locations — gitignored
config/.model               # which LM Studio model to use
config/routines.json        # seeded from routines.example.json — gitignored
data/{vault,notes,memory,files}/  # scaffold
```

If you re-run `python setup.py`, it skips fields you already filled.

---

## Step 5 — Boot it (30 seconds)

```bash
bash gerty-lite.sh start
```

The manager:

1. Checks LM Studio is reachable (`http://127.0.0.1:1234`)
2. Starts `gemma-listener.sh` in the background
3. Returns the message PID

Verify:

```bash
bash gerty-lite.sh status
```

Should print `Running (winpid <N>)` (Windows) or `Running (PID <N>)` (mac).

Open Telegram, message your bot. First reply takes about 5 seconds while
the model warms up.

---

## Step 6 — Open the admin dashboard (30 seconds)

```bash
python admin/server.py
```

Open `http://127.0.0.1:9090` in your browser. You'll see:

- **Overview** — every component's health
- **Models** — what LM Studio has loaded; you can swap models from here
- **Stats** — token usage, calls, peak prompt size
- **Resources** — VRAM, RAM, CPU, disk
- **Routines** — scheduled prompts (the `dream` routine consolidates memory
  every night at 03:30)
- **Memory** — every fact the bot has saved, with filters
- **Chats** — your conversation history, with per-turn delete
- **Files** — sandboxed file storage
- **Voice** — pick a TTS voice
- **MCP** — Model Context Protocol server registry

Leave the admin server running in a terminal or close it — the bot keeps
running either way.

---

## Step 7 — Make it boot on login (1 minute)

```bash
python scripts/install-autostart.py
```

- **macOS**: writes `~/Library/LaunchAgents/com.gerty.lite.autostart.plist`
  + a 5-minute health-check plist. Loaded immediately.
- **Windows**: registers Task Scheduler entries `Gerty Autostart` (logon
  trigger) and `Gerty Health Check` (every 5 min).

Verify autostart works by logging out and back in (or rebooting). The
bot should be reachable within ~30 seconds of login.

To uninstall:

```bash
python scripts/install-autostart.py uninstall
```

---

## What to try first

Once your bot is alive, message it:

| Try this | What it does |
|---|---|
| `/status` | Shows loaded model + thinking mode |
| `/help` | Lists slash commands |
| `/new` | Wipes conversation history |
| `/thinking` | Toggle Gemma's reasoning mode (slower, smarter) |
| Send a photo | Vision model describes it, main model replies |
| Send a PDF | Saved to `data/files/inbox/`, accessible via `read_pdf` |
| `save this: my favourite color is teal` | Bot calls `save_memory` |
| `what colour did I tell you?` (next day) | Bot calls `recall_memory` |
| `find me good headphones under $200` | `web_search` → `web_fetch` |
| `read me the news` (with TTS enabled) | `web_fetch` + `speak` |

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Common issues:

- **"LM Studio API not reachable"** — open the LM Studio app or run
  `lms server start`
- **No reply on Telegram** — check `bash gerty-lite.sh status`; if it
  shows "Not running", re-run `bash gerty-lite.sh start`
- **`speak()` doesn't work** — voice engines are optional. Either install
  OmniVoice / Kokoro and set the path via the admin Voice tab, or just
  ignore voice tools
- **Permission errors writing to data/** — check your shell's working
  directory matches the repo root, or set `VAULT_ROOT` to an absolute
  path in `config/.paths`

---

## What's next

- Edit `scripts/gemma-listener.sh` (the heredoc near the top) to customize
  your bot's persona / system prompt
- Add routines via the admin Routines tab or by editing
  `config/routines.json` directly
- Wire up MCP servers (Filesystem, Memory, custom) via `config/mcp.json`
  and the admin MCP tab
- Optional: clone `gerty-live` next to this repo to add realtime voice
  chat via a WebSocket UI
