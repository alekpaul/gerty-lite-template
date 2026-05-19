"""
GERTY Lite — routine scheduler daemon.

Reads config/routines.json, watches the clock, fires each routine on its cron
schedule by piping the prompt into gemma_chat.py in "routine" mode. The agent
inside chooses delivery (send_message to Telegram, write_file to vault, both).

Stateless per fire — no history. One process per fire (subprocess isolation).
"""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from croniter import croniter

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "routines.json"
TG_CONFIG = BASE_DIR / "config" / ".telegram-config"
SYSTEM_PROMPT = BASE_DIR / "config" / ".system-prompt"
GEMMA_CHAT = BASE_DIR / "scripts" / "gemma_chat.py"
LOG_FILE = BASE_DIR / "routines.log"
PID_FILE = BASE_DIR / ".routines.pid"
LAST_FIRE_FILE = BASE_DIR / ".routines-last-fire.json"

LLM_API = "http://127.0.0.1:1234"
MODEL = os.environ.get("GERTY_MODEL", "gemma-4-26b-a4b-it-uncensored")
# Same Python as the parent process — no hardcoded interpreter path needed.
PYTHON_EXE = sys.executable


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_tg_token() -> str | None:
    if not TG_CONFIG.exists():
        return None
    for line in TG_CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            tok = line.split("=", 1)[1].strip().strip('"').strip("'")
            return tok
    return None


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"chat_id": 0, "routines": []}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"config parse error: {e}")
        return {"chat_id": 0, "routines": []}


def load_last_fires() -> dict:
    if not LAST_FIRE_FILE.exists():
        return {}
    try:
        return json.loads(LAST_FIRE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_last_fires(fires: dict) -> None:
    try:
        LAST_FIRE_FILE.write_text(json.dumps(fires, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"save last_fires error: {e}")


def fire_routine(routine: dict, chat_id: int, tg_token: str) -> None:
    """Spawn gemma_chat.py in routine mode with the routine's prompt on stdin."""
    rid = routine.get("id", "unknown")
    prompt = routine.get("prompt", "")
    if not prompt:
        log(f"[{rid}] no prompt — skipping")
        return

    # Substitute {today}
    today = date.today().isoformat()
    prompt = prompt.replace("{today}", today)

    history_path = BASE_DIR / ".history" / f"routine_{rid}.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        PYTHON_EXE,
        str(GEMMA_CHAT),
        "routine",
        str(chat_id),
        str(history_path),
        str(SYSTEM_PROMPT),
        LLM_API,
        MODEL,
        "30",       # max_history (unused in routine mode)
        tg_token,
        "0",        # message_id (no inbound message)
    ]

    log(f"[{rid}] firing — prompt: {prompt[:80]}")
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if result.returncode != 0:
            log(f"[{rid}] non-zero exit {result.returncode}: {result.stderr[:300]}")
        else:
            # gemma_chat prints [routine] final-text to stderr
            last_lines = "\n".join(result.stderr.strip().splitlines()[-6:])
            log(f"[{rid}] done. tail: {last_lines}")
    except subprocess.TimeoutExpired:
        log(f"[{rid}] timed out after 600s")
    except Exception as e:
        log(f"[{rid}] error: {e}")


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Backed by psutil when available;
    falls back to tasklist on Windows / os.kill(pid,0) on POSIX."""
    try:
        from _proc import pid_alive
        return pid_alive(pid)
    except Exception:
        # Last-resort POSIX fallback
        import os
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def main():
    # Single-instance guard — uses _proc.pid_alive under the hood, which is
    # platform-aware (Windows tasklist / POSIX kill(0) / psutil when present).
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip())
            if _is_pid_alive(old):
                log(f"another routines daemon running (PID {old}); exiting")
                sys.exit(0)
        except (ValueError, OSError):
            pass
        # Stale pid file — take over
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()))

    tg_token = load_tg_token()
    if not tg_token:
        log("no TELEGRAM_BOT_TOKEN — exiting")
        sys.exit(1)

    log(f"routines daemon online (PID {os.getpid()})")

    try:
        while True:
            cfg = load_config()
            chat_id = cfg.get("chat_id") or 0
            routines = cfg.get("routines", [])
            last_fires = load_last_fires()
            now = datetime.now().replace(second=0, microsecond=0)

            for r in routines:
                if not r.get("enabled", False):
                    continue
                rid = r.get("id")
                sched = r.get("schedule", "")
                if not rid or not sched:
                    continue
                try:
                    last_str = last_fires.get(rid)
                    # Compute the next-fire time. For routines that have fired
                    # before, base = last fire. For brand-new routines, use a
                    # small lookback window (5 min) so we don't fire cron matches
                    # from years ago, but still catch "just-overdue" reminders
                    # if the daemon was briefly down.
                    if last_str:
                        base = datetime.fromisoformat(last_str)
                    else:
                        base = now - timedelta(minutes=5)
                    it = croniter(sched, base)
                    next_fire = it.get_next(datetime)
                    if next_fire <= now:
                        fire_routine(r, chat_id, tg_token)
                        last_fires[rid] = now.isoformat(timespec="minutes")
                        save_last_fires(last_fires)
                        # one-shot reminders auto-disable after firing once
                        if r.get("one_shot"):
                            try:
                                cfg2 = load_config()
                                for rr in cfg2.get("routines", []):
                                    if rr.get("id") == rid:
                                        rr["enabled"] = False
                                CONFIG_FILE.write_text(
                                    json.dumps(cfg2, indent=2, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                                log(f"[{rid}] one-shot fired, disabled")
                            except Exception as e:
                                log(f"[{rid}] couldn't disable after one-shot: {e}")
                except Exception as e:
                    log(f"[{rid}] schedule error: {e}")

            time.sleep(30)
    finally:
        try:
            PID_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
