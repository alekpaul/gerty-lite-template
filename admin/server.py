"""
GERTY admin dashboard.

Single FastAPI app on http://127.0.0.1:9090 with a status view of every
component (LM Studio, OmniVoice, Kokoro, camofox, gerty-lite bot, gerty-live
WS server, cloudflared tunnel) plus restart buttons, log tails, the current
voice selection, and a "resend magic link" action.

Bound to 127.0.0.1 only — no auth (assumed local). Don't expose it.

Run with:
    python admin/server.py

Or via the autostart hook the installer registers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

try:
    import psutil  # type: ignore
except ImportError:  # optional dep — falls back to wmic / /proc
    psutil = None


class LlmLoadRequest(BaseModel):
    model: str
    context_length: int | None = None


class LlmUnloadRequest(BaseModel):
    model: str | None = None
    all: bool = False


class RoutineToggle(BaseModel):
    enabled: bool


class McpToggle(BaseModel):
    enabled: bool


class VoiceSet(BaseModel):
    engine: str
    voice: str


class ThinkingSet(BaseModel):
    on: bool


# Cross-platform path/binary discovery — single source of truth, no hardcoded
# /c/Users/<name>/... or D:/... paths in this file. Override via env vars
# (GERTY_DIR, GERTY_LIVE_DIR, GERTY_PYTHON3, GERTY_LMS, GERTY_BASH) if needed.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from _paths import (  # noqa: E402
    repo_root as _repo_root,
    gerty_live_dir as _live_dir,
    find_python as _find_python,
    find_bash as _find_bash,
    find_lms as _find_lms,
    IS_WINDOWS as _IS_WINDOWS,
)

LITE = _repo_root()
LIVE = _live_dir() or (LITE.parent / "gerty-live")  # gerty-live is optional

VOICE_CONFIG    = LITE / ".voice-config.json"
TG_CONFIG       = LITE / "config" / ".telegram-config"
SYSTEM_PROMPT   = LITE / "config" / ".system-prompt"
MODEL_CONFIG    = LITE / "config" / ".model"
LISTENER_SH     = LITE / "scripts" / "gemma-listener.sh"
USAGE_STATS     = LITE / ".usage-stats.json"
ROUTINES_JSON   = LITE / "config" / "routines.json"
MCP_JSON        = LITE / "config" / "mcp.json"
THINKING_FILE   = LITE / ".thinking-mode"
HISTORY_DIR     = LITE / ".history"
PATHS_FILE      = LITE / "config" / ".paths"
PATHS_TEMPLATE  = LITE / "config" / ".paths.template"

# ── Component definitions ───────────────────────────────────────────────────
# Each entry tells the dashboard:
#   - how to probe liveness (URL or PID-file or shell command)
#   - how to start / stop / restart (shell command, run via bash)
#   - which log file to tail
#
# All shell commands run through Git Bash so the existing .sh launchers work.


def _bash() -> str:
    """Locate a POSIX bash. Cross-platform via _paths.find_bash."""
    b = _find_bash()
    if not Path(b).exists():
        raise RuntimeError(
            "bash not found — install Git for Windows (Windows) "
            "or ensure /bin/bash is on PATH (macOS/Linux)"
        )
    return b


# Cross-platform Kokoro lifecycle — uses scripts/_proc.py for port-based PID
# discovery and platform-aware kill (psutil → tasklist on Windows / lsof+kill
# on POSIX). External Kokoro script path comes from $GERTY_KOKORO_TTS; if
# missing the dashboard shows "not configured" instead of trying to launch.
_PYBIN = _find_python()
_LITE = str(LITE).replace("\\", "/")
_KOKORO_SCRIPT = os.environ.get("GERTY_KOKORO_TTS", "")

def _kokoro_start_cmd() -> str:
    if not _KOKORO_SCRIPT:
        return "echo 'kokoro disabled — set GERTY_KOKORO_TTS to enable'"
    return (
        "if curl -sf http://127.0.0.1:8880/health >/dev/null 2>&1; then "
        "echo 'kokoro already running'; "
        "else "
        f"nohup '{_PYBIN}' '{_KOKORO_SCRIPT}' "
        f">> '{_LITE}/kokoro.log' 2>&1 & disown; echo 'kokoro started'; "
        "fi"
    )

def _kokoro_kill_cmd() -> str:
    # Python one-liner that uses _proc.kill_pid + pid_on_port for portability.
    return (
        f"'{_PYBIN}' -c \""
        f"import sys; sys.path.insert(0, r'{_LITE}/scripts'); "
        "from _proc import pid_on_port, kill_pid; "
        "p = pid_on_port(8880); "
        "ok = kill_pid(p) if p else False; "
        "print(f'kokoro stopped (pid {p})' if ok else 'kokoro not running')\""
    )


COMPONENTS: dict[str, dict] = {
    "lm_studio": {
        "label": "LM Studio",
        "kind": "external",
        "probe": {"http": "http://127.0.0.1:1234/v1/models", "timeout": 3},
        "log": None,
        "note": "Open the LM Studio app to load/unload models.",
    },
    "kokoro": {
        "label": "Kokoro TTS",
        "kind": "script",  # not in PM2 — managed via _proc on every platform
        "probe": {"http": "http://127.0.0.1:8880/health", "timeout": 2},
        "start":   _kokoro_start_cmd(),
        "stop":    _kokoro_kill_cmd(),
        "restart": _kokoro_kill_cmd() + " ; sleep 1 ; " + _kokoro_start_cmd(),
        "log": str(LITE / "kokoro.log"),
        "note": None if _KOKORO_SCRIPT else "Set GERTY_KOKORO_TTS env var to enable Kokoro.",
    },
    "omnivoice": {
        "label": "OmniVoice TTS",
        "kind": "pm2",
        "probe": {"http": "http://127.0.0.1:8883/health", "timeout": 2},
        "start": "pm2 start omnivoice",
        "stop": "pm2 stop omnivoice",
        "restart": "pm2 restart omnivoice",
        "log": None,
    },
    "camofox": {
        "label": "camofox browser",
        "kind": "pm2",
        "probe": {"http": "http://127.0.0.1:9377/health", "timeout": 2},
        "start": "pm2 start camofox",
        "stop": "pm2 stop camofox",
        "restart": "pm2 restart camofox",
        "log": None,
    },
    "gerty_lite": {
        "label": "gerty-lite (Telegram bot)",
        "kind": "script",
        "probe": {"cmd": f"bash '{_LITE}/gerty-lite.sh' status",
                  "match": "Running"},
        "restart": f"bash '{_LITE}/gerty-lite.sh' restart",
        "stop":    f"bash '{_LITE}/gerty-lite.sh' stop",
        "start":   f"bash '{_LITE}/gerty-lite.sh' start",
        "log": str(LITE / "gerty-lite.log"),
    },
    "gerty_live": {
        "label": "gerty-live (voice WS)",
        "kind": "script",
        "probe": {"http": "http://127.0.0.1:8901/", "timeout": 2,
                  "accept_codes": [200, 401]},
        "restart": f"bash '{LIVE}/scripts/live-down.sh' && bash '{LIVE}/scripts/live-up.sh'",
        "stop":    f"bash '{LIVE}/scripts/live-down.sh'",
        "start":   f"bash '{LIVE}/scripts/live-up.sh'",
        "log": str(LIVE / "server.log"),
    },
    "cloudflared": {
        "label": "cloudflared tunnel",
        "kind": "pid",
        "probe": {"pid_file": str(LIVE / ".cloudflared.pid")},
        "restart": f"bash '{LIVE}/scripts/live-down.sh' && bash '{LIVE}/scripts/live-up.sh'",
        "log": str(LIVE / "tunnel.log"),
    },
}

# Drop the gerty-live + cloudflared entries entirely when the sibling repo
# isn't installed — keeps the dashboard tidy for users who only want the bot.
if not (LIVE / "scripts" / "live-up.sh").exists():
    COMPONENTS.pop("gerty_live", None)
    COMPONENTS.pop("cloudflared", None)


# ── Probes ──────────────────────────────────────────────────────────────────

def _probe_http(url: str, timeout: float, accept_codes: list[int] | None = None) -> dict:
    accept = set(accept_codes or [200])
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {"status": "up" if r.status in accept else "down",
                    "detail": f"HTTP {r.status}"}
    except urllib.error.HTTPError as e:
        return {"status": "up" if e.code in accept else "down",
                "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "down", "detail": str(e)[:120]}


def _probe_cmd(cmd: str, match: str) -> dict:
    try:
        r = subprocess.run([_bash(), "-c", cmd], capture_output=True, text=True, timeout=8)
        ok = match in (r.stdout + r.stderr)
        return {"status": "up" if ok else "down",
                "detail": r.stdout.strip()[:120]}
    except Exception as e:
        return {"status": "unknown", "detail": str(e)[:120]}


def _probe_pid(pid_file: str) -> dict:
    p = Path(pid_file)
    if not p.exists():
        return {"status": "down", "detail": "no pid file"}
    try:
        pid = int(p.read_text().strip())
        # signal 0 just checks existence
        os.kill(pid, 0)
        return {"status": "up", "detail": f"pid {pid}"}
    except Exception:
        return {"status": "down", "detail": "stale pid"}


def probe(comp: dict) -> dict:
    pr = comp.get("probe", {})
    if "http" in pr:
        return _probe_http(pr["http"], pr.get("timeout", 3), pr.get("accept_codes"))
    if "cmd" in pr:
        return _probe_cmd(pr["cmd"], pr.get("match", ""))
    if "pid_file" in pr:
        return _probe_pid(pr["pid_file"])
    return {"status": "unknown", "detail": "no probe configured"}


# ── Shell action helper ─────────────────────────────────────────────────────

def run_bash(cmd: str, timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            [_bash(), "-c", cmd], capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        # stdout / stderr can be None when bash exits before writing; coerce
        # to empty string so concat never explodes (this was returning ok=false
        # on PM2 stop/start commands even when the action actually succeeded).
        out = ((r.stdout or "") + (r.stderr or ""))[-4000:]
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


# ── FastAPI ─────────────────────────────────────────────────────────────────

app = FastAPI(title="GERTY admin")


from fastapi.responses import HTMLResponse  # used by index() below


@app.get("/")
async def index():
    """Serve index.html with mtime-based cache-busting on app.js / style.css
    and Cache-Control: no-store. Stops browsers from showing stale UI after
    edits — the asset URLs change whenever the files change."""
    try:
        html = (HERE / "index.html").read_text(encoding="utf-8")
        js_v  = int((HERE / "app.js").stat().st_mtime)
        css_v = int((HERE / "style.css").stat().st_mtime)
        html = html.replace("/static/app.js",    f"/static/app.js?v={js_v}")
        html = html.replace("/static/style.css", f"/static/style.css?v={css_v}")
        return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
    except Exception:
        return FileResponse(str(HERE / "index.html"))


app.mount("/static", StaticFiles(directory=str(HERE)), name="static")


@app.get("/api/status")
def api_status():
    out = []
    for key, comp in COMPONENTS.items():
        s = probe(comp)
        out.append({
            "key": key,
            "label": comp["label"],
            "status": s["status"],
            "detail": s["detail"],
            "has_log": comp.get("log") is not None,
            "can_start": "start" in comp,
            "can_stop":  "stop"  in comp,
            "can_restart": "restart" in comp,
            "note": comp.get("note"),
        })
    return {"components": out}


@app.get("/api/log/{key}")
def api_log(key: str, lines: int = 100):
    comp = COMPONENTS.get(key)
    if not comp or not comp.get("log"):
        raise HTTPException(404, "no log for this component")
    p = Path(comp["log"])
    if not p.exists():
        return {"lines": [], "note": "log file does not exist yet"}
    # Tail last N lines
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read up to last 64 KB and split
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        tail = data.splitlines()[-lines:]
        return {"lines": tail}
    except Exception as e:
        return {"lines": [], "error": str(e)}


def _action(key: str, action: str):
    comp = COMPONENTS.get(key)
    if not comp:
        raise HTTPException(404, "unknown component")
    cmd = comp.get(action)
    if not cmd:
        raise HTTPException(400, f"component does not support '{action}'")
    ok, output = run_bash(cmd, timeout=90)
    return {"ok": ok, "output": output[-2000:]}


@app.post("/api/start/{key}")
def api_start(key: str):
    return _action(key, "start")


@app.post("/api/stop/{key}")
def api_stop(key: str):
    return _action(key, "stop")


@app.post("/api/restart/{key}")
def api_restart(key: str):
    return _action(key, "restart")


@app.get("/api/voice")
def api_voice():
    if not VOICE_CONFIG.exists():
        return {"engine": None, "voice": None}
    try:
        return json.loads(VOICE_CONFIG.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/magic-link")
def api_magic_link():
    ok, output = run_bash("bash /d/gerty-live/scripts/send-magic-link.sh", timeout=30)
    return {"ok": ok, "output": output[-1500:]}


@app.get("/api/tunnel-url")
def api_tunnel_url():
    p = LIVE / ".tunnel-url"
    url = p.read_text(encoding="utf-8").strip() if p.exists() else None
    return {"url": url}


# ─── LLM model management (LM Studio) ───────────────────────────────────────

LM_STUDIO_API = "http://127.0.0.1:1234"


def _resolve_lms() -> str | None:
    """Find the `lms` CLI binary cross-platform. Returns None if not installed."""
    return _find_lms()


@app.get("/api/llm/models")
def api_llm_models():
    """List every model LM Studio knows about, with load state + context length.
    Sync def so FastAPI runs it in a thread pool — sync urllib won't block the
    event loop and starve other endpoints."""
    try:
        with urllib.request.urlopen(f"{LM_STUDIO_API}/api/v0/models", timeout=5) as r:
            data = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": f"LM Studio unreachable: {e}", "models": []}
    models = []
    for m in data.get("data", []):
        models.append({
            "id": m.get("id"),
            "state": m.get("state", "unknown"),
            "type": m.get("type", "?"),
            "loaded_context": m.get("loaded_context_length"),
            "max_context": m.get("max_context_length"),
        })
    models.sort(key=lambda m: (m["state"] != "loaded", m["id"] or ""))
    return {"ok": True, "models": models}


@app.post("/api/llm/load")
def api_llm_load(req: LlmLoadRequest):
    """Load an LLM by id, optionally with a specific context length."""
    model = (req.model or "").strip()
    if not model:
        raise HTTPException(400, "model required")
    lms = _resolve_lms()
    if not lms:
        raise HTTPException(500, "lms CLI not found — set GERTY_LMS env var")
    # NOTE: omitting `--ttl` is intentional. The newer lms CLI rejects `--ttl 0`
    # ("must be at least 1") and uses "no TTL = never auto-unload" by default.
    cmd = f'"{lms}" load {shell_q(model)} -y'
    if req.context_length and req.context_length > 0:
        cmd += f" --context-length {int(req.context_length)}"
    ok, output = run_bash(cmd, timeout=180)
    return {"ok": ok, "output": output[-2000:]}


@app.post("/api/llm/unload")
def api_llm_unload(req: LlmUnloadRequest):
    """Unload a specific model or every loaded model."""
    lms = _resolve_lms()
    if not lms:
        raise HTTPException(500, "lms CLI not found — set GERTY_LMS env var")
    if req.all:
        cmd = f'"{lms}" unload --all'
    else:
        model = (req.model or "").strip()
        if not model:
            raise HTTPException(400, "model required (or set all=true)")
        cmd = f'"{lms}" unload {shell_q(model)}'
    ok, output = run_bash(cmd, timeout=60)
    return {"ok": ok, "output": output[-2000:]}


@app.get("/api/vram")
def api_vram():
    """Return VRAM totals via nvidia-smi. Falls back gracefully if no GPU."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[:200]}
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        used, free, total = (int(x) for x in parts)
        return {"ok": True, "used_mb": used, "free_mb": free, "total_mb": total}
    except FileNotFoundError:
        return {"ok": False, "error": "nvidia-smi not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def shell_q(s: str) -> str:
    """Single-quote for bash; escape inner quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


@app.get("/api/telegram-config")
def api_telegram_config():
    """Return whether the config exists + masked token (for status, never raw)."""
    if not TG_CONFIG.exists():
        return {"configured": False}
    cfg = {}
    for line in TG_CONFIG.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat = cfg.get("TELEGRAM_CHAT_ID", "")
    return {
        "configured": bool(token and chat),
        "bot_token_masked": (token[:6] + "…" + token[-4:]) if len(token) > 12 else "set",
        "chat_id": chat,
    }


# ─── Helpers for the expanded admin panels ──────────────────────────────────

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_paths() -> dict:
    """Load FILES_ROOT / VAULT_ROOT / NOTES_ROOT / MEMORY_ROOT from .paths
    (falling back to .paths.template)."""
    paths: dict[str, str] = {}
    for f in (PATHS_TEMPLATE, PATHS_FILE):
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            paths[k.strip()] = v.strip()

    def _abs(val: str) -> Path:
        p = Path(val)
        return p if p.is_absolute() else (LITE / p).resolve()

    return {k: str(_abs(v)) for k, v in paths.items()}


def _parse_model_file() -> dict:
    """Pull `export KEY=value` lines from config/.model into a dict."""
    out: dict[str, str] = {}
    if not MODEL_CONFIG.exists():
        return out
    for line in MODEL_CONFIG.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^\s*export\s+([A-Z_]+)\s*=\s*"?([^"#]*?)"?\s*(#.*)?$', line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    # Resolve simple $VAR references (e.g. GERTY_LIVE_MODEL="$GERTY_MODEL")
    for k, v in list(out.items()):
        if v.startswith("$"):
            ref = v[1:]
            if ref in out:
                out[k] = out[ref]
    return out


# ─── System prompt (read-only) ──────────────────────────────────────────────

@app.get("/api/system-prompt")
def api_system_prompt():
    """Return the live system prompt + a note that it's regenerated from the
    listener heredoc on every startup. Read-only — edit the heredoc to change."""
    if not SYSTEM_PROMPT.exists():
        return {"prompt": "", "note": "config/.system-prompt does not exist yet — start the listener once."}
    try:
        text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {
        "prompt": text,
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "source_file": str(LISTENER_SH),
        "note": ("Read-only. config/.system-prompt is regenerated from the "
                 "heredoc in scripts/gemma-listener.sh on every listener start. "
                 "Edit the heredoc between the `cat > ... <<'PROMPT'` and `PROMPT` markers."),
    }


# ─── Model config (.model env vars) ─────────────────────────────────────────

@app.get("/api/model-config")
def api_model_config():
    cfg = _parse_model_file()
    return {
        "model":         cfg.get("GERTY_MODEL", ""),
        "context":       int(cfg.get("GERTY_CONTEXT", "0") or 0),
        "live_model":    cfg.get("GERTY_LIVE_MODEL", ""),
        "vision_model":  cfg.get("GERTY_VISION_MODEL", ""),
        "vision_ttl":    int(cfg.get("GERTY_VISION_TTL", "0") or 0),
        "source":        str(MODEL_CONFIG),
        "raw":           cfg,
    }


# ─── Bot stats: token usage + history sizes + thinking mode ─────────────────

@app.get("/api/stats")
def api_stats():
    usage = _read_json(USAGE_STATS, {})
    histories = []
    if HISTORY_DIR.exists():
        for f in sorted(HISTORY_DIR.glob("chat_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                turns = len(data) if isinstance(data, list) else 0
            except Exception:
                turns = 0
            try:
                size = f.stat().st_size
                mtime = f.stat().st_mtime
            except Exception:
                size = 0
                mtime = 0
            chat_id = f.stem.replace("chat_", "")
            histories.append({
                "chat_id": chat_id,
                "turns": turns,
                "size_kb": round(size / 1024, 1),
                "last_modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else None,
            })
    thinking = ""
    if THINKING_FILE.exists():
        thinking = THINKING_FILE.read_text(encoding="utf-8").strip()
    return {
        "usage": usage,
        "histories": histories,
        "thinking_mode": thinking or "off",
    }


# ─── Chat history (view + delete) ───────────────────────────────────────────

@app.get("/api/history/{chat_id}")
def api_history_get(chat_id: str):
    """Return the full turn list for one chat."""
    if not chat_id.isdigit() and not re.match(r"^[\w-]+$", chat_id):
        raise HTTPException(400, "invalid chat_id")
    path = HISTORY_DIR / f"chat_{chat_id}.json"
    if not path.exists():
        raise HTTPException(404, "history not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"history parse error: {e}")
    if not isinstance(data, list):
        raise HTTPException(500, "history is not a list")
    return {"chat_id": chat_id, "turns": data, "count": len(data),
            "path": str(path)}


@app.delete("/api/history/{chat_id}")
def api_history_clear(chat_id: str):
    """Wipe every turn from a chat (file kept, contents reset to `[]`)."""
    if not chat_id.isdigit() and not re.match(r"^[\w-]+$", chat_id):
        raise HTTPException(400, "invalid chat_id")
    path = HISTORY_DIR / f"chat_{chat_id}.json"
    if not path.exists():
        raise HTTPException(404, "history not found")
    path.write_text("[]", encoding="utf-8")
    return {"ok": True, "chat_id": chat_id, "cleared": True}


@app.delete("/api/history/{chat_id}/{turn_index}")
def api_history_delete_turn(chat_id: str, turn_index: int):
    """Delete one turn from a chat by its zero-based index."""
    if not chat_id.isdigit() and not re.match(r"^[\w-]+$", chat_id):
        raise HTTPException(400, "invalid chat_id")
    path = HISTORY_DIR / f"chat_{chat_id}.json"
    if not path.exists():
        raise HTTPException(404, "history not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"history parse error: {e}")
    if not isinstance(data, list):
        raise HTTPException(500, "history is not a list")
    if turn_index < 0 or turn_index >= len(data):
        raise HTTPException(400, "turn_index out of range")
    removed = data.pop(turn_index)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "removed_role": removed.get("role"), "remaining": len(data)}


@app.post("/api/thinking")
def api_thinking_set(req: ThinkingSet):
    THINKING_FILE.write_text("on" if req.on else "off", encoding="utf-8")
    return {"ok": True, "thinking_mode": "on" if req.on else "off"}


# ─── System resources: RAM / CPU / disk (VRAM is /api/vram) ─────────────────

@app.get("/api/resources")
def api_resources():
    out: dict = {"ok": True}
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            out["ram"] = {
                "used_mb":   vm.used // (1024 * 1024),
                "free_mb":   vm.available // (1024 * 1024),
                "total_mb":  vm.total // (1024 * 1024),
                "percent":   vm.percent,
            }
            out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            out["cpu_count"]   = psutil.cpu_count(logical=True)
        except Exception as e:
            out["ram_error"] = str(e)
        try:
            du = psutil.disk_usage(str(LITE))
            out["disk"] = {
                "used_gb":  round(du.used  / (1024**3), 1),
                "free_gb":  round(du.free  / (1024**3), 1),
                "total_gb": round(du.total / (1024**3), 1),
                "percent":  du.percent,
            }
        except Exception as e:
            out["disk_error"] = str(e)
    else:
        out["ok"] = False
        out["error"] = "psutil not installed — run: python -m pip install psutil"
    return out


# ─── Routines manager ────────────────────────────────────────────────────────

@app.get("/api/routines")
def api_routines_list():
    data = _read_json(ROUTINES_JSON, {"chat_id": None, "routines": []})
    return {
        "chat_id": data.get("chat_id"),
        "routines": data.get("routines", []),
        "source": str(ROUTINES_JSON),
    }


@app.post("/api/routines/{rid}/toggle")
def api_routines_toggle(rid: str, req: RoutineToggle):
    data = _read_json(ROUTINES_JSON, {"chat_id": None, "routines": []})
    found = False
    for r in data.get("routines", []):
        if r.get("id") == rid:
            r["enabled"] = bool(req.enabled)
            found = True
            break
    if not found:
        raise HTTPException(404, f"routine {rid} not found")
    _write_json(ROUTINES_JSON, data)
    return {"ok": True}


@app.delete("/api/routines/{rid}")
def api_routines_delete(rid: str):
    data = _read_json(ROUTINES_JSON, {"chat_id": None, "routines": []})
    before = len(data.get("routines", []))
    data["routines"] = [r for r in data.get("routines", []) if r.get("id") != rid]
    after = len(data["routines"])
    if before == after:
        raise HTTPException(404, f"routine {rid} not found")
    _write_json(ROUTINES_JSON, data)
    return {"ok": True, "removed": before - after}


# ─── Memory browser (MEMORY_ROOT) ───────────────────────────────────────────

def _memory_root() -> Path:
    p = _parse_paths().get("MEMORY_ROOT") or str(LITE / "data" / "memory")
    return Path(p)


@app.get("/api/memory")
def api_memory_list():
    root = _memory_root()
    if not root.exists():
        return {"root": str(root), "entries": [], "note": "memory root does not exist yet"}
    entries: list[dict] = []
    for f in root.rglob("*.md"):
        if f.name == "MOC.md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        subject = "general"
        desc = ""
        saved_at = ""
        m = re.search(r"^subject:\s*(.+)$", text, re.MULTILINE)
        if m:
            subject = m.group(1).strip() or "general"
        m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if m:
            desc = m.group(1).strip()
        m = re.search(r"^saved_at:\s*(.+)$", text, re.MULTILINE)
        if m:
            saved_at = m.group(1).strip()
        # Fallback to mtime if frontmatter is missing
        if not saved_at:
            try:
                saved_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime))
            except Exception:
                pass
        entries.append({
            "name": f.stem,
            "subject": subject,
            "description": desc[:160],
            "saved_at": saved_at,
            "size": f.stat().st_size,
        })
    entries.sort(key=lambda e: (e["subject"], e["name"]))
    # Surface subject list separately so the UI can build a dropdown without
    # re-deriving it.
    subjects = sorted({e["subject"] for e in entries})
    return {"root": str(root), "entries": entries, "subjects": subjects}


@app.get("/api/memory/{name}")
def api_memory_view(name: str):
    root = _memory_root()
    safe = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    for f in root.rglob(f"{safe}.md"):
        if f.name == "MOC.md":
            continue
        try:
            return {"name": safe, "content": f.read_text(encoding="utf-8"), "path": str(f)}
        except Exception as e:
            raise HTTPException(500, str(e))
    raise HTTPException(404, f"memory {safe} not found")


@app.delete("/api/memory/{name}")
def api_memory_delete(name: str):
    root = _memory_root()
    root_resolved = root.resolve()
    safe = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    for f in root.rglob(f"{safe}.md"):
        if f.name == "MOC.md":
            continue
        # Sandbox check — refuse anything outside MEMORY_ROOT
        try:
            f.resolve().relative_to(root_resolved)
        except ValueError:
            raise HTTPException(400, "refusing to delete outside memory root")
        f.unlink()
        return {"ok": True, "deleted": safe}
    raise HTTPException(404, f"memory {safe} not found")


# ─── MCP server registry ────────────────────────────────────────────────────

@app.get("/api/mcp")
def api_mcp_list():
    data = _read_json(MCP_JSON, {"servers": []})
    return {"servers": data.get("servers", []), "source": str(MCP_JSON)}


@app.post("/api/mcp/{name}/toggle")
def api_mcp_toggle(name: str, req: McpToggle):
    data = _read_json(MCP_JSON, {"servers": []})
    found = False
    for s in data.get("servers", []):
        if s.get("name") == name:
            s["enabled"] = bool(req.enabled)
            found = True
            break
    if not found:
        raise HTTPException(404, f"mcp server {name} not found")
    _write_json(MCP_JSON, data)
    return {"ok": True, "note": "Run `python scripts/mcp_client.py refresh` to re-cache tool schemas, then restart the listener."}


# ─── Files sandbox (FILES_ROOT) ─────────────────────────────────────────────

def _files_root() -> Path:
    p = _parse_paths().get("FILES_ROOT") or str(LITE / "data" / "files")
    return Path(p)


@app.get("/api/files")
def api_files_list():
    root = _files_root()
    if not root.exists():
        return {"root": str(root), "files": []}
    entries: list[dict] = []
    for f in root.rglob("*"):
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            rel = str(f.relative_to(root)).replace("\\", "/")
            st = f.stat()
        except Exception:
            continue
        entries.append({
            "path": rel,
            "size": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        })
    entries.sort(key=lambda e: e["path"])
    return {"root": str(root), "files": entries}


@app.delete("/api/files")
def api_files_delete(path: str):
    """Delete one file inside FILES_ROOT. Path is sandbox-relative."""
    if not path:
        raise HTTPException(400, "path required")
    root = _files_root().resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "path escapes files sandbox")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    target.unlink()
    return {"ok": True, "deleted": path}


# ─── Voice picker (uses tools.KOKORO_VOICES catalog) ────────────────────────

@app.get("/api/voice/list")
def api_voice_list():
    """List available voices by introspecting tools.KOKORO_VOICES. Falls back
    gracefully if the import fails."""
    try:
        sys.path.insert(0, str(LITE / "scripts"))
        import tools  # type: ignore
        voices = [
            {"id": vid, "label": label, "engine": engine}
            for vid, (label, engine) in getattr(tools, "KOKORO_VOICES", {}).items()
        ]
        return {"voices": voices, "default": getattr(tools, "DEFAULT_VOICE", None)}
    except Exception as e:
        return {"voices": [], "error": str(e)}


@app.post("/api/voice")
def api_voice_set(req: VoiceSet):
    cfg = {"engine": req.engine, "voice": req.voice}
    VOICE_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **cfg}


if __name__ == "__main__":
    port = int(os.environ.get("GERTY_ADMIN_PORT", "9090"))
    print(f"[admin] starting on http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
