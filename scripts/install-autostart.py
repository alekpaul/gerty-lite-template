#!/usr/bin/env python3
"""
GERTY — cross-platform autostart installer.

Registers GERTY to launch automatically when the user logs in.

  • macOS:   writes ~/Library/LaunchAgents/com.gerty.lite.plist + a 5-min
             health-check plist, then loads both via `launchctl bootstrap`.
  • Windows: registers Scheduled Tasks `Gerty Autostart` (logon trigger) and
             `Gerty Health Check` (every 5 min). Idempotent re-registers.

Run:
    python scripts/install-autostart.py             # install
    python scripts/install-autostart.py uninstall   # remove

No sudo required; everything lives in the user's home directory.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from _paths import IS_WINDOWS, IS_MACOS, find_bash  # noqa: E402


AUTOSTART_SH    = REPO / "scripts" / "autostart.sh"
HEALTHCHECK_SH  = REPO / "scripts" / "health-check.sh"


# ── macOS: launchd ──────────────────────────────────────────────────────

MAC_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
MAC_AUTOSTART_LABEL = "com.gerty.lite.autostart"
MAC_HEALTH_LABEL    = "com.gerty.lite.healthcheck"


def _mac_plist(label: str, args: list[str], interval_sec: int | None = None,
                run_at_load: bool = False) -> str:
    """Generate a LaunchAgent plist string."""
    arg_xml = "\n".join(f"        <string>{a}</string>" for a in args)
    schedule_xml = ""
    if interval_sec is not None:
        schedule_xml = f"    <key>StartInterval</key>\n    <integer>{interval_sec}</integer>\n"
    if run_at_load:
        schedule_xml += "    <key>RunAtLoad</key>\n    <true/>\n    <key>KeepAlive</key>\n    <false/>\n"
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
          <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
        {arg_xml}
            </array>
        {schedule_xml}    <key>WorkingDirectory</key>
            <string>{REPO}</string>
            <key>EnvironmentVariables</key>
            <dict>
              <key>GERTY_DIR</key>
              <string>{REPO}</string>
              <key>PATH</key>
              <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
            </dict>
            <key>StandardOutPath</key>
            <string>{REPO}/{label}.out.log</string>
            <key>StandardErrorPath</key>
            <string>{REPO}/{label}.err.log</string>
          </dict>
        </plist>
    """)


def install_macos() -> None:
    if not AUTOSTART_SH.exists():
        print(f"✗ {AUTOSTART_SH} not found", file=sys.stderr)
        sys.exit(1)
    MAC_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    bash = find_bash()

    # Autostart at login
    auto_path = MAC_AGENT_DIR / f"{MAC_AUTOSTART_LABEL}.plist"
    auto_path.write_text(_mac_plist(
        label=MAC_AUTOSTART_LABEL,
        args=[bash, str(AUTOSTART_SH)],
        run_at_load=True,
    ))
    print(f"✓ wrote {auto_path}")

    # Health check every 5 min
    health_path = MAC_AGENT_DIR / f"{MAC_HEALTH_LABEL}.plist"
    health_path.write_text(_mac_plist(
        label=MAC_HEALTH_LABEL,
        args=[bash, str(HEALTHCHECK_SH)],
        interval_sec=300,
    ))
    print(f"✓ wrote {health_path}")

    uid = os.getuid()
    for label, path in ((MAC_AUTOSTART_LABEL, auto_path), (MAC_HEALTH_LABEL, health_path)):
        # Unload first (ignore failure on first install)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True)
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            print(f"✓ loaded {label} into launchd")
        else:
            print(f"! launchctl bootstrap {label} returned {r.returncode}: "
                  f"{(r.stderr or '').strip()}")

    print()
    print("GERTY will now launch automatically on login.")
    print(f"Logs:  {REPO}/{MAC_AUTOSTART_LABEL}.out.log")


def uninstall_macos() -> None:
    uid = os.getuid()
    for label in (MAC_AUTOSTART_LABEL, MAC_HEALTH_LABEL):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True)
    for label in (MAC_AUTOSTART_LABEL, MAC_HEALTH_LABEL):
        p = MAC_AGENT_DIR / f"{label}.plist"
        if p.exists():
            p.unlink()
            print(f"✓ removed {p}")
    print("GERTY autostart uninstalled.")


# ── Windows: Scheduled Tasks ────────────────────────────────────────────

WIN_AUTOSTART_TASK = "Gerty Autostart"
WIN_HEALTH_TASK    = "Gerty Health Check"


def install_windows() -> None:
    if not AUTOSTART_SH.exists():
        print(f"X {AUTOSTART_SH} not found", file=sys.stderr)
        sys.exit(1)
    bash = find_bash()

    # Autostart at logon
    autostart_action = f'"{bash}" "{AUTOSTART_SH}"'
    r = subprocess.run([
        "schtasks", "/Create", "/F",
        "/TN", WIN_AUTOSTART_TASK,
        "/SC", "ONLOGON",
        "/RL", "LIMITED",
        "/TR", autostart_action,
    ], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"OK registered '{WIN_AUTOSTART_TASK}'")
    else:
        print(f"X schtasks failed: {(r.stderr or r.stdout or '').strip()}")
        sys.exit(1)

    # Health check every 5 min
    health_action = f'"{bash}" "{HEALTHCHECK_SH}"'
    r = subprocess.run([
        "schtasks", "/Create", "/F",
        "/TN", WIN_HEALTH_TASK,
        "/SC", "MINUTE", "/MO", "5",
        "/TR", health_action,
    ], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"OK registered '{WIN_HEALTH_TASK}'")
    else:
        print(f"X schtasks failed: {(r.stderr or r.stdout or '').strip()}")
        sys.exit(1)

    print()
    print("GERTY will launch on next login. View tasks: `schtasks /Query /TN \"Gerty Autostart\"`")


def uninstall_windows() -> None:
    for task in (WIN_AUTOSTART_TASK, WIN_HEALTH_TASK):
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", task],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"OK removed '{task}'")
        else:
            print(f"! schtasks delete '{task}' returned: "
                  f"{(r.stderr or r.stdout or '').strip()}")


# ── entry point ─────────────────────────────────────────────────────────

def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if action not in ("install", "uninstall"):
        print(__doc__.strip())
        sys.exit(2)

    if IS_MACOS:
        (install_macos if action == "install" else uninstall_macos)()
    elif IS_WINDOWS:
        (install_windows if action == "install" else uninstall_windows)()
    else:
        # Linux — provide a hint, don't pretend to support systemd here.
        print("Linux is not yet supported automatically. You can wrap")
        print("scripts/autostart.sh in a systemd user unit:")
        print()
        print(f"  ~/.config/systemd/user/gerty.service:")
        print("  [Unit] Description=GERTY")
        print("  [Service]")
        print(f"  ExecStart={find_bash()} {AUTOSTART_SH}")
        print(f"  WorkingDirectory={REPO}")
        print("  Restart=on-failure")
        print("  [Install] WantedBy=default.target")
        print()
        print("  systemctl --user daemon-reload && systemctl --user enable --now gerty")


if __name__ == "__main__":
    main()
