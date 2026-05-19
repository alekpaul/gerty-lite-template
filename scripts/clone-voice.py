#!/usr/bin/env python3
"""
Voice-clone helper. Shared by the Telegram listener and the browser UI.

Workflow:
  1. Source the audio (download by Telegram file_id, or use a local path)
  2. Convert to 24 kHz mono WAV (OmniVoice expects this)
  3. Transcribe via /d/Claude/scripts/transcribe-audio.py (faster-whisper auto-detect)
  4. Save <name>[.wav|-uk.wav] + matching .txt under D:/gerty-lite/.tts-refs/
  5. POST /v1/refresh so the new ref is usable immediately
  6. (Telegram path only) send a confirmation message
  7. For lang=both: after saving the EN clip, re-arm .clone-pending.json with phase=uk

Usage:
  clone-voice.py <chat_id> <file_id-or-path> <name> <tg_token> [lang] [gender]
    lang   = "en" (default) | "uk" | "both"
    gender = "feminine" | "masculine" | "unknown" (default)
             When set to feminine/masculine, the clone is registered in
             .voice-genders.json so gemma_chat.py can inject the right
             Ukrainian grammar directive when the voice is active.
    file_id-or-path: either a Telegram file_id (no "/" or "\\"), or an absolute
                     local path (or file:// URL) to an audio file. When a local
                     path is given the Telegram download step is skipped.

  chat_id can be "-" when invoked from the browser UI (no Telegram messaging).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
REFS_DIR = _REPO_ROOT / ".tts-refs"
PENDING_FILE = _REPO_ROOT / ".clone-pending.json"
OMNIVOICE_URL = "http://127.0.0.1:8883"
# Bundled transcribe script lives next to this file. Override via GERTY_TRANSCRIBE
# if you want to point at a different STT script (e.g. one tuned for your hardware).
TRANSCRIBE_SCRIPT = os.environ.get("GERTY_TRANSCRIBE",
                                    str(Path(__file__).resolve().parent / "transcribe-audio.py"))
# Use the same Python that's running this script — no hardcoded interpreter path.
PYTHON_EXE = sys.executable


def tg_send_text(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id or chat_id == "-":
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[clone] tg send error: {e}", file=sys.stderr)


def tg_download_voice(token: str, file_id: str, dest_path: str) -> bool:
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getFile?file_id={urllib.parse.quote(file_id)}"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.loads(r.read())
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return False
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        urllib.request.urlretrieve(url, dest_path)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception as e:
        print(f"[clone] download error: {e}", file=sys.stderr)
        return False


def convert_to_wav_24k_mono(in_path: str, out_path: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path,
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", out_path],
            capture_output=True, timeout=60,
        )
        print(f"[clone] ffmpeg rc={result.returncode} exists={os.path.exists(out_path)}", flush=True)
        if result.returncode != 0:
            print(f"[clone] ffmpeg stderr: {result.stderr.decode('utf-8', errors='replace')[:500]}", flush=True)
        return result.returncode == 0 and os.path.exists(out_path)
    except Exception as e:
        print(f"[clone] ffmpeg error: {e}", file=sys.stderr, flush=True)
        return False


def transcribe(audio_path: str) -> str | None:
    """Call transcribe-audio.py directly (auto-detects language)."""
    script = TRANSCRIBE_SCRIPT
    # Accept MSYS-style /c/... or /d/... paths in case a user set GERTY_TRANSCRIBE
    # from inside Git Bash on Windows.
    if script.startswith("/") and len(script) > 2 and script[2] == "/":
        script = script[1].upper() + ":" + script[2:].replace("/", "\\")
    try:
        result = subprocess.run(
            [PYTHON_EXE, script, audio_path],
            capture_output=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"[clone] transcribe rc={result.returncode} stderr={result.stderr[:300]}",
                  file=sys.stderr, flush=True)
        text = (result.stdout or "").strip()
        return text if text else None
    except Exception as e:
        print(f"[clone] transcribe error: {e}", file=sys.stderr, flush=True)
        return None


def refresh_omnivoice() -> bool:
    """Tell the running OmniVoice server to rescan .tts-refs/ for new voices."""
    try:
        req = urllib.request.Request(f"{OMNIVOICE_URL}/v1/refresh", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception as e:
        print(f"[clone] refresh failed ({e}); falling back to pm2 restart", file=sys.stderr)
        try:
            subprocess.run(["pm2", "restart", "omnivoice"], check=True, timeout=15,
                           capture_output=True, shell=True)
            return True
        except Exception as e2:
            print(f"[clone] pm2 restart also failed: {e2}", file=sys.stderr)
            return False


def looks_like_local_path(s: str) -> bool:
    """Distinguish a local audio path from a Telegram file_id."""
    if s.startswith("file://"):
        return True
    # Telegram file_ids are URL-safe base64-ish and don't contain path separators
    if "/" in s or "\\" in s or ":" in s[:3]:
        return True
    return False


def local_path_from_arg(s: str) -> str:
    if s.startswith("file:///"):
        return urllib.parse.unquote(s[len("file:///"):])
    if s.startswith("file://"):
        return urllib.parse.unquote(s[len("file://"):])
    return s


def sanitize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30]


def main():
    if len(sys.argv) < 5:
        print("usage: clone-voice.py <chat_id> <file_id-or-path> <name> <tg_token> [lang] [gender]",
              file=sys.stderr)
        sys.exit(2)
    chat_id = sys.argv[1]
    source = sys.argv[2]
    name = sys.argv[3]
    token = sys.argv[4]
    lang = (sys.argv[5] if len(sys.argv) >= 6 else "en").lower()
    if lang not in ("en", "uk", "both"):
        print(f"[clone] invalid lang '{lang}', defaulting to en", file=sys.stderr)
        lang = "en"
    gender = (sys.argv[6] if len(sys.argv) >= 7 else "unknown").lower()
    if gender not in ("feminine", "masculine", "unknown"):
        print(f"[clone] invalid gender '{gender}', defaulting to unknown", file=sys.stderr)
        gender = "unknown"

    print(f"[clone] start: chat={chat_id} name={name} lang={lang} gender={gender} source={source[:40]}...", flush=True)

    safe_name = sanitize_name(name)
    if not safe_name:
        tg_send_text(token, chat_id, "❌ invalid name for clone — use letters/numbers only")
        sys.exit(1)

    # Decide save target by language.
    # lang=both starts with the EN slot, then re-arms pending for the UK phase.
    save_lang = "en" if lang in ("en", "both") else "uk"
    suffix = "" if save_lang == "en" else "-uk"

    REFS_DIR.mkdir(parents=True, exist_ok=True)
    target_wav = REFS_DIR / f"{safe_name}{suffix}.wav"
    target_txt = REFS_DIR / f"{safe_name}{suffix}.txt"
    print(f"[clone] paths: wav={target_wav} txt={target_txt}", flush=True)

    # 1. Get the source audio file onto disk as a temp.
    tmp_input = None
    if looks_like_local_path(source):
        # Browser-UI path: use the file directly.
        src_path = local_path_from_arg(source)
        if not os.path.exists(src_path) or os.path.getsize(src_path) == 0:
            tg_send_text(token, chat_id, "❌ source file missing or empty")
            print(f"[clone] local source missing: {src_path}", file=sys.stderr)
            sys.exit(1)
        print(f"[clone] using local source: {src_path} ({os.path.getsize(src_path)} bytes)", flush=True)
    else:
        tg_send_text(token, chat_id, f"cloning '{safe_name}' ({save_lang})... downloading audio")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_input = tmp.name
        if not tg_download_voice(token, source, tmp_input):
            tg_send_text(token, chat_id, "couldn't download your audio from telegram")
            try: os.remove(tmp_input)
            except Exception: pass
            sys.exit(1)
        src_path = tmp_input
        print(f"[clone] downloaded {os.path.getsize(src_path)} bytes", flush=True)

    # 2. Convert to 24 kHz mono WAV
    if not convert_to_wav_24k_mono(src_path, str(target_wav)):
        if tmp_input:
            try: os.remove(tmp_input)
            except Exception: pass
        tg_send_text(token, chat_id, "❌ ffmpeg conversion failed")
        sys.exit(1)
    if tmp_input:
        try: os.remove(tmp_input)
        except Exception: pass

    # 3. Transcribe
    tg_send_text(token, chat_id, "🎙 transcribing reference...")
    text = transcribe(str(target_wav))
    if not text:
        tg_send_text(token, chat_id, "❌ transcription failed — try a clearer recording")
        sys.exit(1)
    target_txt.write_text(text, encoding="utf-8")

    # 4. Refresh server
    tg_send_text(token, chat_id, "reloading OmniVoice with your voice...")
    refresh_omnivoice()

    # 5. Handle lang=both phase transition or finalize
    if lang == "both" and save_lang == "en":
        # Re-arm pending for the UK phase. Same chat, same name, same gender,
        # fresh 5-min window. Gender is carried through so the UK recording
        # doesn't need to re-prompt.
        try:
            PENDING_FILE.write_text(
                json.dumps({
                    "chat_id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id,
                    "name": safe_name,
                    "lang": "uk",
                    "gender": gender,
                    "expires_at": int(time.time()) + 300,
                }, indent=2),
                encoding="utf-8",
            )
            tg_send_text(
                token, chat_id,
                f"✅ English reference saved as 'omni_{safe_name}'\n\n"
                f"transcript: {text[:200]}\n\n"
                "now send a 5–15 second Ukrainian recording (or upload an audio file) "
                "to add the Ukrainian variant. window: 5 min."
            )
        except Exception as e:
            tg_send_text(token, chat_id, f"saved EN but failed to re-arm UK phase: {e}")
        return

    # Clear the pending file (clone fully completed) — best-effort
    try:
        if PENDING_FILE.exists():
            PENDING_FILE.unlink()
    except Exception:
        pass

    # Persist gender override so the model uses correct Ukrainian grammar when
    # this voice is the active TTS voice. tools.set_voice_gender handles the
    # JSON file at .voice-genders.json — see tools.py:_voice_gender resolution.
    if gender in ("feminine", "masculine"):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from tools import set_voice_gender
            msg = set_voice_gender(f"omni_{safe_name}", gender)
            print(f"[clone] {msg}", flush=True)
        except Exception as e:
            print(f"[clone] could not persist gender: {e}", file=sys.stderr, flush=True)

    # 6. Confirm
    lang_label = "Ukrainian" if save_lang == "uk" else "English"
    gender_note = f", {gender}" if gender in ("feminine", "masculine") else ""
    tg_send_text(
        token, chat_id,
        f"✅ cloned as 'omni_{safe_name}' ({lang_label}{gender_note})\n"
        f"transcript: {text[:200]}\n\n"
        f"try it:  /voice sample omni_{safe_name}\n"
        f"set as default:  /voice omni_{safe_name}"
    )


if __name__ == "__main__":
    main()
