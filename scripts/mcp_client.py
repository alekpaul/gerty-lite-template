"""GERTY Lite — minimal MCP client.

Reads config/mcp.json for stdio MCP servers, exposes their tools through
the existing tool dispatcher. Tool *schemas* are cached on disk so the
per-message hot path doesn't pay the spawn cost just to know what's
available. Tool *calls* spawn the server fresh each time (~200-500ms
overhead). Good enough for personal use; the right fix later is a
persistent MCP host process.

Public API used by tools.py:
    MCP_AVAILABLE        — True if both vendored mcp + a usable config exist
    cached_tool_schemas() — list of OpenAI-style tool schemas, namespaced as
                             "mcp__<server>__<tool>"
    call_tool(name, args) — synchronous call; routes to the right server,
                             spawns it, calls, tears it down

CLI:
    python scripts/mcp_client.py refresh        — populate the cache from
                                                    every enabled server
    python scripts/mcp_client.py status         — show config + cache state
    python scripts/mcp_client.py test <server>  — connect to one server,
                                                    list its tools, exit
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import vendor_setup  # noqa: F401 — must run before importing mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "mcp.json"
CACHE_DIR = REPO_ROOT / ".mcp-cache"

# Hard upper bounds so a misbehaving server can't wedge the chat handler.
CONNECT_TIMEOUT_S = 8.0
CALL_TIMEOUT_S = 30.0

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_IMPORT_OK = True
except Exception as e:
    _MCP_IMPORT_OK = False
    _MCP_IMPORT_ERR = repr(e)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"servers": []}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"servers": []}


def _enabled_servers() -> list[dict]:
    cfg = _load_config()
    return [s for s in cfg.get("servers", []) if s.get("enabled") and s.get("name")]


def _config_fingerprint(server: dict) -> str:
    """Hash of the bits that affect what tools a server exposes. Used to
    invalidate the cache when a server config changes."""
    keys = ("command", "args", "env")
    payload = json.dumps({k: server.get(k) for k in keys}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _cache_path(server_name: str) -> Path:
    return CACHE_DIR / f"{server_name}.json"


MCP_AVAILABLE = _MCP_IMPORT_OK and bool(_enabled_servers())


# ── Async core ────────────────────────────────────────────────────────────────
async def _with_session(server: dict, fn):
    """Open a stdio MCP session for one server and run `fn(session)`. Closes
    the subprocess on exit, even on error."""
    if not _MCP_IMPORT_OK:
        raise RuntimeError(f"mcp not importable: {_MCP_IMPORT_ERR}")
    params = StdioServerParameters(
        command=server["command"],
        args=list(server.get("args", [])),
        env={**os.environ, **server.get("env", {})} if server.get("env") else None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_S)
            return await fn(session)


async def _async_list_tools(server: dict) -> list[dict]:
    async def _do(session):
        result = await session.list_tools()
        out = []
        for t in getattr(result, "tools", []) or []:
            # Coerce to plain JSON-able shape. mcp returns pydantic models;
            # we want vanilla dicts that match the OpenAI tool schema format.
            schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            out.append({
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "inputSchema": schema,
            })
        return out

    return await _with_session(server, _do)


async def _async_call_tool(server: dict, tool_name: str, args: dict) -> str:
    async def _do(session):
        result = await asyncio.wait_for(
            session.call_tool(tool_name, arguments=args or {}),
            timeout=CALL_TIMEOUT_S,
        )
        # mcp returns a CallToolResult with .content (list of TextContent / ImageContent).
        chunks = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                chunks.append(text)
        if getattr(result, "isError", False):
            return "MCP tool error: " + "\n".join(chunks) if chunks else "MCP tool error (no message)"
        return "\n".join(chunks) if chunks else "(empty result)"

    return await _with_session(server, _do)


def _run_async(coro):
    """Run an async coroutine to completion from sync code. gemma_chat is
    fully synchronous, so every MCP call gets its own short-lived loop."""
    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        # Defensive — if we're somehow already inside an event loop (we shouldn't
        # be), fall back to new_event_loop. This branch is unlikely to fire.
        if "asyncio.run() cannot be called" in str(e):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise


# ── Schema namespacing ────────────────────────────────────────────────────────
PREFIX = "mcp__"
SEP = "__"


def _qualified(server_name: str, tool_name: str) -> str:
    safe_server = "".join(c if c.isalnum() or c in "-_" else "_" for c in server_name)
    safe_tool = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_name)
    return f"{PREFIX}{safe_server}{SEP}{safe_tool}"


def _parse_qualified(qualified: str) -> tuple[str, str] | None:
    if not qualified.startswith(PREFIX):
        return None
    rest = qualified[len(PREFIX):]
    if SEP not in rest:
        return None
    server, _, tool = rest.partition(SEP)
    return (server, tool)


def is_mcp_tool(name: str) -> bool:
    return name.startswith(PREFIX) and SEP in name[len(PREFIX):]


# ── Cache layer ───────────────────────────────────────────────────────────────
def _load_server_cache(server_name: str) -> dict | None:
    p = _cache_path(server_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_server_cache(server_name: str, fingerprint: str, tools: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fingerprint": fingerprint, "tools": tools}
    _cache_path(server_name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cached_tool_schemas() -> list[dict]:
    """Return OpenAI-style tool schemas for every enabled server that has a
    valid cache entry. Skips servers with stale or missing caches — the user
    refreshes those via `python scripts/mcp_client.py refresh`."""
    if not _MCP_IMPORT_OK:
        return []
    out: list[dict] = []
    for server in _enabled_servers():
        cache = _load_server_cache(server["name"])
        if not cache or cache.get("fingerprint") != _config_fingerprint(server):
            continue
        for t in cache.get("tools", []):
            out.append({
                "type": "function",
                "function": {
                    "name": _qualified(server["name"], t["name"]),
                    "description": (
                        f"[MCP/{server['name']}] {t.get('description', '')}".strip()
                    ),
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
    return out


def call_tool(qualified_name: str, args: dict) -> str:
    """Sync entry point used by tools.execute_tool. Routes the call to the
    right server. Returns a plain-text result the LLM can read."""
    parsed = _parse_qualified(qualified_name)
    if not parsed:
        return f"mcp_client error: not a qualified MCP tool name ({qualified_name})"
    server_name, tool_name = parsed
    if not _MCP_IMPORT_OK:
        return f"mcp_client error: mcp not importable ({_MCP_IMPORT_ERR})"
    server = next((s for s in _enabled_servers() if s["name"] == server_name), None)
    if not server:
        return f"mcp_client error: server '{server_name}' is not enabled or not configured"
    try:
        return _run_async(_async_call_tool(server, tool_name, args or {}))
    except asyncio.TimeoutError:
        return f"mcp_client error: server '{server_name}' timed out"
    except Exception as e:
        return f"mcp_client error: {server_name}.{tool_name}: {e}"


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cli_refresh() -> int:
    if not _MCP_IMPORT_OK:
        print(f"mcp not importable: {_MCP_IMPORT_ERR}", file=sys.stderr)
        return 1
    servers = _enabled_servers()
    if not servers:
        print("no enabled servers in config/mcp.json — nothing to refresh")
        return 0
    code = 0
    for server in servers:
        name = server["name"]
        print(f"[{name}] spawning…", flush=True)
        try:
            tools = _run_async(_async_list_tools(server))
        except Exception as e:
            print(f"[{name}] FAILED: {e}", file=sys.stderr)
            code = 1
            continue
        _save_server_cache(name, _config_fingerprint(server), tools)
        print(f"[{name}] cached {len(tools)} tool(s):")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description','')[:80]}")
    return code


def _cli_status() -> int:
    cfg = _load_config()
    servers = cfg.get("servers", [])
    if not servers:
        print("config/mcp.json has no servers configured.")
        return 0
    print(f"mcp importable: {_MCP_IMPORT_OK}" + (f" ({_MCP_IMPORT_ERR})" if not _MCP_IMPORT_OK else ""))
    print()
    for s in servers:
        name = s.get("name", "?")
        enabled = bool(s.get("enabled"))
        cache = _load_server_cache(name) if enabled else None
        fp_match = cache and cache.get("fingerprint") == _config_fingerprint(s)
        status = "DISABLED" if not enabled else ("CACHED" if fp_match else "NEEDS REFRESH")
        n_tools = len(cache["tools"]) if cache else 0
        print(f"  {name:20s} {status:14s} tools={n_tools}")
    return 0


def _cli_test(server_name: str) -> int:
    if not _MCP_IMPORT_OK:
        print(f"mcp not importable: {_MCP_IMPORT_ERR}", file=sys.stderr)
        return 1
    cfg = _load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("name") == server_name), None)
    if not server:
        print(f"server '{server_name}' not in config/mcp.json", file=sys.stderr)
        return 1
    try:
        tools = _run_async(_async_list_tools(server))
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"{server_name} exposes {len(tools)} tool(s):")
    for t in tools:
        print(f"  - {t['name']}: {t.get('description','')[:100]}")
    return 0


def _cli_main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "refresh":
        return _cli_refresh()
    if cmd == "status":
        return _cli_status()
    if cmd == "test":
        if len(argv) < 3:
            print("usage: mcp_client.py test <server_name>", file=sys.stderr)
            return 2
        return _cli_test(argv[2])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv))
