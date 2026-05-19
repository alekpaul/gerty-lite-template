"""Cross-platform path + binary discovery for GERTY.

Single source of truth for "where am I, what's installed, how do I call it"
across macOS, Linux, and Windows. Every other Python module imports from
here instead of hardcoding `D:/gerty-lite/...` or `/c/Users/alekp/...`.

Environment overrides (set in your shell or .env-style file) win over
auto-detection:

  GERTY_DIR           – absolute path to the repo root (override of __file__-based detection)
  GERTY_PYTHON3       – absolute path to the Python interpreter to use for spawned subprocesses
  GERTY_BASH          – absolute path to a POSIX-shell bash (Git Bash on Windows, /bin/bash on mac/linux)
  GERTY_LMS           – absolute path to LM Studio's `lms` CLI binary
  GERTY_LIVE_DIR      – sibling gerty-live repo (defaults to ../gerty-live)
  GERTY_KOKORO_TTS    – path to kokoro-server.py (optional voice engine)
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from functools import lru_cache
from pathlib import Path


# ── Platform flags ────────────────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS   = platform.system() == "Darwin"
IS_LINUX   = platform.system() == "Linux"


# ── Repo root ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Where is gerty-lite installed?

    Order:
      1. $GERTY_DIR if set (env override — used by setup wizards and CI)
      2. The parent of the directory containing this file (`scripts/_paths.py`
         lives one level under the repo root)
    """
    env = os.environ.get("GERTY_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


# ── Python ────────────────────────────────────────────────────────────────

def _is_real_python(path: str) -> bool:
    """Skip the Windows Store stub at C:\\Users\\<user>\\AppData\\Local\\Microsoft\\WindowsApps — it
    exits silently when run from a subprocess."""
    if not path:
        return False
    if "WindowsApps" in path:
        return False
    return os.path.exists(path)


@lru_cache(maxsize=1)
def find_python() -> str:
    """Locate a real Python 3 interpreter for spawned subprocesses.

    Order:
      1. $GERTY_PYTHON3 override
      2. The interpreter running this code (sys.executable) — usually correct
      3. `python3` then `python` on PATH (skipping Windows Store stubs)
      4. Common uv install locations on Mac and Windows
    """
    env = os.environ.get("GERTY_PYTHON3", "").strip()
    if _is_real_python(env):
        return env

    if _is_real_python(sys.executable or ""):
        return sys.executable

    for candidate in ("python3", "python"):
        found = shutil.which(candidate) or ""
        if _is_real_python(found):
            return found

    # uv standalone installs — common on both platforms
    if IS_WINDOWS:
        home = os.environ.get("USERPROFILE", "")
        for sub in ("AppData/Roaming/uv/python", "AppData/Local/uv/python"):
            base = Path(home) / sub
            if base.is_dir():
                for d in sorted(base.iterdir(), reverse=True):
                    exe = d / "python.exe"
                    if exe.exists():
                        return str(exe)
    else:
        home = os.environ.get("HOME", "")
        for sub in (".local/share/uv/python", "Library/Application Support/uv/python"):
            base = Path(home) / sub
            if base.is_dir():
                for d in sorted(base.iterdir(), reverse=True):
                    exe = d / "bin" / "python3"
                    if exe.exists():
                        return str(exe)

    return "python3"  # last-resort name, hope it's on PATH at runtime


# ── Bash (Git Bash on Windows, /bin/bash on mac/linux) ────────────────────

@lru_cache(maxsize=1)
def find_bash() -> str:
    """Locate a POSIX bash. Required for the .sh scripts the bot uses
    (gemma-listener.sh, autostart.sh, etc.)."""
    env = os.environ.get("GERTY_BASH", "").strip()
    if env and os.path.exists(env):
        return env

    found = shutil.which("bash") or ""
    if found and os.path.exists(found):
        return found

    if IS_WINDOWS:
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if os.path.exists(cand):
                return cand
        return r"C:\Program Files\Git\bin\bash.exe"  # last-resort guess

    return "/bin/bash"


# ── LM Studio's `lms` CLI ─────────────────────────────────────────────────

@lru_cache(maxsize=1)
def find_lms() -> str | None:
    """Locate LM Studio's CLI binary. Returns None if not installed —
    callers can show a helpful "install LM Studio" message instead of
    crashing."""
    env = os.environ.get("GERTY_LMS", "").strip()
    if env and os.path.exists(env):
        return env

    found = shutil.which("lms") or ""
    if found and os.path.exists(found):
        return found

    if IS_WINDOWS:
        home = os.environ.get("USERPROFILE", "")
        cand = Path(home) / ".lmstudio" / "bin" / "lms.exe"
        if cand.exists():
            return str(cand)
    else:
        home = os.environ.get("HOME", "")
        candidates = [
            Path(home) / ".lmstudio" / "bin" / "lms",
            Path(home) / ".cache" / "lm-studio" / "bin" / "lms",
        ]
        if IS_MACOS:
            candidates += [
                Path("/Applications/LM Studio.app/Contents/Resources/lms/lms"),
                Path(home) / "Applications" / "LM Studio.app" / "Contents" / "Resources" / "lms" / "lms",
            ]
        for c in candidates:
            if c.exists():
                return str(c)

    return None


# ── Sibling repo: gerty-live (optional) ───────────────────────────────────

@lru_cache(maxsize=1)
def gerty_live_dir() -> Path | None:
    env = os.environ.get("GERTY_LIVE_DIR", "").strip()
    if env:
        p = Path(env).resolve()
        return p if p.exists() else None
    sibling = repo_root().parent / "gerty-live"
    return sibling if sibling.exists() else None


# ── Convenience: well-known files inside the repo ─────────────────────────

def repo_file(*parts: str) -> Path:
    """Resolve a path relative to the repo root. Use this instead of
    hardcoding `D:/gerty-lite/...`."""
    return repo_root().joinpath(*parts)
