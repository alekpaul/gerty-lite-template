@config/gerty-lite-rules.md

# GERTY — Agent Manifest

This file is auto-loaded by Claude Code when working in this repo. It
defers to `config/gerty-lite-rules.md` for the full project context
(architecture, tools, persona, repo layout).

## Where the bot lives

This repo is the GERTY Lite Telegram bot. After `python setup.py` it:

- Reads/writes a free-form **vault** at `VAULT_ROOT` (default `./data/vault/`)
- Reads/writes a **notes vault** at `NOTES_ROOT` (default `./data/notes/`) —
  typically pointed at an Obsidian vault on iCloud / Dropbox / Syncthing
- Stores **memory** under `MEMORY_ROOT` (default `./data/memory/`)
- Sandboxes **user files** under `FILES_ROOT` (default `./data/files/`)

All four paths are configured in `config/.paths` (gitignored). The setup
wizard writes a starting `.paths` file pointing at the in-repo defaults.

## Working in this repo

- **Run** with `bash gerty-lite.sh start`. Admin dashboard at
  `http://127.0.0.1:9090` (`python admin/server.py`).
- **Cross-platform** — macOS + Windows. Path/process abstractions live in
  `scripts/_paths.py` and `scripts/_proc.py`. Don't hardcode `D:/...` or
  `/c/Users/...`.
- **System prompt** is regenerated from a heredoc in
  `scripts/gemma-listener.sh` on every listener start. Edits to
  `config/.system-prompt` get wiped. Edit the heredoc, not the file.
- **Personal data** (telegram token, chat allowlist, vault contents, chat
  history) is gitignored. The setup wizard never writes secrets to
  tracked files.
