"""Activate the repo-local vendor/ directory for vendored Python packages.

We install MCP and its transitive deps to D:/gerty-lite/vendor/ via
`pip install --target` instead of the system Python (which is uv-managed).
This module wires up sys.path and the Windows pywin32 DLL search path so
`import mcp` and friends work the same way they would in a venv.

Idempotent — safe to import from multiple modules.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR = _REPO_ROOT / "vendor"


def activate() -> bool:
    """Add vendor/ to sys.path and configure pywin32. Returns True if vendor
    exists, False otherwise (caller can decide whether to degrade gracefully)."""
    if not VENDOR.exists():
        return False

    # Most packages just need vendor/ on sys.path.
    vendor_str = str(VENDOR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)

    # pywin32 is structured oddly under --target installs: it ships sub-roots
    # (win32/, win32/lib/, pythonwin/) that must each be on sys.path, plus a
    # pywin32_system32/ holding the actual DLLs (pywintypes312.dll etc.).
    win32_root = VENDOR / "win32"
    if win32_root.exists():
        for sub in (win32_root, win32_root / "lib", VENDOR / "pythonwin", VENDOR / "win32com"):
            if sub.exists() and str(sub) not in sys.path:
                sys.path.insert(0, str(sub))

    dll_dir = VENDOR / "pywin32_system32"
    if dll_dir.exists():
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            # Python 3.8+ requires this for DLL search outside of PATH on Windows.
            os.add_dll_directory(str(dll_dir))
        except (AttributeError, OSError):
            pass
    return True


activate()
