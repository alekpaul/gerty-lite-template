# Troubleshooting

Common failure modes, in roughly the order they show up.

## Bot doesn't respond to Telegram messages

1. `bash gerty-lite.sh status` — is the listener running?
   - "Not running" → `bash gerty-lite.sh start`
   - "Running (PID …)" but no reply → keep reading
2. `curl -s http://127.0.0.1:1234/v1/models` — is LM Studio reachable?
   - Empty / refused → open the LM Studio app, or run `lms server start`
3. Is the configured model actually loaded?
   - Check the admin **Models** tab, or `lms ls`
   - Click **Load** on the model in the admin Models tab if it's not loaded
4. `tail -40 gerty-lite.log` — any errors?
   - `[llm] error: timed out` repeatedly → see "Inference times out" below
   - `[blocked] message from unknown chat` → your chat id isn't in
     `config/.allowed-chats`

## Inference times out

Usually VRAM-related. The admin **Resources** tab tells you instantly.

- VRAM > 95% → too many models loaded. Open admin **Models** tab,
  click **Unload all**, then load only the one you want.
- VRAM fine but still timing out → the model is genuinely slow. Try a
  smaller context (e.g. `GERTY_CONTEXT=16384` in `config/.model`) and
  restart the listener.
- LM Studio auto-unloaded the model after idle → in LM Studio settings,
  disable auto-unload. Or accept the 5-second cold-start on first message.

## Voice messages don't transcribe

- `ffmpeg` missing from `PATH`: `which ffmpeg` (mac) / `where ffmpeg`
  (Windows). Install: `brew install ffmpeg` or `winget install Gyan.FFmpeg`.
- `faster-whisper` model not downloaded: it auto-downloads on first use;
  give it a minute on the first voice message after install.

## `speak()` doesn't make audio

- No voice engine configured. The admin **Voice** tab lists what's available.
  If empty, install Kokoro or OmniVoice and point GERTY at them:
  - Kokoro: set `GERTY_KOKORO_TTS` in `config/.env`
  - OmniVoice: start its server (default port 8883) and pick a voice in
    the admin

## camofox keeps dying

- Check `pm2 logs camofox` for the actual error
- On Windows, the plugin loader needs ESM-safe paths (already patched in
  this repo; verify `camofox-browser/server.js` has `pathToFileURL()`)
- Restart: `pm2 restart camofox`

## Admin dashboard shows stale data

The HTML is served with `Cache-Control: no-store` and assets are
mtime-versioned (`?v=…`). If you still see stale content, force-reload
the page once: `Ctrl + Shift + R`.

## Port already in use

- 1234 → LM Studio (don't run two instances)
- 8880 → Kokoro TTS
- 8883 → OmniVoice
- 9090 → admin dashboard
- 9377 → camofox-browser
- 8901 → gerty-live (sibling repo, optional)

Find what's holding a port:

```bash
# macOS
lsof -nP -iTCP:9090 -sTCP:LISTEN
# Windows
netstat -ano | findstr ":9090"
```

Kill it via the admin dashboard's component card, or:

```bash
python -c "import sys; sys.path.insert(0,'scripts'); from _proc import pid_on_port, kill_pid; p = pid_on_port(9090); print('killed' if kill_pid(p) else 'nope')"
```

## "lms CLI not found"

- macOS: open LM Studio → Developer → "Install LM Studio's CLI"
- Windows: same path
- Alternative: set `GERTY_LMS=/absolute/path/to/lms` in `config/.env` (or
  in your shell) and re-run

## Autostart didn't fire after reboot

- macOS: `launchctl list | grep gerty` — both `com.gerty.lite.autostart`
  and `com.gerty.lite.healthcheck` should be listed. Re-run
  `python scripts/install-autostart.py` if missing.
- Windows: `schtasks /Query /TN "Gerty Autostart"` — should show "Ready".
  Re-run the installer if missing.
- View autostart output: `tail -50 autostart.log` in the repo root.

## "Permission denied" on `data/`

- Either the repo isn't writable by your user, or `VAULT_ROOT` points at
  a path you can't reach (iCloud / sync conflict). Set
  `VAULT_ROOT=./data/vault` in `config/.paths` to use the in-repo
  fallback.

## I broke something. Reset?

Nothing GERTY does is destructive outside its own data folders. To reset:

```bash
bash gerty-lite.sh stop
rm -f .gerty-lite.pid .listener.lock .routines.pid .thinking-mode \
      .gemma-offset .processed-ids .restart-flag
bash gerty-lite.sh start
```

Conversation history lives in `.history/`. Delete a chat with the admin
**Chats** tab or:

```bash
rm .history/chat_<your-chat-id>.json
```

Memory entries are markdown files under `MEMORY_ROOT` (default
`./data/memory/entries/`). Delete individual `.md` files or the whole
directory.
