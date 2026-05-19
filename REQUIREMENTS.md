# Requirements

## Hardware

| Component | Minimum                                  | What you actually want                   |
|-----------|------------------------------------------|------------------------------------------|
| GPU       | NVIDIA, 16 GB VRAM, compute ≥ 7.5        | RTX 3090 / 4090 / 5090 (24 GB)           |
| RAM       | 16 GB                                    | 32 GB                                    |
| Disk      | 60 GB free                               | 150 GB+ (models + sample clips pile up)  |
| Network   | broadband — first run downloads ~30 GB   | unmetered                                |
| Mic       | any USB / built-in                       | something that handles Ukrainian + EN    |

CPU choice barely matters — almost everything is on GPU.

## Operating system

- **Windows 11** (tested). Windows 10 22H2 should work but unverified.
- **Git Bash / MSYS2** installed (the listener + launchers are bash scripts).
- **PowerShell 7+** (for the installer).
- macOS / Linux not supported yet — Windows-specific paths and PM2 quirks throughout.

## Software prerequisites

Install these once before running `install.ps1`:

| Tool                                      | Why                                         | Get it from                                 |
|-------------------------------------------|---------------------------------------------|---------------------------------------------|
| **LM Studio** 0.3+                         | hosts the LLM with an OpenAI-compatible API | https://lmstudio.ai                         |
| **Python 3.12** (via `uv` or directly)    | runs the bot, TTS server, ASR, web server   | https://www.python.org/ or https://astral.sh/uv |
| **Node.js 18+**                            | camofox stealth browser, PM2                | https://nodejs.org                          |
| **Git for Windows** (includes Git Bash)   | clone repos, run bash scripts               | https://git-scm.com                         |
| **PM2** (`npm i -g pm2`)                  | keeps camofox + omnivoice up                | comes with npm                              |
| **ffmpeg**                                 | audio transcode (Telegram OGG → WAV)        | https://www.gyan.dev/ffmpeg/builds/         |
| **NVIDIA driver** + **CUDA Toolkit 12+**  | GPU compute for ASR + TTS                   | https://developer.nvidia.com                |

The installer checks for each of these and tells you what's missing.

## LM Studio setup (one-time, manual)

1. Open LM Studio → Settings → Developer → enable **Local Server**.
2. Search & download a model. Tested:
   - `google/gemma-4-26b-a4b @ q4_k_s` (best fit, ~14 GB VRAM, 24 GB context)
   - `google_gemma-4-26b-a4b-it @ iq4_xs` (lighter, ~14 GB VRAM)
3. Load the model **before** starting Gerty. The installer registers an autostart task that runs `lms load …` on boot — that's enough after the first manual load.

## Telegram bot (one-time, manual)

1. DM [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.
2. DM your new bot once. Find your `chat_id` via [@RawDataBot](https://t.me/RawDataBot) or `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Drop them into `config/.telegram-config` (copy from `.telegram-config.template`).

## VRAM budget — what fits on a 24 GB card

| Component                       | VRAM       |
|---------------------------------|------------|
| Gemma 26B Q4_K_S                | ~14.0 GB   |
| OmniVoice TTS                   | ~2.35 GB   |
| faster-whisper `small` fp16     | ~0.9 GB    |
| Windows + browser baseline      | ~1.4 GB    |
| **Total**                       | **~18.7 GB** — leaves 5 GB headroom |

For tighter quants or larger models, see [voice-vram-budget reference](https://github.com/alekpaul/gerty-lite/wiki) (TODO).
