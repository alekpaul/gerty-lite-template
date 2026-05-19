"""
GERTY Lite — context-bar daemon.

Maintains two pinned Telegram messages showing live context-window usage:
  Detail: "1.5K/32K | Gemma-4-31B  •  N calls"
  Bar:    "Context ▓▓▓░░░░░░░░ 4.6%"

Reads .usage-stats.json (written by gemma_chat.py after every LLM call).
Queries LM Studio's /api/v0/models for the loaded context length.
Edits the pinned messages in place — never re-sends, never floods the chat.

Zero token cost: this daemon is purely external. The LLM never sees or
generates anything related to context-bar display.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
USAGE_FILE = BASE_DIR / ".usage-stats.json"
DETAIL_MSG_FILE = BASE_DIR / ".detail-msg-id"
BAR_MSG_FILE = BASE_DIR / ".bar-msg-id"
PID_FILE = BASE_DIR / ".context-bar.pid"
LOG_FILE = BASE_DIR / "context-bar.log"
TG_CONFIG = BASE_DIR / "config" / ".telegram-config"
LLM_API = "http://127.0.0.1:1234"

POLL_INTERVAL_SEC = 3        # how often to check the usage file
MIN_EDIT_INTERVAL_SEC = 2    # rate-limit Telegram edits (avoid spam / 429s)

MODEL_LABELS = {
    "gemma-4-31b-it": "Gemma-4-31B",
    "google/gemma-4-26b-a4b": "Gemma-4-26B",
    "gemma-4-26b-a4b-it-uncensored": "Gemma-4-26B-Uncensored",
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Config & Telegram helpers ──────────────────────────────────────────────────

def load_config() -> tuple[str | None, int | None]:
    token = None
    chat_id = None
    if not TG_CONFIG.exists():
        return None, None
    for line in TG_CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("TELEGRAM_CHAT_ID="):
            try:
                chat_id = int(line.split("=", 1)[1].strip().strip('"').strip("'"))
            except ValueError:
                pass
    return token, chat_id


def _tg_post(token: str, method: str, payload: dict) -> dict | None:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        if "message is not modified" in body:
            return {"ok": True, "not_modified": True}
        log(f"tg {method} HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log(f"tg {method} error: {e}")
        return None


def send_and_pin(token: str, chat_id: int, text: str) -> int | None:
    r = _tg_post(token, "sendMessage", {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not r or not r.get("ok"):
        return None
    mid = r["result"]["message_id"]
    _tg_post(token, "pinChatMessage",
             {"chat_id": chat_id, "message_id": mid, "disable_notification": "true"})
    return mid


def edit(token: str, chat_id: int, message_id: int, text: str) -> bool:
    r = _tg_post(token, "editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    return bool(r and r.get("ok"))


# ── gerty-live magic link (so the pinned context bar also shows the link) ─────
LIVE_DIR = Path("D:/gerty-live")


def read_magic_link() -> str | None:
    """Return the current magic link URL (tunnel + session token), or None if
    gerty-live isn't up. Cheap — just two file reads."""
    try:
        tunnel = (LIVE_DIR / ".tunnel-url").read_text(encoding="utf-8").strip()
        token  = (LIVE_DIR / ".session-token").read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None
    if not tunnel or not token:
        return None
    return f"{tunnel}/?t={token}"


def magic_link_mtime() -> float:
    """Combined mtime of the two state files; 0 if either is missing."""
    try:
        return max(
            (LIVE_DIR / ".tunnel-url").stat().st_mtime,
            (LIVE_DIR / ".session-token").stat().st_mtime,
        )
    except (OSError, FileNotFoundError):
        return 0.0


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── LM Studio info ─────────────────────────────────────────────────────────────

def get_loaded_context() -> tuple[int, str]:
    """Query LM Studio for the loaded context length and active model id."""
    try:
        req = urllib.request.Request(f"{LLM_API}/api/v0/models")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        for m in data.get("data", []):
            if m.get("state") == "loaded":
                ctx = int(m.get("loaded_context_length") or 0)
                if ctx > 0:
                    return ctx, m.get("id", "")
    except Exception:
        pass
    return 0, ""


# ── Rendering ──────────────────────────────────────────────────────────────────

def fmt_k(n: int) -> str:
    if n >= 10000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def build_bar(fraction: float, width: int = 14) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    return "▓" * filled + "░" * (width - filled)


def render(stats: dict, loaded_ctx: int, loaded_model: str,
           magic_link: str | None = None) -> tuple[str, str]:
    """Return (detail_text, bar_text). Bar tracks PEAK prompt_tokens since last
    /new — smaller side-calls (reaction picker, tool router) don't make the bar
    drop. Peak = the largest single prompt the model has had to process.

    If `magic_link` is provided, appended as a clickable <a> tag below the bar
    so the user always has gerty-live one tap away from the pinned message."""
    peak = int(stats.get("peak_prompt_tokens", 0) or 0)
    last_prompt = int(stats.get("last_prompt_tokens", 0) or 0)
    calls = int(stats.get("call_count", 0) or 0)
    model_id = stats.get("model") or loaded_model or "?"
    label = MODEL_LABELS.get(model_id, model_id)

    if loaded_ctx <= 0:
        detail = f"   model unloaded — peak {fmt_k(peak)} tokens | {label} | {calls} calls"
        bar_plain = "Context ░░░░░░░░░░░░░░ (no model)"
    else:
        frac = peak / loaded_ctx if loaded_ctx else 0
        detail = (
            f"   peak {fmt_k(peak)} / {fmt_k(loaded_ctx)} | last {fmt_k(last_prompt)} "
            f"| {label} | {calls} calls"
        )
        bar_plain = f"Context {build_bar(frac)} {frac * 100:.1f}%"

    # HTML-escape the plain text (everything we render is plain prose now) and
    # append the magic-link clickable label on its own line when available.
    bar = _html_escape(bar_plain)
    if magic_link:
        bar = f'{bar}\n🔓 <a href="{_html_escape(magic_link)}">open gerty.live</a>'
    detail_html = _html_escape(detail)
    return detail_html, bar


# ── Daemon loop ────────────────────────────────────────────────────────────────

def read_stats() -> dict:
    if not USAGE_FILE.exists():
        return {}
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_msg_id(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_msg_id(path: Path, mid: int) -> None:
    try:
        path.write_text(str(mid), encoding="utf-8")
    except Exception as e:
        log(f"write msg id error: {e}")


def main():
    # Single-instance guard via PID file + tasklist check
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip())
            # Cheap liveness check: try sending signal 0
            os.kill(old, 0)
            log(f"another context-bar daemon running (PID {old}); exiting")
            sys.exit(0)
        except (ValueError, OSError, ProcessLookupError):
            pass
    PID_FILE.write_text(str(os.getpid()))

    token, chat_id = load_config()
    if not token or not chat_id:
        log("missing telegram token or chat_id; exiting")
        sys.exit(1)

    log(f"context-bar daemon online (PID {os.getpid()})")

    last_stats_mtime = -1   # force first iteration to draw
    last_loaded_ctx = -2
    last_magic_mtime = -1.0
    last_edit_time = 0
    last_detail = "__init__"
    last_bar = "__init__"

    try:
        while True:
            try:
                # Cheap polling: only redraw when usage file, loaded context, or
                # magic-link state changes.
                stats = read_stats()
                mtime = USAGE_FILE.stat().st_mtime if USAGE_FILE.exists() else 0
                loaded_ctx, loaded_model = get_loaded_context()
                magic_mtime = magic_link_mtime()

                if (mtime != last_stats_mtime or loaded_ctx != last_loaded_ctx
                        or magic_mtime != last_magic_mtime):
                    magic_link = read_magic_link()
                    detail_text, bar_text = render(stats, loaded_ctx, loaded_model, magic_link)

                    now = time.time()
                    if (detail_text != last_detail or bar_text != last_bar) and \
                       (now - last_edit_time >= MIN_EDIT_INTERVAL_SEC):

                        detail_id = read_msg_id(DETAIL_MSG_FILE)
                        bar_id = read_msg_id(BAR_MSG_FILE)

                        # Send + pin once; subsequently edit in place
                        if detail_id is None:
                            mid = send_and_pin(token, chat_id, detail_text)
                            if mid:
                                write_msg_id(DETAIL_MSG_FILE, mid)
                                log(f"detail pinned msg_id={mid}")
                        else:
                            edit(token, chat_id, detail_id, detail_text)

                        if bar_id is None:
                            mid = send_and_pin(token, chat_id, bar_text)
                            if mid:
                                write_msg_id(BAR_MSG_FILE, mid)
                                log(f"bar pinned msg_id={mid}")
                        else:
                            edit(token, chat_id, bar_id, bar_text)

                        last_edit_time = now
                        last_detail = detail_text
                        last_bar = bar_text

                    last_stats_mtime = mtime
                    last_loaded_ctx = loaded_ctx
                    last_magic_mtime = magic_mtime
            except Exception as e:
                log(f"loop error: {e}")

            time.sleep(POLL_INTERVAL_SEC)
    finally:
        try:
            PID_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
