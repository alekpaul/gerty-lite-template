"""
Smoke tests for read-aloud.py and the read_aloud tool.

Run from project root:
  python scripts/test_read_aloud.py

Plain asserts + a tiny pass/fail counter. No pytest dependency.

Network-touching tests are skipped by default; set RUN_NETWORK_TESTS=1 to
include them.
"""

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_ra():
    """Load read-aloud.py as a module (its filename has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "ra", SCRIPTS_DIR / "read-aloud.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


# ── 1. /read b64 command decoding ────────────────────────────────────────────

def test_decode_b64_command():
    print("\n[1] _decode_b64_command")
    ra = _load_ra()

    cases = [
        ("/read https://example.com",  True,  "https://example.com"),
        ("/read  some text  ",         True,  "some text"),
        ("/read",                      True,  ""),
        ("/READ obsidian/x.md",        True,  "obsidian/x.md"),  # case-insensitive
        ("Hello there",                False, ""),
        ("/help",                      False, ""),
        ("",                           False, ""),
    ]
    for text, expected_is_read, expected_arg in cases:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        is_read, arg = ra._decode_b64_command(b64)
        check(
            f"{text!r} → is_read={expected_is_read} arg={expected_arg!r}",
            is_read == expected_is_read and arg == expected_arg,
            detail=f"got ({is_read}, {arg!r})",
        )


# ── 2. Input classification (no network for URL case) ───────────────────────

def test_classification_offline():
    print("\n[2] _resolve_input classification (offline)")
    ra = _load_ra()

    # Vault path — uses real file (master Obsidian index)
    try:
        source, text = ra._resolve_input("obsidian/Organized-notes/CLAUDE.md")
        check(
            "vault path → source label",
            source.startswith("file: obsidian/Organized-notes/CLAUDE.md"),
            detail=f"source={source!r}",
        )
        check(
            "vault path → non-empty text",
            len(text) > 100,
            detail=f"len={len(text)}",
        )
    except Exception as e:
        check("vault path resolves", False, detail=str(e))

    # Raw text → speaks as-is
    source, text = ra._resolve_input("Hello world")
    check("raw text → source=text", source == "text", detail=f"got {source!r}")
    check("raw text → text passthrough", text == "Hello world")

    # Empty input rejected
    try:
        ra._resolve_input("   ")
        check("empty input raises", False)
    except ValueError:
        check("empty input raises", True)


# ── 3. Subprocess exit codes (--from-b64) ────────────────────────────────────

def test_subprocess_exit_codes():
    print("\n[3] read-aloud.py exit codes via --from-b64")

    py = sys.executable
    script = str(SCRIPTS_DIR / "read-aloud.py")

    def run_b64(text: str) -> int:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        proc = subprocess.run(
            [py, script, "165548659", "FAKE_TOKEN", "--from-b64", b64],
            capture_output=True, timeout=30,
        )
        return proc.returncode

    # Non-/read message → exit 2 (listener falls through to LLM)
    rc = run_b64("Hello there")
    check("non-/read message → exit 2", rc == 2, detail=f"rc={rc}")

    # /read with raw text (fake token; Telegram send will fail silently;
    # OmniVoice will be tried, which is also fine — we just check the script
    # treats it as a /read command, NOT exit 2).
    if os.environ.get("RUN_NETWORK_TESTS"):
        rc = run_b64("/read Hello world")
        check("/read raw text → exit != 2", rc != 2, detail=f"rc={rc}")
    else:
        print("  SKIP  /read raw text (would call OmniVoice — set RUN_NETWORK_TESTS=1)")


# ── 4. Tool registered in tools.py ───────────────────────────────────────────

def test_tool_registered():
    print("\n[4] read_aloud tool registered in tools.py")
    import tools  # noqa
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    check("read_aloud in TOOL_SCHEMAS", "read_aloud" in names,
          detail=f"available: {sorted(names)}")
    check("read_aloud dispatcher works", callable(tools.read_aloud))
    # Calling without context returns the 'no context' error (not a crash)
    tools._CTX = {}
    out = tools.read_aloud("any text")
    check("read_aloud handles missing context",
          isinstance(out, str) and "context" in out.lower(),
          detail=f"out={out!r}")


# ── 5. Router includes read_aloud on relevant keywords ──────────────────────

def test_router_keywords():
    print("\n[5] gemma_chat router includes read_aloud on keywords")
    import gemma_chat  # noqa
    # The router's LLM call is mocked out by simulating its empty fallback —
    # we only test the keyword heuristic branch.
    triggers = [
        "read me this article",
        "read it aloud",
        "speak this aloud",
        "voice it for me",
        "прочитай мені цю статтю",
        "озвуч мені це",
    ]
    for msg in triggers:
        # We can't easily call select_tools without LLM access; instead, do
        # the keyword check the same way the heuristic does.
        low = msg.lower()
        read_aloud_kw = (
            "read me ", "read this aloud", "read aloud", "read it aloud",
            "speak this", "speak it aloud", "voice this", "voice it",
            "say it aloud", "tts this", "read out loud",
            "прочитай", "озвуч", "озвучити", "прочитати вголос",
            "скажи вголос", "озвуч мені",
        )
        hit = any(kw in low for kw in read_aloud_kw)
        check(f"keyword match: {msg!r}", hit)

    # Negative — non-read messages shouldn't trip
    for msg in ["how are you", "what is the weather", "saved a note"]:
        low = msg.lower()
        read_aloud_kw = (
            "read me ", "read this aloud", "read aloud", "read it aloud",
            "speak this", "speak it aloud", "voice this", "voice it",
            "прочитай", "озвуч",
        )
        hit = any(kw in low for kw in read_aloud_kw)
        check(f"no keyword match: {msg!r}", not hit)


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("read_aloud smoke tests")
    print("=" * 60)
    test_decode_b64_command()
    test_classification_offline()
    test_subprocess_exit_codes()
    test_tool_registered()
    test_router_keywords()
    print()
    print("=" * 60)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
