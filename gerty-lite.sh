#!/bin/bash
# GERTY Lite manager — Gemma 4 mode
#
# Cross-platform: tries pgrep/pkill first (Mac, Linux, Git-Bash-with-procps),
# falls back to PowerShell+tasklist when those aren't available (default Git
# Bash on Windows). Same script works on both.

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LISTENER="$BASE_DIR/scripts/gemma-listener.sh"
PID_FILE="$BASE_DIR/.gerty-lite.pid"
LOCK_DIR="$BASE_DIR/.listener.lock"
ROUTINES_PID_FILE="$BASE_DIR/.routines.pid"
CONTEXT_BAR_PID_FILE="$BASE_DIR/.context-bar.pid"
LOG="$BASE_DIR/gerty-lite.log"

# Returns 0 if any listener process is currently running, 1 otherwise.
is_running() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "gemma-listener" >/dev/null 2>&1
    return $?
  fi
  if command -v powershell.exe >/dev/null 2>&1; then
    local count
    count=$(powershell.exe -NoProfile -Command \
      "(Get-CimInstance Win32_Process -Filter \"Name='bash.exe'\" | Where-Object { \$_.CommandLine -match 'gemma-listener' }).Count" \
      2>/dev/null | tr -d '\r\n ')
    [[ -n "$count" && "$count" -ge 1 ]]
    return $?
  fi
  # Last resort: check lock dir
  [[ -d "$LOCK_DIR" ]]
}

# Kill every gemma-listener / routines / context_bar process (any orphans too).
nuke_all() {
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "gemma-listener|routines\.py|context_bar\.py" 2>/dev/null || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command \
      "Get-CimInstance Win32_Process -Filter \"Name='bash.exe' OR Name='python.exe'\" | Where-Object { \$_.CommandLine -match 'gemma-listener|routines\.py|context_bar\.py' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>/dev/null || true
  else
    # Try to kill PIDs from the recorded files as a last resort
    for f in "$PID_FILE" "$ROUTINES_PID_FILE" "$CONTEXT_BAR_PID_FILE"; do
      [[ -f "$f" ]] && kill -9 "$(cat "$f" 2>/dev/null)" 2>/dev/null || true
    done
  fi
  rm -f "$PID_FILE" "$ROUTINES_PID_FILE" "$CONTEXT_BAR_PID_FILE"
  rm -rf "$LOCK_DIR"
}

case "${1:-}" in
  start)
    if is_running; then
      echo "GERTY Lite already running."
      exit 0
    fi
    # Defensive: clean any orphans before starting fresh
    nuke_all
    nohup bash "$LISTENER" >> "$LOG" 2>&1 &
    disown
    # Wait up to 5 seconds for the listener to come online
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 0.5
      is_running && break
    done
    if is_running; then
      echo "GERTY Lite (Gemma 4) online."
    else
      echo "Failed to start. Check $LOG"
      exit 1
    fi
    ;;
  stop)
    nuke_all
    echo "GERTY Lite stopped (all listener + routines processes terminated)."
    ;;
  restart)
    bash "$0" stop
    sleep 0.5
    bash "$0" start
    ;;
  status)
    if is_running; then
      # winpid file is Windows-only (Git Bash). On Mac/Linux just show the bash PID.
      if [[ -f "$LOCK_DIR/winpid" ]]; then
        echo "Running (winpid $(cat "$LOCK_DIR/winpid" 2>/dev/null))"
      elif [[ -f "$PID_FILE" ]]; then
        echo "Running (pid $(cat "$PID_FILE" 2>/dev/null))"
      else
        echo "Running."
      fi
    else
      echo "Not running."
    fi
    ;;
  log)
    tail -40 "$LOG"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log}"
    ;;
esac
