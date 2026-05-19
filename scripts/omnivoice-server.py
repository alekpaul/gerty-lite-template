#!/usr/bin/env python3
"""
OmniVoice HTTP server.

Loads OmniVoice once (k2-fsa/OmniVoice, Qwen3-0.6B base). Exposes a tiny
POST /v1/speak endpoint that auto-picks the right reference clip based on
the language of the text:
  - Cyrillic text  -> uses <name>-uk.wav as the voice reference (if present)
  - Else           -> uses <name>.wav (English reference)

References live under D:/gerty-lite/.tts-refs/ (same dir NeuTTS uses).

Endpoints:
  GET  /health
  POST /v1/refresh    -- rescan refs dir for new wav+txt pairs
  POST /v1/speak      -- body: {text, voice_id, speed, format}
"""

import argparse
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import soundfile as sf
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = SCRIPT_DIR.parent
REFS_DIR = _REPO_ROOT / ".tts-refs"
UI_HTML = SCRIPT_DIR / "omnivoice-ui.html"
CLONE_SCRIPT = SCRIPT_DIR / "clone-voice.py"
# Reuse this server's own Python — works on any OS without hardcoded paths.
PYTHON_EXE = sys.executable

print("[omnivoice] loading model...", flush=True)
import torch
from omnivoice import OmniVoice

_device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[omnivoice] device={_device}", flush=True)

try:
    # bfloat16: same memory as float16 (~1.2 GB on 4090) but float32-equivalent
    # exponent range, avoiding the silent-audio NaN issue we hit with fp16.
    _dtype = torch.bfloat16 if _device == "cuda" else torch.float32
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", torch_dtype=_dtype)
    if _device == "cuda":
        model = model.to(_device)
    print(f"[omnivoice] loaded on {_device} ({_dtype})", flush=True)
except Exception as e:
    print(f"[omnivoice] cuda load failed ({e}); falling back to CPU", flush=True)
    _device = "cpu"
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", torch_dtype=torch.float32)

_sample_rate = getattr(model, "sample_rate", None) or 24000


# References cache: { "oleh": {"en": (wav, text), "uk": (wav, text)} }
refs: dict[str, dict[str, tuple[str, str]]] = {}


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _load_refs() -> None:
    refs.clear()
    if not REFS_DIR.exists():
        return
    for wav in sorted(REFS_DIR.glob("*.wav")):
        stem = wav.stem
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            continue
        # Stems can be "oleh" (EN) or "oleh-uk" (UK). Split that out.
        if stem.endswith("-uk"):
            base = stem[:-3]
            lang = "uk"
        elif stem.endswith("-en"):
            base = stem[:-3]
            lang = "en"
        else:
            base = stem
            lang = "en"
        refs.setdefault(base, {})[lang] = (str(wav), txt.read_text(encoding="utf-8").strip())
    print(f"[omnivoice] loaded refs: {list(refs.keys())}", flush=True)


_load_refs()


app = FastAPI()


class SpeakRequest(BaseModel):
    text: str
    voice_id: str = "oleh"  # base name; server auto-picks -uk or default
    format: str = "ogg"
    speed: float = 1.0
    force_lang: str | None = None  # "en" / "uk" / None for auto-detect


@app.get("/health")
def health():
    return {
        "ok": True,
        "device": _device,
        "voices": sorted(refs.keys()),
        "lang_refs": {k: list(v.keys()) for k, v in refs.items()},
    }


@app.post("/v1/refresh")
def refresh():
    _load_refs()
    return {"ok": True, "voices": sorted(refs.keys())}


# ─── Browser UI ────────────────────────────────────────────────────────────────
# A tiny single-page app for managing voice clones: upload an audio file or
# record from the mic, name it, pick a language, save. Lives at GET /.

@app.get("/")
def ui_root():
    """Serve the management UI. Re-read on every request so HTML edits are live."""
    if not UI_HTML.exists():
        raise HTTPException(404, f"UI file missing at {UI_HTML}")
    return FileResponse(str(UI_HTML), media_type="text/html")


@app.get("/v1/voices")
def list_voices_endpoint():
    """List all clones with their language variants and transcripts."""
    out = []
    for name in sorted(refs.keys()):
        variants = refs[name]
        entry = {
            "id": name,
            "langs": sorted(variants.keys()),
            "transcripts": {lang: text for lang, (_, text) in variants.items()},
        }
        out.append(entry)
    return {"voices": out}


@app.post("/v1/clone")
def clone_endpoint(
    file: UploadFile = File(...),
    name: str = Form(...),
    lang: str = Form("en"),
):
    """Accept an uploaded audio file, save it to a temp path, then hand off to
    clone-voice.py which converts, transcribes, saves into .tts-refs/, and
    triggers /v1/refresh. Returns JSON with the result and the new voice list."""
    if lang not in ("en", "uk"):
        # 'both' makes no sense from the browser UI (one file at a time) — the
        # user uploads one EN and one UK separately under the same name.
        raise HTTPException(400, "lang must be 'en' or 'uk'")
    if not name.strip():
        raise HTTPException(400, "name required")

    # Preserve the upload's extension when known so ffmpeg can sniff the format.
    orig_name = file.filename or "upload.bin"
    ext = os.path.splitext(orig_name)[1].lower() or ".bin"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name
        try:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        finally:
            file.file.close()

    try:
        # Shell out to clone-voice.py with chat_id="-" (no Telegram messaging),
        # local path as source, dummy token. Same code path as the Telegram side.
        proc = subprocess.run(
            [PYTHON_EXE, str(CLONE_SCRIPT), "-", tmp_path, name, "-", lang],
            capture_output=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return JSONResponse(
                {"ok": False, "error": "clone failed",
                 "stderr": (proc.stderr or "")[-2000:],
                 "stdout": (proc.stdout or "")[-2000:]},
                status_code=500,
            )
    finally:
        try: os.remove(tmp_path)
        except Exception: pass

    # Refresh in-process so the new voice is immediately available locally too
    # (clone-voice.py also POSTs /v1/refresh, but redundancy here is harmless).
    _load_refs()
    return {
        "ok": True,
        "voice": name,
        "lang": lang,
        "voices": [
            {"id": n, "langs": sorted(refs[n].keys())}
            for n in sorted(refs.keys())
        ],
    }


class DeleteRequest(BaseModel):
    name: str
    lang: str | None = None  # None or "all" → delete both variants


@app.post("/v1/delete")
def delete_endpoint(req: DeleteRequest):
    """Delete a voice's reference files. lang=None deletes both EN+UK variants."""
    if not req.name.strip():
        raise HTTPException(400, "name required")
    name = req.name.strip()
    targets = []
    if req.lang in (None, "all"):
        targets += [REFS_DIR / f"{name}.wav", REFS_DIR / f"{name}.txt",
                    REFS_DIR / f"{name}-uk.wav", REFS_DIR / f"{name}-uk.txt"]
    elif req.lang == "en":
        targets += [REFS_DIR / f"{name}.wav", REFS_DIR / f"{name}.txt"]
    elif req.lang == "uk":
        targets += [REFS_DIR / f"{name}-uk.wav", REFS_DIR / f"{name}-uk.txt"]
    else:
        raise HTTPException(400, "lang must be 'en', 'uk', or 'all'")

    removed = []
    for p in targets:
        if p.exists():
            try:
                p.unlink()
                removed.append(p.name)
            except Exception as e:
                print(f"[omnivoice] delete {p}: {e}", flush=True)
    _load_refs()
    return {"ok": True, "removed": removed, "voices": sorted(refs.keys())}


@app.post("/v1/speak")
def speak(req: SpeakRequest):
    voice = req.voice_id
    if voice not in refs:
        raise HTTPException(404, f"unknown voice: {voice}. Available: {sorted(refs.keys())}")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")

    # Pick the right reference: forced lang > Cyrillic detection > default
    lang = req.force_lang
    if not lang:
        lang = "uk" if _has_cyrillic(text) else "en"
    voice_refs = refs[voice]
    ref_wav, ref_text = voice_refs.get(lang) or voice_refs.get("en") or next(iter(voice_refs.values()))

    print(f"[omnivoice] speak voice={voice} lang={lang} ref={Path(ref_wav).name} len={len(text)}", flush=True)
    try:
        t0 = time.time()
        kwargs = dict(text=text, ref_audio=ref_wav, ref_text=ref_text)
        # Pass language hint if explicit
        if lang in ("en", "uk"):
            kwargs["language"] = lang
        result = model.generate(**kwargs)
        audio = result[0] if isinstance(result, list) else result
        if hasattr(audio, "cpu"):
            audio = audio.cpu().numpy()
        dt = time.time() - t0
        print(f"[omnivoice]   generated in {dt:.2f}s", flush=True)
    except Exception as e:
        raise HTTPException(500, f"inference failed: {e}")

    # WAV -> OGG/Opus with optional speed adjustment
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_tmp:
        sf.write(wav_tmp.name, audio, _sample_rate)
        wav_path = wav_tmp.name

    if req.format.lower() == "wav":
        try:
            with open(wav_path, "rb") as f:
                return Response(f.read(), media_type="audio/wav")
        finally:
            try: os.remove(wav_path)
            except Exception: pass

    ogg_path = wav_path.replace(".wav", ".ogg")
    speed = max(0.5, min(2.0, float(req.speed or 1.0)))
    cmd = ["ffmpeg", "-y", "-i", wav_path]
    if abs(speed - 1.0) > 0.01:
        cmd += ["-filter:a", f"atempo={speed}"]
    cmd += ["-c:a", "libopus", "-b:a", "32k", "-application", "voip", ogg_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"ffmpeg failed: {e.stderr.decode('utf-8', errors='replace')[:300]}")
    finally:
        try: os.remove(wav_path)
        except Exception: pass

    try:
        with open(ogg_path, "rb") as f:
            data = f.read()
    finally:
        try: os.remove(ogg_path)
        except Exception: pass
    return Response(data, media_type="audio/ogg")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8883)
    args = ap.parse_args()
    print(f"[omnivoice] starting on http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
