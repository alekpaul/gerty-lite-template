"""Cross-platform process helpers.

Replaces direct calls to `tasklist`/`taskkill` (Windows-only) and `ps`/`kill`
(POSIX-only) scattered across the codebase. Uses psutil when available,
falls back to os.kill(pid, 0) liveness check on POSIX. Windows without
psutil falls back to `tasklist` / `taskkill` exec.
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess

IS_WINDOWS = platform.system() == "Windows"

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def pid_alive(pid: int) -> bool:
    """Is this PID still running? Returns False on any error (including
    "process not found")."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _HAS_PSUTIL:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False
    if IS_WINDOWS:
        # tasklist /FI "PID eq <pid>" — output contains the pid on a match
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def kill_pid(pid: int, force: bool = True) -> bool:
    """Kill a PID. Returns True on success, False if the PID didn't exist
    or termination failed."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _HAS_PSUTIL:
        try:
            p = psutil.Process(pid)
            p.kill() if force else p.terminate()
            return True
        except psutil.NoSuchProcess:
            return False
        except Exception:
            pass  # fall through to shell-out below
    if IS_WINDOWS:
        try:
            args = ["taskkill", "/PID", str(pid)]
            if force:
                args.append("/F")
            r = subprocess.run(args, capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except Exception:
        return False


def pid_on_port(port: int) -> int | None:
    """Find the PID listening on a TCP port (localhost). Returns None if
    no process is bound. Used to manage ad-hoc Python servers (Kokoro TTS,
    admin dashboard) that aren't under PM2."""
    if _HAS_PSUTIL:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                    return conn.pid
        except Exception:
            pass
        # Some platforms restrict psutil.net_connections without root — fall
        # through to the platform-native CLIs below.
    if IS_WINDOWS:
        try:
            r = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
            for line in (r.stdout or "").splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    try:
                        return int(parts[-1])
                    except ValueError:
                        continue
        except Exception:
            return None
        return None
    # Mac / Linux — lsof if present, else ss
    lsof = shutil.which("lsof")
    if lsof:
        try:
            r = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (r.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1])
        except Exception:
            return None
    ss = shutil.which("ss")
    if ss:
        try:
            r = subprocess.run(
                [ss, "-tlnp", f"sport = :{port}"],
                capture_output=True, text=True, timeout=5,
            )
            # parse `users:(("name",pid=NNN,fd=N))`
            import re
            m = re.search(r"pid=(\d+)", r.stdout or "")
            if m:
                return int(m.group(1))
        except Exception:
            return None
    return None
