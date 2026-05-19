#!/usr/bin/env python3
"""
GERTY — first-run setup wizard.

Cross-platform (macOS + Windows). Run this once after cloning the repo:

    python setup.py

Walks you through:
  1. Python version check (need 3.10+)
  2. LM Studio detection — link if missing, pick model from `lms ls` if present
  3. Telegram bot — paste a token from @BotFather + the chat ID you want gerty
     to talk to
  4. Data paths — defaults to ./data/{vault,notes,memory,files} inside the
     repo; point them at iCloud/Dropbox/wherever if you have an existing vault
  5. Optional: enable Kokoro TTS, OmniVoice, camofox stealth browser
  6. Writes all config files; idempotent — re-running re-prompts only for
     missing values

After this completes:
    bash gerty-lite.sh start        # or `python setup.py --start`

The setup wizard never writes secrets to anywhere git-tracked. Everything
sensitive lives in config/.telegram-config, config/.allowed-chats,
config/.paths, config/routines.json — all gitignored.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# Local helpers — single source of truth for path/binary discovery.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))
from _paths import (  # noqa: E402
    find_python, find_bash, find_lms,
    IS_WINDOWS, IS_MACOS, IS_LINUX,
)


# ── tiny TUI helpers ────────────────────────────────────────────────────

C_RESET = "\033[0m"
C_DIM   = "\033[2m"
C_BOLD  = "\033[1m"
C_GREEN = "\033[32m"
C_RED   = "\033[31m"
C_AMBER = "\033[33m"
C_CYAN  = "\033[36m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if IS_WINDOWS:
        # Windows 10+ terminals (Windows Terminal, VS Code, modern cmd) handle ANSI.
        # The classic console host needs explicit enabling — call it.
        try:
            import ctypes
            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)
            return True
        except Exception:
            return False
    return True


_USE_COLOR = supports_color()


def col(text: str, code: str) -> str:
    return f"{code}{text}{C_RESET}" if _USE_COLOR else text


def banner(text: str) -> None:
    line = "─" * (len(text) + 4)
    print()
    print(col(line, C_DIM))
    print(col(f"  {text}", C_BOLD + C_CYAN))
    print(col(line, C_DIM))


def info(text: str) -> None:
    print(col("›", C_CYAN), text)


def ok(text: str) -> None:
    print(col("✓", C_GREEN), text)


def warn(text: str) -> None:
    print(col("!", C_AMBER), text)


def err(text: str) -> None:
    print(col("✗", C_RED), text)


def prompt(question: str, default: str = "", secret: bool = False) -> str:
    suffix = f" {col(f'[{default}]', C_DIM)}" if default else ""
    if secret:
        try:
            import getpass
            ans = getpass.getpass(f"  {question}{suffix} ")
        except Exception:
            ans = input(f"  {question}{suffix} ")
    else:
        ans = input(f"  {question}{suffix} ")
    return (ans.strip() or default).strip()


def prompt_yn(question: str, default: bool = True) -> bool:
    suffix = "(Y/n)" if default else "(y/N)"
    while True:
        a = input(f"  {question} {col(suffix, C_DIM)} ").strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("    please answer y or n")


# ── Config files we manage ─────────────────────────────────────────────

CONFIG_DIR = HERE / "config"
TELEGRAM_CONFIG = CONFIG_DIR / ".telegram-config"
ALLOWED_CHATS   = CONFIG_DIR / ".allowed-chats"
PATHS_FILE      = CONFIG_DIR / ".paths"
PATHS_TEMPLATE  = CONFIG_DIR / ".paths.template"
MODEL_FILE      = CONFIG_DIR / ".model"
ROUTINES_FILE   = CONFIG_DIR / "routines.json"
ROUTINES_EXAMPLE = CONFIG_DIR / "routines.example.json"
ENV_FILE        = CONFIG_DIR / ".env"


def read_keyvalues(path: Path) -> dict:
    """Read KEY=value lines into a dict. Tolerates `export KEY="value"` and
    quoted values. Comments and blank lines are skipped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def write_keyvalues(path: Path, data: dict, header: str = "", as_export: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        for h in header.splitlines():
            lines.append(f"# {h}")
        lines.append("")
    for k, v in data.items():
        prefix = "export " if as_export else ""
        quoted = '"' + str(v).replace('"', '\\"') + '"' if any(c in str(v) for c in " #") else str(v)
        lines.append(f"{prefix}{k}={quoted}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Steps ──────────────────────────────────────────────────────────────

def step_python() -> str:
    banner("1. Python interpreter")
    py = find_python()
    if py == "python3" and not shutil.which("python3"):
        err("No real Python interpreter found.")
        info("Install Python 3.10+ from https://python.org or `brew install python3` / "
             "`winget install Python.Python.3.12`.")
        sys.exit(1)
    # Version check
    try:
        out = subprocess.run([py, "--version"], capture_output=True, text=True, timeout=5)
        version = (out.stdout + out.stderr).strip()
        ok(f"using {py}")
        ok(f"  {version}")
        major_minor = version.split()[-1].split(".")[:2]
        if (int(major_minor[0]), int(major_minor[1])) < (3, 10):
            err("Python 3.10+ required. Upgrade and re-run.")
            sys.exit(1)
    except Exception as e:
        warn(f"couldn't verify Python version: {e}")
    return py


def step_lm_studio() -> tuple[str, int]:
    banner("2. LM Studio")
    lms = find_lms()
    if not lms:
        warn("LM Studio's `lms` CLI not found on this machine.")
        info("Download LM Studio:  https://lmstudio.ai/")
        info("After installing, open the app once → Developer tab → Enable `lms`.")
        info("You can also re-run this wizard later — model setup is skipped for now.")
        return ("", 0)

    ok(f"found `lms` at {lms}")
    info("Make sure LM Studio is running. If not, open the app or run `lms server start`.")

    # List models — best effort
    try:
        r = subprocess.run([lms, "ls"], capture_output=True, text=True, timeout=10)
        listing = r.stdout or r.stderr
    except Exception as e:
        warn(f"`lms ls` failed: {e}")
        listing = ""
    if listing.strip():
        print(col("\n  Models you've downloaded in LM Studio:", C_DIM))
        for line in listing.splitlines()[:30]:
            print(col("    " + line, C_DIM))
        print()
    else:
        warn("no models found. Open LM Studio and download a Gemma-3-4b-it (vision) "
             "and a chat model (e.g. gemma-4-26b-a4b-it).")

    model = prompt("Main chat model id (paste exactly as it appears in `lms ls`):", "")
    if not model:
        warn("Skipping model config — set GERTY_MODEL in config/.model later.")
        return ("", 0)

    while True:
        ctx_raw = prompt("Context length (in tokens):", "32768")
        try:
            ctx = int(ctx_raw)
            break
        except ValueError:
            print("    please enter a whole number")

    return (model, ctx)


def step_telegram() -> tuple[str, str]:
    banner("3. Telegram bot")
    existing = read_keyvalues(TELEGRAM_CONFIG)
    cur_token = existing.get("TELEGRAM_BOT_TOKEN", "")
    cur_chat  = existing.get("TELEGRAM_CHAT_ID", "")
    if cur_token and cur_chat:
        ok(f"Telegram already configured (chat {cur_chat}, token …{cur_token[-6:]})")
        if not prompt_yn("Reconfigure?", default=False):
            return (cur_token, cur_chat)

    info("To create a new bot, open Telegram, message @BotFather:")
    info(col("    /newbot", C_CYAN))
    info("Follow the prompts — it'll give you a token like `123456:ABC-DEF…`.")
    info("To find your chat_id, message @userinfobot — it replies with your numeric ID.")
    print()

    while True:
        token = prompt("TELEGRAM_BOT_TOKEN:", cur_token, secret=False)
        if token and ":" in token and len(token) > 20:
            break
        print("    token should look like `123456:ABCdef…` — try again")
    while True:
        chat_id = prompt("TELEGRAM_CHAT_ID (your numeric Telegram ID):", cur_chat)
        if chat_id.isdigit() or (chat_id.startswith("-") and chat_id[1:].isdigit()):
            break
        print("    chat_id should be a number — try again")

    return (token, chat_id)


def step_paths() -> dict:
    banner("4. Data locations")
    info("GERTY stores data in four folders. Defaults keep everything inside this repo")
    info("under ./data/. Point them elsewhere if you already have an Obsidian vault,")
    info("iCloud notes folder, etc. Leave blank to accept the default.")
    print()

    paths_template = read_keyvalues(PATHS_TEMPLATE)
    existing       = read_keyvalues(PATHS_FILE)

    keys = [
        ("VAULT_ROOT",  "Vault (drafts, inbox, published)"),
        ("NOTES_ROOT",  "Notes vault (your Obsidian or plain notes folder)"),
        ("MEMORY_ROOT", "Long-term memory (bot's persistent facts)"),
        ("FILES_ROOT",  "User files sandbox (PDFs, attachments)"),
    ]
    result: dict[str, str] = {}
    for key, label in keys:
        default = existing.get(key, paths_template.get(key, ""))
        val = prompt(f"{label}:", default)
        result[key] = val
    return result


def step_optional_extras() -> dict:
    banner("5. Optional extras")
    extras: dict[str, str] = {}

    info("Voice — pick a TTS engine (or skip).")
    info("  • OmniVoice: cloned voices, slower, GPU-friendly")
    info("  • Kokoro: fast English, lightweight, smaller models")
    info("  • Skip: the bot stays text-only (you can enable later via /voice in admin)")

    if prompt_yn("Enable Kokoro TTS?", default=False):
        default_kokoro = ""
        if IS_WINDOWS and Path("D:/Claude/scripts/tts.py").exists():
            default_kokoro = "D:/Claude/scripts/tts.py"
        kokoro = prompt("Path to kokoro-server.py (absolute):", default_kokoro)
        if kokoro:
            extras["GERTY_KOKORO_TTS"] = kokoro

    if prompt_yn("Enable camofox stealth browser (for web_fetch fallback)?", default=False):
        info("camofox is bundled in this repo at camofox-browser/. PM2 starts it.")
        info("You'll need Node 18+ and `pm2` installed (`npm i -g pm2`).")

    return extras


def step_write_configs(py: str, lm: tuple[str, int], tg: tuple[str, str],
                        paths: dict, extras: dict) -> None:
    banner("6. Writing config files")

    # .telegram-config
    write_keyvalues(
        TELEGRAM_CONFIG,
        {"TELEGRAM_BOT_TOKEN": tg[0], "TELEGRAM_CHAT_ID": tg[1]},
        header="Telegram credentials — never commit. .gitignore covers this file.",
    )
    ok(f"wrote {TELEGRAM_CONFIG.relative_to(HERE)}")

    # .allowed-chats — start with just the user's chat_id
    ALLOWED_CHATS.write_text(tg[1] + "\n", encoding="utf-8")
    ok(f"wrote {ALLOWED_CHATS.relative_to(HERE)}")

    # .paths — merge with template
    template = read_keyvalues(PATHS_TEMPLATE)
    final = {**template, **paths}
    write_keyvalues(
        PATHS_FILE,
        final,
        header=("Local path overrides — gitignored. Edit to relocate vault, notes, "
                "memory, or files sandbox."),
    )
    ok(f"wrote {PATHS_FILE.relative_to(HERE)}")

    # .model
    if lm[0]:
        write_keyvalues(
            MODEL_FILE,
            {
                "GERTY_MODEL":         lm[0],
                "GERTY_CONTEXT":       str(lm[1]),
                "GERTY_LIVE_MODEL":    "$GERTY_MODEL",  # reuse for live by default
                "GERTY_VISION_MODEL":  "gemma-3-4b-it",
                "GERTY_VISION_TTL":    "300",
            },
            header="LLM selection — sourced by autostart, listener, and health-check.",
            as_export=True,
        )
        ok(f"wrote {MODEL_FILE.relative_to(HERE)}")

    # routines.json — copy example if not already present
    if not ROUTINES_FILE.exists() and ROUTINES_EXAMPLE.exists():
        data = json.loads(ROUTINES_EXAMPLE.read_text(encoding="utf-8"))
        data["chat_id"] = int(tg[1])
        ROUTINES_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok(f"wrote {ROUTINES_FILE.relative_to(HERE)} (seeded from example)")

    # .env — optional extras for shell scripts to source
    if extras:
        write_keyvalues(
            ENV_FILE,
            extras,
            header=("Optional environment overrides — sourced by autostart.sh. "
                    "Each line is `KEY=value`. Safe to delete entirely."),
            as_export=True,
        )
        ok(f"wrote {ENV_FILE.relative_to(HERE)}")

    # Make sure data/ skeleton exists so the bot doesn't crash on first read
    for sub in ("data/vault/inbox", "data/vault/drafts", "data/vault/published",
                "data/vault/resources", "data/vault/templates",
                "data/notes/Progress", "data/memory/entries", "data/files/inbox"):
        d = HERE / sub
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
    ok("scaffolded ./data/ skeleton")


def step_next() -> None:
    banner("Done")
    info("Next steps:")
    print()
    print(col("  Test it:        ", C_DIM) + col("bash gerty-lite.sh start", C_BOLD))
    print(col("  Admin dashboard:", C_DIM) + col(" python admin/server.py", C_BOLD) +
          col(" then open http://127.0.0.1:9090", C_DIM))
    if IS_WINDOWS:
        print(col("  Autostart:      ", C_DIM) +
              col("python scripts/install-autostart.py", C_BOLD) +
              col(" (registers Windows Task Scheduler entries)", C_DIM))
    elif IS_MACOS:
        print(col("  Autostart:      ", C_DIM) +
              col("python scripts/install-autostart.py", C_BOLD) +
              col(" (installs a launchd agent in ~/Library/LaunchAgents)", C_DIM))
    print()
    info("Message your bot on Telegram to confirm it's alive. The first message after")
    info("startup may take ~5 s while the model warms up.")
    print()


# ── main ───────────────────────────────────────────────────────────────

def main() -> None:
    print(col(textwrap.dedent("""
        ╭──────────────────────────────────────────╮
        │              GERTY  setup                │
        │   local-first Telegram bot + admin UI    │
        ╰──────────────────────────────────────────╯
    """).strip(), C_CYAN))
    print()
    print(col("This wizard configures GERTY for first use. It writes only to local,", C_DIM))
    print(col("gitignored files — nothing personal ever lands in the repo.", C_DIM))
    print()

    if "--start" in sys.argv:
        # Skip setup, just start
        start_bot()
        return

    py = step_python()
    lm = step_lm_studio()
    tg = step_telegram()
    paths = step_paths()
    extras = step_optional_extras()
    step_write_configs(py, lm, tg, paths, extras)
    step_next()


def start_bot() -> None:
    """Hand off to the manager script. Used by `python setup.py --start`
    so beginners don't need to know about bash on Windows."""
    bash = find_bash()
    cmd = [bash, str(HERE / "gerty-lite.sh"), "start"]
    info(f"launching: {' '.join(cmd)}")
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("aborted by user — nothing was written")
        sys.exit(130)
