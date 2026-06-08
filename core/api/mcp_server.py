"""
Cosmos v5 — MCP Server (Tier 0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Model Context Protocol server for Claude Desktop / Cursor.

5 tools: brain_search, brain_get, brain_aggregate, brain_remember, brain_status

Phase 3 enhancements:
  - Permission engine (per-tool + per-folder access control)
  - Activity log (every call recorded for transparency)
  - Folder-scoped read/write enforcement
  - Graceful handling when MCP SDK is missing
"""
from __future__ import annotations
import json
import os
import re
import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Resolve absolute path to project root so DB paths like "data/brain_v2" work
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.append(PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import TextContent, Tool, Resource
    from pydantic import AnyUrl
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from core.memory.store_v2 import get_store_v2
from core.memory.search import BM25Search
from core.memory.aggregator_v2 import UniversalAggregator
from core.memory.schema_registry_v2 import get_registry_v2
from core.api.mcp_permissions import get_permission_engine
from core.api.mcp_activity import get_activity_log, Timer

try:
    from core.agents import registry as _agent_registry
except ImportError:
    class DummyRegistry:
        @staticmethod
        def is_tool_allowed(agent, tool_name):
            return True
        @staticmethod
        def is_path_in_scope(scope_path, target):
            return True
        @staticmethod
        def create_agent(*args, **kwargs):
            raise NotImplementedError("Agent provisioning is a premium feature only available in the Cosmos Desktop App.")
        @staticmethod
        def verify_token(*args, **kwargs):
            return None
    _agent_registry = DummyRegistry



# AI Control Center — Phase 0 enforcement layer.
#
# When this MCP server is spawned with --agent-token (or COSMOS_AGENT_TOKEN
# in the environment), `_AGENT` is bound to the verified Agent record at
# startup. Every tool call is then gated by:
#   1. Tool name in agent.tools_whitelist (else "tool not allowed")
#   2. For write tools, the resource path must live under agent.scope_path
#
# When _AGENT is None — i.e. legacy launch with no token — the server runs
# in unrestricted "operator" mode (preserves existing behaviour for users
# who haven't enrolled in the Control Center yet). The /personal landing
# page can stay zero-config until the user explicitly creates an agent.
_AGENT: "_agent_registry.Agent | None" = None

# Tools whose resource argument names a brain folder/path; the dispatcher
# extracts the path argument and runs it through is_path_in_scope before
# proceeding. Keep this in lock-step with the tool input schemas added in
# Phase 0 — drift = a write tool that bypasses scope enforcement.
_SCOPED_WRITE_TOOLS: dict[str, str] = {
    "brain_remember":      "folder",       # path string, e.g. "/Notes/Trades"
    "brain_create_folder": "path",         # full target path
    "brain_delete_folder": "path",         # full target path
    "brain_move_memory":   "target_folder",
    # brain_create_agent is not scoped — it's a high-trust tool that
    # should only land in the whitelist of an explicitly "trusted"
    # template. Whitelist gating is the only barrier.
}


# Tool-name aliases live in their own module so the permission engine and
# agent registry can consult them without circular-importing this file.
# Re-exported here under the legacy underscored names for back-compat with
# the existing test suite + any downstream callers that hard-code these.
from core.api.tool_aliases import (
    CANONICAL_ALIASES as _CANONICAL_ALIASES,
    resolve_canonical as _resolve_canonical,
)


def _enforce_agent_policy(tool_name: str, arguments: dict) -> tuple[bool, str]:
    """Returns (ok, reason). Used by call_tool BEFORE the existing
    per-tool perms.can_call_tool check so denials surface a single,
    coherent reason."""
    if _AGENT is None:
        return True, ""
    # 1. Tool whitelist
    if not _agent_registry.is_tool_allowed(_AGENT, tool_name):
        return False, (
            f"agent {_AGENT.name!r} is not allowed to call {tool_name!r} "
            f"(template={_AGENT.template}). Edit tools_whitelist in the "
            f"AI Control Center to grant."
        )
    # 2. Scope check for write tools
    arg_key = _SCOPED_WRITE_TOOLS.get(tool_name)
    if arg_key:
        target = (arguments or {}).get(arg_key) or ""
        if not isinstance(target, str) or not target.strip():
            # Write tools without a folder/path argument are allowed —
            # e.g. brain_remember with no folder lands at /Inbox by
            # default. We treat /Inbox as "inside scope" for any agent
            # since it's the universal capture zone.
            return True, ""
        if not _agent_registry.is_path_in_scope(_AGENT.scope_path, target):
            return False, (
                f"agent {_AGENT.name!r} has scope {_AGENT.scope_path!r}; "
                f"target path {target!r} is outside that scope."
            )
    return True, ""


# ─────────────────────────────────────────────
# Solution 3 — periodic Cosmos-first reminder
# ─────────────────────────────────────────────
#
# Long sessions drift: an AI obeys CLAUDE.md at session-start, then ~10–20
# tool calls in it starts grep-first because the rule slid out of working
# attention. We counter that by appending a short banner to MCP tool
# responses every N successful calls (default 10).
#
# Process-scoped counter — MCP stdio clients get their own process per
# session, so this naturally resets between sessions. Skip injection for
# the orientation tools themselves (`cosmos_get_preamble`,
# `find_relevant_code`) so the user doesn't see double guidance on the
# call that already IS the orientation.
#
# Opt-out: env COSMOS_NO_REMINDER=1.
# Tunable interval: env COSMOS_REMINDER_INTERVAL=N (default 10).
# Gap-trigger: env COSMOS_REMINDER_GAP_SECONDS=N (default 600). When the gap
# between the previous and current counted call exceeds this, the banner
# fires immediately — even if n is not yet a multiple of the interval. This
# is the moment Claude/Cursor/Cline/Windsurf/Gemini etc. most often drift
# back from a grep-Read excursion: catching the FIRST call after the gap
# re-anchors them on the default-first flow.

import threading as _threading
import time as _time

_REMINDER_LOCK = _threading.Lock()
_REMINDER_CALL_COUNT = 0
_REMINDER_LAST_CALL_TS = 0.0

_REMINDER_SKIP_TOOLS = frozenset({
    "cosmos_get_preamble",
    "find_relevant_code",
    # claude_log_task / claude_report are operator-facing dogfood tools;
    # piggybacking a reminder on them adds noise to the report.
    "claude_log_task",
    "claude_report",
})

_REMINDER_BANNER_TEMPLATE = (
    "\n\n---\n"
    "🌌 **Cosmos · session reminder (call #{n}{annotation})**\n\n"
    "Default flow for every code task in this project:\n"
    "1. `cosmos_get_preamble(path=cwd, intent=...)`  — once per session\n"
    "2. `find_relevant_code(symptom=<user prompt>, path=cwd)`  — per task\n"
    "3. `code_list_errors(path=cwd, file_path=...)`  — before any edit\n"
    "4. THEN `grep`/`Read`/`Edit` if Cosmos returned nothing.\n\n"
    "_This is the default, not a trigger. Silence with env `COSMOS_NO_REMINDER=1`._"
)


def _reminder_interval() -> int:
    """Reads COSMOS_REMINDER_INTERVAL on every call so it's tunable
    without restarting the MCP server. Clamps to >=2 so a misconfigured
    1 doesn't spam every response."""
    raw = os.environ.get("COSMOS_REMINDER_INTERVAL", "").strip()
    if not raw:
        return 10
    try:
        n = int(raw)
        return max(2, n)
    except ValueError:
        return 10


def _reminder_disabled() -> bool:
    return os.environ.get("COSMOS_NO_REMINDER", "").strip() in ("1", "true", "yes")


def _reminder_gap_seconds() -> int:
    """Threshold for the gap-trigger. Clamped to >=60 so a misconfigured
    value can't fire the banner on back-to-back calls."""
    raw = os.environ.get("COSMOS_REMINDER_GAP_SECONDS", "").strip()
    if not raw:
        return 600
    try:
        return max(60, int(raw))
    except ValueError:
        return 600


def _maybe_append_reminder(tool_name: str, result_text: str) -> str:
    """Increment the per-process call counter and append the Cosmos-first
    reminder when ANY of these is true:

      * counter is a multiple of the configured interval (default 10), OR
      * gap-trigger — the previous counted call was >GAP_SECONDS ago
        (default 600), meaning the AI just re-entered Cosmos after a long
        excursion (typically grep/Read fallback). This is when reminders
        land hardest.

    Skip rules:
      * env COSMOS_NO_REMINDER=1 disables entirely
      * orientation tools (preamble/find_relevant_code) — already on-topic
      * non-success paths handled by caller (only called for status='ok')

    Process-local counter — works for every MCP client (Claude Code, Cursor,
    Cline, Windsurf, Aider, Gemini CLI, ...) because every stdio session
    spawns its own mcp_server process.
    """
    global _REMINDER_CALL_COUNT, _REMINDER_LAST_CALL_TS
    if _reminder_disabled():
        return result_text
    if tool_name in _REMINDER_SKIP_TOOLS:
        return result_text
    interval = _reminder_interval()
    gap_threshold = _reminder_gap_seconds()
    now = _time.time()
    with _REMINDER_LOCK:
        _REMINDER_CALL_COUNT += 1
        n = _REMINDER_CALL_COUNT
        prev_ts = _REMINDER_LAST_CALL_TS
        _REMINDER_LAST_CALL_TS = now

    gap_seconds = now - prev_ts if prev_ts > 0 else 0.0
    fire_interval = n > 0 and n % interval == 0
    # Gap-trigger only fires when there's a real previous call to gap from
    # (prev_ts > 0) — the very first call of the process never re-fires.
    fire_gap = prev_ts > 0 and gap_seconds >= gap_threshold

    if not (fire_interval or fire_gap):
        return result_text

    if fire_gap and not fire_interval:
        annotation = f" · re-entry after {int(gap_seconds // 60)}min gap"
    else:
        annotation = ""
    return result_text + _REMINDER_BANNER_TEMPLATE.format(n=n, annotation=annotation)


def _reset_reminder_counter():
    """Test-only helper — never invoked from production paths."""
    global _REMINDER_CALL_COUNT, _REMINDER_LAST_CALL_TS
    with _REMINDER_LOCK:
        _REMINDER_CALL_COUNT = 0
        _REMINDER_LAST_CALL_TS = 0.0


# ─────────────────────────────────────────────────────────────────────────
# Demo-blocker hardening — payload-trim + periodic process recycle.
# Lesson 78c5b62a: MCP stdio calls hang indefinitely (10min+) on long-lived
# processes even though the handler runs in 0.01s — the stall is isolated to
# the SDK's stdio transport, correlated with (a) long uptime and (b) large
# JSON payloads hitting a slow serialization path. The event-loop starvation
# half is already fixed (dispatch runs on a worker thread, see _DISPATCH_POOL).
# These two guards close the remaining gaps:
#
#   1. _trim_payload — caps any single tool result before it crosses stdio so
#      a runaway serialization (a code_search that matched everything, a giant
#      skeleton) can never wedge the transport. Tunable / disable-able.
#   2. _RecycleWatchdog — a daemon thread that exits(0) the process once it's
#      "old" (uptime or call count), but ONLY while idle (no in-flight call,
#      last call older than a grace window). The stdio client (Claude Code /
#      Cursor / Cline / ...) respawns a fresh server on the next tool call, so
#      this is the automated form of the lesson's manual "kill + respawn"
#      workaround. The idle gate guarantees it never drops an in-flight call
#      or fires mid-conversation during a live demo.
# ─────────────────────────────────────────────────────────────────────────

# Default cap: 200 KB. Most legitimate results are <20 KB; a 200 KB ceiling
# leaves generous headroom while still bounding the worst case well below the
# payload sizes that correlate with the SDK stall.
_PAYLOAD_TRIM_DEFAULT = 200_000


def _payload_trim_limit() -> int:
    """Max chars for a single tool result. Read per-call so it's tunable
    without a restart. 0 (or negative) disables trimming."""
    raw = os.environ.get("COSMOS_MAX_PAYLOAD_CHARS", "").strip()
    if not raw:
        return _PAYLOAD_TRIM_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return _PAYLOAD_TRIM_DEFAULT


def _trim_payload(text: str) -> str:
    """Cap an outbound tool result so an oversized payload can't stall the
    stdio transport. Trims on a line boundary near the limit and appends a
    visible marker so the AI knows the result was clipped (and can re-query
    more narrowly)."""
    limit = _payload_trim_limit()
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    # Prefer cutting at the last newline so we don't sever a line mid-token.
    nl = head.rfind("\n")
    if nl > limit * 0.8:
        head = head[:nl]
    dropped = len(text) - len(head)
    return (
        head
        + f"\n\n---\n_⚠️ Result truncated by Cosmos MCP — {dropped:,} chars "
          f"dropped (cap {limit:,}). Re-query more narrowly (add a filter, "
          f"smaller `limit`, or a specific symbol/path) to see the rest._"
    )


# Recycle bookkeeping — process-scoped (every stdio client spawns its own
# mcp_server process, so these reset naturally between sessions).
_RECYCLE_LOCK = _threading.Lock()
_RECYCLE_START_TS = _time.time()
_RECYCLE_CALL_COUNT = 0
_RECYCLE_LAST_CALL_TS = 0.0
_RECYCLE_INFLIGHT = 0
_RECYCLE_CLIENT_NAME = ""  # lowercased clientInfo.name, captured on first call

# Clients that do NOT transparently respawn the stdio server after it exits —
# they surface the exit as a hard "Server disconnected / Refresh the page"
# error instead of restarting on the next tool call. Claude Desktop AND
# Claude.ai web both report clientInfo.name == "claude-ai" (confirmed in
# ~/Library/Logs/Claude/mcp.log). Recycling under these turns an invisible
# memory-refresh into a user-visible disconnect every few hours — the
# "MCP keeps dropping / Refresh the page every ~6h" report. Claude Code /
# Cursor / Cline DO respawn on the next call, so recycle stays on there.
_RECYCLE_NO_RESPAWN_CLIENTS = {"claude-ai"}


def _note_client_name(name: str) -> None:
    """Record the connecting client's name once (from the initialize handshake's
    clientInfo) so the recycle watchdog can skip clients that don't respawn."""
    global _RECYCLE_CLIENT_NAME
    if name and not _RECYCLE_CLIENT_NAME:
        _RECYCLE_CLIENT_NAME = name.strip().lower()


def _recycle_disabled() -> bool:
    return os.environ.get("COSMOS_MCP_NO_RECYCLE", "").strip() in ("1", "true", "yes")


def _recycle_max_uptime() -> int:
    """Recycle after this many seconds of uptime. Default 6h. <=0 disables
    the uptime trigger (call-count trigger still applies)."""
    raw = os.environ.get("COSMOS_MCP_MAX_UPTIME_SECONDS", "").strip()
    if not raw:
        return 6 * 60 * 60
    try:
        return int(raw)
    except ValueError:
        return 6 * 60 * 60


def _recycle_max_calls() -> int:
    """Recycle after this many successful tool calls. Default 500. <=0
    disables the call-count trigger (uptime trigger still applies)."""
    raw = os.environ.get("COSMOS_MCP_MAX_CALLS", "").strip()
    if not raw:
        return 500
    try:
        return int(raw)
    except ValueError:
        return 500


def _recycle_idle_grace() -> int:
    """Only recycle when the last call finished at least this many seconds
    ago — i.e. the session is idle, so respawn won't interrupt active work.
    Clamped to >=15 so a misconfigured tiny value can't recycle mid-burst."""
    raw = os.environ.get("COSMOS_MCP_RECYCLE_IDLE_GRACE", "").strip()
    if not raw:
        return 60
    try:
        return max(15, int(raw))
    except ValueError:
        return 60


def _note_call_start():
    global _RECYCLE_INFLIGHT
    with _RECYCLE_LOCK:
        _RECYCLE_INFLIGHT += 1


def _note_call_end():
    global _RECYCLE_INFLIGHT, _RECYCLE_CALL_COUNT, _RECYCLE_LAST_CALL_TS
    with _RECYCLE_LOCK:
        _RECYCLE_INFLIGHT = max(0, _RECYCLE_INFLIGHT - 1)
        _RECYCLE_CALL_COUNT += 1
        _RECYCLE_LAST_CALL_TS = _time.time()


def _should_recycle_now() -> tuple[bool, str]:
    """Decide whether the process is due for recycle AND safe to recycle
    right now. Returns (should, reason). Safe = no call in flight and the
    last call is older than the idle grace window."""
    if _recycle_disabled():
        return (False, "")
    # Skip clients that don't transparently respawn — recycling them just
    # surfaces a hard disconnect ("Refresh the page") instead of a silent
    # restart. See _RECYCLE_NO_RESPAWN_CLIENTS.
    if _RECYCLE_CLIENT_NAME in _RECYCLE_NO_RESPAWN_CLIENTS:
        return (False, "")
    now = _time.time()
    with _RECYCLE_LOCK:
        inflight = _RECYCLE_INFLIGHT
        calls = _RECYCLE_CALL_COUNT
        last_ts = _RECYCLE_LAST_CALL_TS
        uptime = now - _RECYCLE_START_TS

    if inflight > 0:
        return (False, "")  # never recycle with a call in flight

    grace = _recycle_idle_grace()
    # If a call has happened, require an idle gap. If no call ever happened,
    # a long-uptime idle process is still fine to recycle (it's a zombie).
    if last_ts > 0 and (now - last_ts) < grace:
        return (False, "")

    max_uptime = _recycle_max_uptime()
    if max_uptime > 0 and uptime >= max_uptime:
        return (True, f"uptime {int(uptime)}s >= {max_uptime}s")

    max_calls = _recycle_max_calls()
    if max_calls > 0 and calls >= max_calls:
        return (True, f"call_count {calls} >= {max_calls}")

    return (False, "")


def _recycle_watchdog_loop(poll_seconds: float = 30.0):
    """Daemon loop: periodically check whether the process is due for an
    idle recycle, and if so exit(0) so the stdio client respawns a fresh
    server. os._exit avoids running atexit/finalizers that could block on
    the very transport we're trying to refresh."""
    while True:
        _time.sleep(poll_seconds)
        try:
            should, reason = _should_recycle_now()
        except Exception:
            continue
        if should:
            try:
                print(f"♻️  Cosmos MCP recycling process (idle) — {reason}. "
                      f"Client will respawn on next tool call.", file=sys.stderr,
                      flush=True)
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)


def _start_recycle_watchdog():
    """Start the recycle watchdog once. No-op when disabled via env."""
    if _recycle_disabled():
        print("ℹ️  MCP process recycle disabled (COSMOS_MCP_NO_RECYCLE).",
              file=sys.stderr)
        return
    t = _threading.Thread(target=_recycle_watchdog_loop, name="mcp-recycle",
                          daemon=True)
    t.start()
    print(f"♻️  MCP recycle watchdog armed — max_uptime={_recycle_max_uptime()}s "
          f"max_calls={_recycle_max_calls()} idle_grace={_recycle_idle_grace()}s.",
          file=sys.stderr)


def create_mcp_server():
    if not MCP_AVAILABLE:
        raise RuntimeError("MCP SDK not installed. Run: pip install mcp (Python 3.10+)")

    server = Server("cosmos")
    perms = get_permission_engine()
    activity = get_activity_log()

    _store = _search = _agg = _reg = None

    def store():
        nonlocal _store
        if not _store: _store = get_store_v2()
        return _store
    def search():
        nonlocal _search
        if not _search: _search = BM25Search(store().conn)
        return _search
    def agg():
        nonlocal _agg
        if not _agg: _agg = UniversalAggregator(store().conn)
        return _agg
    def reg():
        nonlocal _reg
        if not _reg: _reg = get_registry_v2(store().conn)
        return _reg

    # ─────────────────────────────────────────────
    # Tool registry
    # ─────────────────────────────────────────────

    @server.list_tools()
    async def list_tools():
        cats = ", ".join(c["name"] for c in reg().list_categories())
        legacy_tools = [
            Tool(name="brain_search",
                 description="[Cosmos · Memory] Search the user's personal brain (saved notes / decisions / logs) using BM25 (Thai+English). Returns ranked snippets. For CODE searches, prefer `code_search` or `find_relevant_code` — this tool indexes memories, not source files.",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string", "description": "Search query"},
                     "folder": {"type": "string", "description": "Folder path filter (e.g. /Trading)"},
                     "tags": {"type": "array", "items": {"type": "string"}},
                     "category": {"type": "string"},
                     "limit": {"type": "integer", "default": 5}
                 }, "required": ["query"]}),
            Tool(name="brain_get",
                 description="[Cosmos · Memory] Get a memory by ID with full content + typed_data. Pair with `brain_search` (find the id first) or `brain_sitemap` (browse folders for ids).",
                 inputSchema={"type": "object", "properties": {
                     "memory_id": {"type": "string"}
                 }, "required": ["memory_id"]}),
            Tool(name="brain_aggregate",
                 description="[Cosmos · Memory] SQL aggregation across memories: sum / avg / count / group_by / win_rate / top / worst / overview. Use for analytics questions like 'how many trades this month', 'avg PnL by symbol'.",
                 inputSchema={"type": "object", "properties": {
                     "category": {"type": "string"},
                     "operation": {"type": "string", "enum": ["sum", "avg", "count", "min", "max", "group_by", "time_series", "win_rate", "overview", "top", "worst"]},
                     "field": {"type": "string"},
                     "group_field": {"type": "string"},
                     "filters": {"type": "object"}
                 }, "required": ["category", "operation"]}),
            Tool(name="brain_remember",
                 description=f"[Cosmos · Memory] Save a new memory into the user's brain (note / decision / expense / journal / log). Categories: {cats}. For CODE-related bug fixes, prefer `code_remember_error` so the lesson is filed against the project's error log and surfaces on future edits.",
                 inputSchema={"type": "object", "properties": {
                     "content": {"type": "string"},
                     "category": {"type": "string", "default": "note"},
                     "folder": {"type": "string"},
                     "tags": {"type": "array", "items": {"type": "string"}},
                     "typed_data": {"type": "object"}
                 }, "required": ["content"]}),
            Tool(name="brain_status",
                 description="[Cosmos · Orientation] Get brain status, AI tier, capabilities, stats, and your current permissions. Call when the user asks 'what can you do' or 'is local AI on'.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="brain_sitemap",
                 description="[Cosmos · Orientation · CALL FIRST] Returns the full layout of this brain — folders, purposes, "
                             "permissions, category routing, naming conventions, and what "
                             "you can/cannot do. Call this FIRST in a new conversation to "
                             "understand how this user organizes their knowledge before reaching "
                             "for any other Cosmos tool. Pairs with `cosmos_get_preamble` (code side) "
                             "and `brain_session_context` (recent activity).",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="code_search",
                 description="[Cosmos · Code] Search the user's indexed codebase for functions, classes, or keywords. ~50 tokens per result × limit (default ~250 tokens). Cheaper than grep when you also need symbol type + signature inline. For natural-language symptoms (\"app freezes\", \"thai text wrong\"), use `find_relevant_code` instead — it joins this with past lessons. Pass `path` (cwd or any folder inside a watched project) to restrict results to that project — without it, results pool across every watched project sharing the same relative-path namespace.",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string"},
                     "limit": {"type": "integer", "default": 5},
                     "path": {"type": "string", "description": "Scope results to this project path. Optional but recommended in multi-project setups."}
                 }, "required": ["query"]}),
            Tool(name="find_relevant_code",
                 description=(
                     "[Cosmos · Code+Lessons · CALL FIRST for vague symptoms] "
                     "Given a natural-language symptom or task description, return the most likely "
                     "files/symbols + any past errors+fixes that match. Use FIRST when you don't "
                     "know where to look — e.g. 'app freezes on launch' or 'thai text rendering wrong'. "
                     "Combines code FTS + per-project error log; ranks by relevance. "
                     "~500-800 tokens. ONLY tool that surfaces past lessons from prior debug sessions — "
                     "grep cannot replicate this. If a returned lesson matches the symptom, "
                     "APPLY THE FIX rather than re-deriving from scratch."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "symptom": {"type": "string", "description": "Natural-language description of the problem or feature you're investigating."},
                     "path": {"type": "string", "description": "Optional cwd / project path for past-error lookup. Defaults to all watched projects."},
                     "limit": {"type": "integer", "default": 6}
                 }, "required": ["symptom"]}),
            Tool(name="code_find_function",
                 description="[Cosmos · Code] Find a specific function/class definition and get its full body and related calls. ~300-1000 tokens depending on body size. Use when you need the FULL implementation. Prefer `code_get_symbol` with mode='header' if you only need the signature.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_find_callers",
                 description="[Cosmos · Code] Find which functions call the specified function (reverse call graph). AST-accurate, no string false positives from grep. Pair with `code_callees` for the full graph or `code_analyze_refactor_impact` before a rename/refactor. Pass `path` (cwd or any folder inside a watched project) to disambiguate same-named symbols across projects.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"},
                     "path": {"type": "string", "description": "Scope to this project path. Optional; recommended in multi-project setups."}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_explain_project",
                 description="[Cosmos · Code] Get a high-level overview of the project structure, frameworks, entry points, and module statistics. Pass `path` (cwd or any folder inside a watched project) to scope the module list to dirs that actually exist under that project — without it, the overview pools dirs from every watched project sharing the same relative-path namespace and may show modules from other projects (visible as unexpected top-level dirs).",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "Project path to scope the overview to (cwd or any path inside a watched project). Optional but recommended in multi-project setups."}
                 }}),
            Tool(name="code_get_symbol",
                 description=(
                     "[Cosmos · Code] Fetch a code symbol by name. "
                     "Default mode='full' returns signature + body (~300-1500 tokens). "
                     "Use mode='header' when you only need to know WHERE the symbol is "
                     "or what its signature/parameters look like — header mode skips the "
                     "body and is ~5-10x cheaper for common 'where is X defined' questions. "
                     "Use mode='full' when you actually need to read the implementation."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"},
                     "file_path": {"type": "string", "description": "Optional file filter for disambiguation"},
                     "mode": {"type": "string", "enum": ["header", "full"], "default": "full",
                              "description": "'header' = skip body (location + signature + docstring only). 'full' = include body."},
                     "path": {"type": "string", "description": "Scope to this project path (cwd or folder inside a watched project) to disambiguate same-named symbols across projects."}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_callees",
                 description="[Cosmos · Code] What does this function call? Returns the forward call graph — symbols invoked by the named function. ~50 tokens per callee. AST-accurate (no false positives from comments/strings). Pair with `code_find_callers` for the reverse direction. Pass `path` to disambiguate same-named symbols across watched projects.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"},
                     "path": {"type": "string", "description": "Scope to this project path. Optional; recommended in multi-project setups."},
                     "scope_path": {"type": "string", "description": "Deprecated alias for `path` (legacy). Prefer `path`."}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_uses",
                 description="[Cosmos · Code] Where is this identifier (variable/function/class) used? Returns file:line list. ~30 tokens per reference. AST-based — more accurate than grep for common identifiers (no string/comment false positives). Pass `path` to scope to one project in multi-project setups.",
                 inputSchema={"type": "object", "properties": {
                     "identifier": {"type": "string"},
                     "path": {"type": "string", "description": "Scope to this project path. Optional; recommended in multi-project setups."}
                 }, "required": ["identifier"]}),
            Tool(name="code_hierarchy",
                 description="[Cosmos · Code] Drill into the codebase: project → folder → file → symbols. Pass `path` to descend. Prefer `code_skeleton` if you want a flat list of signatures.",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "Empty for project root, or relative path like 'core/api'"}
                 }}),
            Tool(name="code_explain",
                 description="[Cosmos · Code] Lazy LLM annotation of a symbol — only available with Tier 2 (local LLM) or Cloud. Returns natural-language explanation. For raw signature + body without LLM call, use `code_get_symbol` instead.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_skeleton",
                 description="[Cosmos · Code · CALL FIRST for structure] Returns ONLY signatures (no bodies) for a file or whole project. Use this FIRST to understand structure — 95% fewer tokens than reading files (~750 tokens for a typical 50-symbol file vs ~17000 to Read it). Returns class hierarchy with method signatures + types. Pass `path` (cwd or any folder inside a watched project) when omitting `file_path` so the project-wide skeleton excludes rows from sibling watched projects — without it, results pool across every watched project.",
                 inputSchema={"type": "object", "properties": {
                     "file_path": {"type": "string", "description": "Optional: skeleton of one file. Omit for project-wide skeleton."},
                     "max_symbols": {"type": "integer", "default": 200},
                     "path": {"type": "string", "description": "Scope project-wide skeleton to this project path. Ignored when file_path is set. Recommended in multi-project setups."}
                 }}),
            Tool(name="code_context_bundle",
                 description="[Cosmos · Code] One-shot context aggregator — given a query, returns relevant symbols + callers + callees + related decisions in a single call. Saves 5-10 round-trips. Use this when starting work on a feature/area.",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string"},
                     "depth": {"type": "integer", "default": 1, "description": "Hops in call graph (1 = direct only)"}
                 }, "required": ["query"]}),
            Tool(name="code_diff",
                 description="[Cosmos · Code] Git-aware diff of a symbol since a given reference (default: last commit). Returns only what changed, not the whole symbol.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"},
                     "since": {"type": "string", "default": "HEAD~1", "description": "Git ref/commit/relative date"}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_trace_value",
                 description="[Cosmos · Code] Trace a symbol's return value through the codebase, stopping at serialization boundaries (JWT/JSON/ORM/network). Returns the consumers actually reached and the set of paths terminated at boundaries — distinguishing 'no consumer' from 'trace cut'. Use for 'what breaks if I change X return type' style questions.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string", "description": "Function/method whose return value to trace."},
                     "max_depth":   {"type": "integer", "default": 3, "description": "Hops to follow before stopping."},
                     "scope_path": {"type": "string", "description": "Optional file_path prefix (e.g. 'docs_src/security/') to scope same-named symbols to one tutorial/package."}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_analyze_refactor_impact",
                 description="[Cosmos · Code · CALL BEFORE refactor] Composite tool for 'will changing X break anything?' questions. Bundles callers + callees + uses + boundary-respecting trace into a single response with confidence-tier annotations. Prefer this over 5 separate calls. Call BEFORE editing when the user says 'refactor / rename / remove / what depends on X'.",
                 inputSchema={"type": "object", "properties": {
                     "symbol_name": {"type": "string"},
                     "change_kind": {"type": "string", "enum": ["return_type", "signature", "rename", "remove"], "default": "return_type"},
                     "scope_path": {"type": "string", "description": "Optional file_path prefix (e.g. 'docs_src/security/') to scope same-named symbols to one tutorial/package."}
                 }, "required": ["symbol_name"]}),
            Tool(name="code_boundaries",
                 description="[Cosmos · Code] List serialization-boundary call sites (JWT, JSON, ORM, network/queue) in a file or directory. Useful before answering refactor-impact questions to know where the call graph is cut.",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "File or directory inside a watched project."}
                 }, "required": ["path"]}),
            Tool(name="cosmos_get_preamble",
                 description="[Cosmos · Orientation · CALL FIRST in a new session] Fetch the 3-tier project preamble (Hot/Warm/Cold) — Cosmos capability map + top symbols + module map + past lessons + decision rules — in one read. Call at session start with tier='hot' BEFORE orchestrating other tools so you understand what Cosmos can do for this user. Pass `intent` (the user's first prompt or session goal) to augment the preamble with topic-relevant symbols + lessons — closes the gap when the user is about to work on a specific topic the fixed preamble wouldn't cover. Subsequent calls served from a content-addressed cache; pass the hash you have to skip transfer when unchanged.",
                 inputSchema={"type": "object", "properties": {
                     "path":  {"type": "string", "description": "Project path (cwd or any path inside a watched project)."},
                     "tier":  {"type": "string", "enum": ["hot", "warm", "cold"], "default": "hot"},
                     "intent": {"type": "string", "description": "Optional user intent / first prompt. When provided, the hot tier appends a 'Topic-relevant items' section with top code symbols + brain lessons matching the intent (Track 3.5 — closes niche-intent coverage gap)."},
                     "known_hash": {"type": "string", "description": "Hash you already have. Server replies 'unchanged' if it matches."}
                 }, "required": ["path"]}),
            Tool(name="cosmos_get_design_context",
                 description="[Cosmos · UX/UI] Fetch the design-aware system context — including CSS variables, colors, Tailwind theme, breakpoints, typography, and styling choices extracted from this project. Call BEFORE generating UI components to ensure perfect brand visual alignment.",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "Project path (cwd or any path inside a watched project)."}
                 }, "required": ["path"]}),
            Tool(name="cosmos_design_audit",
                 description="[Cosmos · UX/UI] Audit a component file or directory inside a watched project against DESIGN.md and design.tokens.json rules. Identifies design drifts, hardcoded hex colors, radius contract breaches, nested cards, and preventable UI bug vulnerabilities.",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "Project path (cwd or any path inside a watched project)."},
                     "target_path": {"type": "string", "description": "Relative or absolute path to the file or directory to audit."}
                 }, "required": ["path", "target_path"]}),
            Tool(name="brain_session_context",
                 description="[Cosmos · Orientation · CALL FIRST] Auto-context loader — recent activity, open TODOs, last decisions, recently-edited folders. ~200 tokens. Eliminates the 'where was I?' overhead at session start. Pairs with `brain_sitemap` (brain layout) and `cosmos_get_preamble` (code side) — fire all three early when you have no prior context for this user.",
                 inputSchema={"type": "object", "properties": {
                     "lookback_days": {"type": "integer", "default": 7}
                 }}),
            Tool(name="brain_pattern_recall",
                 description="[Cosmos · Memory] Recall user-defined patterns/preferences/style rules. Use BEFORE generating code so output matches user's style. Filter by category (naming/style/architecture/testing) to scope.",
                 inputSchema={"type": "object", "properties": {
                     "category": {"type": "string", "description": "Optional filter: 'naming', 'style', 'architecture', 'testing', etc."}
                 }}),
            Tool(name="code_reindex",
                 description="[Cosmos · Code ops] Force a fresh re-index of a code project and (optionally) register it for live auto-updates. With no args: lists watched projects. With `path`: registers + indexes that path if new, or just re-indexes if already known. Use when the index seems stale or after pulling new code.",
                 inputSchema={"type": "object", "properties": {
                     "project_id": {"type": "string", "description": "Optional. ID of an already-registered project."},
                     "path": {"type": "string", "description": "Optional. Absolute project path. Auto-registers if not yet watched."},
                     "auto_watch": {"type": "boolean", "description": "When auto-registering a new path, enable file watcher. Default true."}
                 }}),
            Tool(name="cosmos_refresh_map",
                 description="[Cosmos · Code ops] Regenerate the Obsidian-style Project Map (MOC) at <project>/.cosmos/project_summary.md — auto-derived architecture, top symbols, module breakdown, entry points, lessons. Day-1 architectural view that does NOT depend on lesson accumulation. Section between COSMOS:MOC:BEGIN/END markers is regenerated; anything outside is preserved (team conventions, ADRs, intent notes). Auto-runs after every reindex; call manually to force a refresh.",
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string", "description": "Project path (cwd or any path inside a watched project)."}
                 }, "required": ["path"]}),
            Tool(name="claude_log_task",
                 description="[Cosmos · Telemetry · CALL AFTER user requests] Log a Claude Code task into the dogfooding telemetry. Call this after finishing a user request — it auto-detects which MCP tools were used since the last log, and records token cost + outcome. Use before starting next task so each entry is scoped correctly.",
                 inputSchema={"type": "object", "properties": {
                     "task":           {"type": "string", "description": "Short description of what the user asked for.", "minLength": 3},
                     "tokens_input":   {"type": "integer", "description": "Input token count from /cost (current session delta)."},
                     "tokens_output":  {"type": "integer", "description": "Output token count from /cost."},
                     "files_edited":   {"type": "array", "items": {"type": "string"},
                                        "description": "Files that were edited (relative paths)."},
                     "compile_errors": {"type": "integer", "default": 0},
                     "retries":        {"type": "integer", "default": 0,
                                        "description": "Number of retries needed before success."},
                     "semantic_bugs":  {"type": "integer", "default": 0,
                                        "description": "Bugs caught by user testing after compile passed."},
                     "outcome":        {"type": "string",
                                        "enum": ["success", "fixed-after-retry", "failed", "abandoned"],
                                        "default": "success"},
                     "duration_min":   {"type": "number"},
                     "notes":          {"type": "string", "description": "Free-form lessons / what surprised the AI."}
                 }, "required": ["task"]}),
            Tool(name="claude_report",
                 description="[Cosmos · Telemetry] Aggregate Claude Code dogfooding logs into a human-readable markdown report. Use period='day'|'week'|'month'|'all'. Call when the user asks 'how am I doing this week' or 'show me the report'.",
                 inputSchema={"type": "object", "properties": {
                     "period": {"type": "string", "enum": ["day", "week", "month", "all"], "default": "week"},
                     "limit":  {"type": "integer", "default": 50}
                 }}),
            Tool(name="code_remember_error",
                 description=(
                     "[Cosmos · Lessons · CALL AFTER non-trivial fixes] "
                     "Save an error+fix you just resolved into the watched "
                     "project's error log so future sessions can avoid it. "
                     "Pass `path` (cwd or any file inside the project) — the "
                     "server auto-routes to the matching watched project. "
                     "Call this AFTER fixing a non-trivial error: root cause "
                     "differed from the message, took >1 try, user reported a "
                     "bug after your work compiled, or anything that surprised "
                     "you. Skip for typos and trial-and-error exploration."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "symptom":        {"type": "string", "description": "Error message + observable behavior."},
                     "root_cause":     {"type": "string", "description": "1–3 sentences: why it actually broke."},
                     "fix":            {"type": "string", "description": "Code/config change applied."},
                     "files_affected": {"type": "array", "items": {"type": "string"}, "description": "Relative paths involved."},
                     "tags":           {"type": "array", "items": {"type": "string"}},
                     "severity":       {"type": "integer", "enum": [1, 2, 3], "default": 2,
                                        "description": "1=blocking, 2=intermittent, 3=cosmetic"},
                     "path":           {"type": "string", "description": "Any path inside the watched project (usually cwd). Used for auto-routing."},
                     "project_id":     {"type": "string", "description": "Override auto-routing with explicit project id."},
                 }, "required": ["symptom"]}),
            Tool(name="code_list_errors",
                 description=(
                     "[Cosmos · Lessons · CALL FIRST before edits] "
                     "List past error+fix entries for a watched project. "
                     "Call FIRST when starting work in a project — and again "
                     "with `file_path` set to the file you're about to edit, "
                     "so you only get entries that touched that file. "
                     "Pass `cross_project=true` to recall lessons from ALL "
                     "watched projects — useful for universal gotchas like "
                     "framework quirks (Tauri, React, FastAPI). ~150-300 "
                     "tokens per entry. If a returned lesson is `pinned` AND "
                     "matches what you were about to do, defer to the lesson "
                     "or surface it to the user before overriding."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "path":          {"type": "string", "description": "Any path inside the watched project (usually cwd) — used for auto-routing."},
                     "project_id":    {"type": "string", "description": "Override auto-routing."},
                     "file_path":     {"type": "string", "description": "Filter to entries whose files_affected contains this relative path."},
                     "min_severity":  {"type": "integer", "enum": [1, 2, 3], "default": 1},
                     "limit":         {"type": "integer", "default": 50},
                     "cross_project": {"type": "boolean", "default": False, "description": "When true, return lessons from ALL watched projects (ignores project_id/path). Results are grouped by project name in output."},
                 }}),
            Tool(name="code_find_file",
                 description=(
                     "[Cosmos · Code] Find indexed source files by filename or path fragment. "
                     "Use when you know the filename (e.g. 'FolderTree.tsx') but "
                     "don't know its directory. Returns matching file paths "
                     "ranked: exact filename → suffix → substring. ~50 tokens "
                     "per result. Cheaper than `find` for the model since results "
                     "come pre-ranked + scoped to indexed code only. Pass `path` "
                     "(cwd or any folder inside a watched project) to filter out "
                     "matches that don't resolve to a file under that project — "
                     "without it, results pool across every watched project."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "name":      {"type": "string", "description": "Filename or path fragment (case-insensitive). e.g. 'FolderTree.tsx', 'Sidebar', 'core/api/'."},
                     "extension": {"type": "string", "description": "Optional extension filter without dot (e.g. 'tsx', 'py'). Filters results to that file type."},
                     "limit":     {"type": "integer", "default": 20},
                     "path":      {"type": "string", "description": "Scope results to this project path. Optional but recommended in multi-project setups."}
                 }, "required": ["name"]}),
            Tool(name="brain_rebuild_links",
                 description="[Cosmos · Brain ops] Re-derive memory↔memory edges from shared tags, same folder, and temporal proximity. Use when the Neural Map looks empty or after importing a batch of memories — this is what makes the graph show filaments instead of orphan stars.",
                 inputSchema={"type": "object", "properties": {
                     "tag_min_overlap":       {"type": "integer", "default": 2,
                                               "description": "Minimum shared tags to connect a pair."},
                     "folder_top_k":          {"type": "integer", "default": 6,
                                               "description": "Max peers per memory inside the same folder (caps hairballs)."},
                     "temporal_window_hours": {"type": "integer", "default": 24,
                                               "description": "Window for temporal cluster edges."},
                     "clear_existing_auto":   {"type": "boolean", "default": True,
                                               "description": "Wipe previous auto-* edges before rebuilding."}
                 }}),
            # ── Phase 0: Control Center foundation tools ──
            # These are gated by per-agent scope when --agent-token is
            # bound (see _enforce_agent_policy). Without a token they
            # run in operator mode (no scope enforcement).
            Tool(name="brain_create_folder",
                 description=(
                     "[Cosmos · Brain ops] Create a brain folder at the given absolute path. "
                     "Walks the path: every missing segment is created idempotently, "
                     "so passing '/Agents/WikiGrapher/concepts' creates "
                     "'/Agents/WikiGrapher' (if missing) and '/concepts' under it. "
                     "Returns the leaf folder_id ready for brain_remember."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "path": {"type": "string",
                              "description": "Absolute brain path, e.g. '/Agents/WikiGrapher/concepts'."}
                 }, "required": ["path"]}),
            Tool(name="brain_delete_folder",
                 description=(
                     "[Cosmos · Brain ops · DESTRUCTIVE — confirm before calling] "
                     "Delete a brain folder. By default refuses to delete a folder that "
                     "contains memories — pass cascade=true to remove the folder and all "
                     "its memories. Disk files are NEVER touched (this is brain metadata only). "
                     "High-trust operation; only available in 'trusted' agent template by default."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "path":    {"type": "string",
                                 "description": "Absolute brain path of the folder to delete."},
                     "cascade": {"type": "boolean", "default": False,
                                 "description": "If true, also delete every memory + sub-folder inside."}
                 }, "required": ["path"]}),
            Tool(name="brain_move_memory",
                 description=(
                     "[Cosmos · Brain ops] Re-parent a memory to a different folder. Same memory id; only "
                     "folder_id changes. Used when an agent realises a memory belongs "
                     "in a more specific sub-folder it just created."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "memory_id":     {"type": "string"},
                     "target_folder": {"type": "string",
                                       "description": "Absolute brain path of the destination folder."}
                 }, "required": ["memory_id", "target_folder"]}),
            Tool(name="brain_create_agent",
                 description=(
                     "[Cosmos · Brain ops · HIGH TRUST] "
                     "Provision a new sub-agent (e.g. 'WikiGrapher', 'Coder'). "
                     "Returns a one-time plaintext auth token + a Claude Desktop config "
                     "snippet the operator pastes into ~/Library/Application "
                     "Support/Claude/claude_desktop_config.json. Subsequent connections "
                     "with that token will be scope-restricted to /Agents/<name>. "
                     "High-trust operation; only available in 'trusted' agent template by default."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "name":     {"type": "string",
                                  "description": "Agent display name. Becomes /Agents/<name> scope."},
                     "template": {"type": "string", "default": "strict",
                                  "enum": ["strict", "standard", "trusted"],
                                  "description": "Permission preset. Strict=read+write-own-scope."}
                 }, "required": ["name"]}),
            Tool(name="brain_link",
                 description=(
                     "[Cosmos · Memory] Create an EXPLICIT, user-intent link between two "
                     "memories — the kind the USER would recognise, NOT generic similarity. "
                     "Call this while working WITH the user, only when you genuinely understand "
                     "two memories are related (one references / elaborates / contradicts / "
                     "continues the other). Be deliberate — a sprayed link is noise. These show "
                     "in the user-perspective graph (unlike auto_temporal / semantic edges, which "
                     "are hidden there). Identify each memory by its id (search first to get ids); "
                     "always include a one-line `why`."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "source_id":     {"type": "string",
                                       "description": "id of the first memory (from a search result)"},
                     "target_id":     {"type": "string",
                                       "description": "id of the second memory"},
                     "relation_type": {"type": "string", "default": "related",
                                       "enum": ["related", "references", "elaborates", "contradicts", "follows"],
                                       "description": "how they relate — user-perspective types only (never auto_*/semantic)"},
                     "why":           {"type": "string",
                                       "description": "one line: the specific relationship you observed. Forces a deliberate, non-spray link."},
                     "weight":        {"type": "number", "default": 0.8,
                                       "description": "0-1 strength; default 0.8 for an explicit link"}
                 }, "required": ["source_id", "target_id", "why"]}),
            Tool(name="brain_update_memory",
                 description=(
                     "[Cosmos · Memory] EDIT an existing memory IN PLACE — change its content, "
                     "typed_data (e.g. set `title` to give a file a proper name + extension like "
                     "\"NAME.md\"), tags, or category. PARTIAL: only the fields you pass change. "
                     "Use this to fix/maintain a file you already created instead of adding a "
                     "duplicate (the brain tools were otherwise add-only). Get the id from a search first."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "memory_id":  {"type": "string", "description": "id of the memory to edit (from a search result)"},
                     "content":    {"type": "string", "description": "new full content (optional)"},
                     "typed_data": {"type": "object", "description": "structured fields to set/override, e.g. {\"title\": \"NAME.md\"} — MERGED into the existing typed_data (your keys win; other keys are preserved) (optional)"},
                     "tags":       {"type": "array", "items": {"type": "string"}, "description": "new tag list (optional)"},
                     "category":   {"type": "string", "description": "new category (optional)"}
                 }, "required": ["memory_id"]}),
            Tool(name="brain_delete_memory",
                 description=(
                     "[Cosmos · Memory] Delete a SINGLE memory by id (NOT a whole folder — that is "
                     "brain_delete_folder). Use to remove one file an agent no longer needs. Get the "
                     "id from a search first. Destructive — confirm with the user for anything you "
                     "did not create yourself."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "memory_id": {"type": "string", "description": "id of the memory to delete (from a search result)"}
                 }, "required": ["memory_id"]}),
        ]

        # Dual-register canonical `cosmos_*` aliases for every legacy
        # `brain_*` / `code_*` tool. Aliases share the legacy tool's
        # inputSchema verbatim and prepend an alias note to the description.
        # This is additive: legacy names still work; new code should prefer
        # `cosmos_memory_*` / `cosmos_code_*`.
        by_legacy = {t.name: t for t in legacy_tools}
        canonical_tools: list[Tool] = []
        for canonical_name, legacy_name in _CANONICAL_ALIASES.items():
            base = by_legacy.get(legacy_name)
            if base is None:
                continue  # drift guard: skip unknown aliases instead of crashing
            canonical_tools.append(
                Tool(
                    name=canonical_name,
                    description=(
                        f"[Canonical alias of `{legacy_name}`] {base.description}"
                    ),
                    inputSchema=base.inputSchema,
                )
            )
        return legacy_tools + canonical_tools

    # ─────────────────────────────────────────────
    # MCP Resources — cosmos:// URIs that any MCP-compatible client
    # can read at session start without invoking a tool. Same purpose
    # as the SKILL.md / .cursor/rules files: tell the AI what Cosmos
    # is and which tool to reach for. This is the protocol-native
    # path; works on clients that auto-load resources (per MCP spec).
    # ─────────────────────────────────────────────

    _COSMOS_CAPABILITIES_DOC = """# Cosmos — AI Memory Layer · Capabilities

Cosmos is the user's local-first memory + code-aware index. It sits
between their folders and you (their AI tool) — you read from it,
write to it, and recall past lessons through MCP.

## Naming (canonical vs legacy aliases)

Every tool below is reachable by two names — pick either, both route
to the same handler:

| Domain  | Canonical prefix    | Legacy prefix (still works) |
|---------|---------------------|------------------------------|
| Memory  | `cosmos_memory_*`   | `brain_*`                    |
| Code    | `cosmos_code_*`     | `code_*`                     |

Prefer `cosmos_*` in new code. Legacy `brain_*` / `code_*` are kept
for backward compat; both surface in `tools/list`.

## Capability map

| Domain | When user asks about… | Reach for |
|---|---|---|
| 💻 **Code** | symbols, functions, who-calls-X, refactor impact | `code_search`, `code_get_symbol`, `code_callers`, `code_callees`, `code_find_function`, `find_relevant_code`, `code_skeleton`, `code_context_bundle`, `code_uses`, `code_diff` |
| 📚 **Lessons** | known bugs, past mistakes, project gotchas | `code_list_errors` (call FIRST before edits), `code_remember_error` (call AFTER non-trivial fix), `find_relevant_code` (joins code+lessons) |
| 📝 **Memory** | saved notes, decisions, research, project log | `brain_search`, `brain_get`, `brain_remember`, `brain_aggregate`, `brain_pattern_recall` |
| 🗺️ **Orientation** | session start, "what is this project", "where am I" | `brain_sitemap` + `brain_session_context` (call FIRST), `cosmos_get_preamble(intent=user's first message)` |
| 🔧 **Project ops** | reindex, refresh map, watched folders | `code_reindex`, `cosmos_refresh_map`, `code_find_file`, `code_explain_project`, `code_hierarchy` |

## Decision rules

- User mentions **code structure / files / symbols** → start with `code_*`,
  NOT a broad `brain_search`.
- User mentions **a saved note / decision / past idea** → start with `brain_*`.
- User describes a **symptom with no clear scope** → call `find_relevant_code`
  first (joins code + past lessons in one round-trip).
- **Before editing any file in a watched project**: call `code_list_errors`
  so prior lessons surface BEFORE the change, not after.
- **After fixing a non-trivial error**: call `code_remember_error`.

## Behavioral guardrails

- Pinned + scope-globbed lessons carry stronger weight than your own
  re-derivation. If a lesson contradicts your plan, surface it to the
  user before proceeding.
- Broad `brain_search` without category/folder filter is a fallback,
  not a starting point.
- Destructive ops (delete, overwrite) require user confirmation.

For per-project context (top symbols + recent lessons + module map),
read `cosmos://preamble/current` or call `cosmos_get_preamble` with
your cwd. For the brain folder layout, read `cosmos://sitemap` or
call `brain_sitemap`.
"""

    _COSMOS_ONBOARDING_DOC = """# Cosmos — First-Time Onboarding for AI Clients

You've just connected to a Cosmos MCP server. Do these three things at
the start of every new conversation:

1. **Read `cosmos://capabilities`** (or call `cosmos_get_preamble` with
   the user's first prompt as `intent`). Tells you which Cosmos tool
   matches which user intent — saves you from wasting a round-trip on
   the wrong tool.

2. **Call `brain_sitemap`** once per session. Tells you how this user
   organises their folders + which categories they actually use. You
   can't write a memory to the right folder without this.

3. **Call `brain_session_context`** once per session. Returns the
   user's recent activity, open TODOs, last decisions — ~200 tokens
   that eliminate the "where was I?" question.

After those three reads, you have:
   • A capability map (what Cosmos does)
   • A sitemap (how this user's brain is shaped)
   • Session context (what they were last working on)

That's enough context to route every subsequent request correctly.

## Before editing code in a watched project

Add a 4th call: `code_list_errors(path=cwd)`. This returns past
fixes / pinned lessons for this project. If a lesson matches what
you were about to do, prefer the lesson over re-deriving the fix.

## After fixing a non-trivial error

Call `code_remember_error(path=cwd, symptom=..., root_cause=...,
fix=...)`. Future sessions (you, the user, or a different agent)
will see it via `code_list_errors` and avoid the same trap.
"""

    @server.list_resources()
    async def list_resources():
        return [
            Resource(
                uri=AnyUrl("cosmos://capabilities"),
                name="Cosmos Capabilities Map",
                description="What Cosmos is + which tool maps to which user intent. Read FIRST at session start.",
                mimeType="text/markdown",
            ),
            Resource(
                uri=AnyUrl("cosmos://onboarding"),
                name="Cosmos First-Session Checklist",
                description="3-step bootstrap for any AI client newly connected to Cosmos. Walks through capabilities → sitemap → session context.",
                mimeType="text/markdown",
            ),
            Resource(
                uri=AnyUrl("cosmos://sitemap"),
                name="Cosmos Brain Sitemap",
                description="Live brain folder layout — folders, purposes, permissions, naming conventions. Same content as `brain_sitemap` tool, exposed as a resource for clients that prefer resources over tools.",
                mimeType="text/markdown",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri):
        uri_str = str(uri)
        if uri_str == "cosmos://capabilities":
            return [ReadResourceContents(content=_COSMOS_CAPABILITIES_DOC, mime_type="text/markdown")]
        if uri_str == "cosmos://onboarding":
            return [ReadResourceContents(content=_COSMOS_ONBOARDING_DOC, mime_type="text/markdown")]
        if uri_str == "cosmos://sitemap":
            # Reuse the same generator as the brain_sitemap tool so the
            # resource view is byte-for-byte identical with the tool view.
            try:
                tc = _handle_sitemap(store, reg, perms)
                text = tc[0].text if tc and hasattr(tc[0], "text") else str(tc)
            except Exception as e:
                text = f"Sitemap unavailable as resource ({e}); call the `brain_sitemap` tool instead."
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        raise ValueError(f"Unknown Cosmos resource URI: {uri_str}")

    # ─────────────────────────────────────────────
    # Tool dispatcher with permission + activity log
    # ─────────────────────────────────────────────

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        # Reload permissions on each call so user changes take effect immediately
        perms.reload()

        # Resolve canonical `cosmos_*` aliases down to their legacy handler
        # name. Permission engine, agent whitelist, scope checks, and the
        # dispatch switch all key off legacy names. Activity log records the
        # name AS CALLED (so adoption of new aliases is measurable) and
        # tags the resolved legacy name so analytics can group both.
        resolved = _resolve_canonical(name)

        # Capture the connecting client's name once (from the initialize
        # handshake) so the recycle watchdog can skip clients that don't
        # transparently respawn after a process exit — Claude Desktop /
        # Claude.ai web ("claude-ai") show a hard disconnect instead.
        if not _RECYCLE_CLIENT_NAME:
            try:
                _ci = server.request_context.session.client_params.clientInfo
                _note_client_name(getattr(_ci, "name", "") or "")
            except Exception:
                pass

        def _user_facing(reason: str) -> str:
            """Internal perm checks key off the resolved (legacy) name, so
            their error messages mention `brain_search` even when the user
            called `cosmos_memory_search`. Swap the legacy name back to the
            as-called name so the user sees the tool they actually invoked."""
            if name == resolved:
                return reason
            return reason.replace(f"'{resolved}'", f"'{name}'")

        # 0. AI Control Center — agent-token enforcement (Phase 0).
        # Runs BEFORE the legacy permission engine so the rejection
        # reason cites the agent + scope + tool whitelist explicitly.
        # When no agent token is bound (operator mode), this is a
        # no-op and behaviour is unchanged.
        ok, reason = _enforce_agent_policy(resolved, arguments)
        if not ok:
            activity.record(name, arguments, status="denied", error=reason)
            return [TextContent(type="text", text=f"❌ Permission denied: {_user_facing(reason)}")]

        # 1. Tool-level permission check
        ok, reason = perms.can_call_tool(resolved)
        if not ok:
            activity.record(name, arguments, status="denied", error=reason)
            return [TextContent(type="text", text=f"❌ Permission denied: {_user_facing(reason)}")]

        # 2. Dispatch with timing + log. Track in-flight count so the
        # recycle watchdog never exits while a call is being serviced.
        _note_call_start()
        with Timer() as t:
            try:
                result_text, result_summary = await asyncio.get_running_loop().run_in_executor(
                    _DISPATCH_POOL, _dispatch,
                    resolved, arguments, store, search, agg, reg, perms
                )
                activity.record(
                    name, arguments,
                    status="ok",
                    duration_ms=t.duration_ms if hasattr(t, "duration_ms") else 0,
                    result_summary=result_summary,
                )
                # Cap oversized payloads BEFORE appending the reminder so a
                # runaway result can't stall the stdio transport (lesson
                # 78c5b62a) and the reminder banner survives the trim.
                result_text = _trim_payload(result_text)
                # Solution 3 — long-session drift guard. Appends a
                # "default flow" reminder every N calls. Disabled when
                # COSMOS_NO_REMINDER=1. Keyed off the resolved (legacy)
                # name so the skip-set works regardless of alias.
                result_text = _maybe_append_reminder(resolved, result_text)
                return [TextContent(type="text", text=result_text)]
            except PermissionError as pe:
                activity.record(name, arguments, status="denied",
                                duration_ms=getattr(t, "duration_ms", 0),
                                error=str(pe))
                return [TextContent(type="text", text=f"❌ {pe}")]
            except Exception as e:
                activity.record(name, arguments, status="error",
                                duration_ms=getattr(t, "duration_ms", 0),
                                error=str(e))
                return [TextContent(type="text", text=f"❌ Error: {e}")]
            finally:
                _note_call_end()

    return server


# Tool handlers are synchronous (SQLite + BM25, no awaits) — running them
# directly on the asyncio event loop meant one slow call (e.g. code_search on
# a big index, or serialising a large result) starved the MCP stdio transport,
# so EVERY following call hung until the client's ~4-min timeout, then it
# respawned the server = the intermittent "MCP keeps dropping" behaviour. We
# now run _dispatch on a dedicated single worker thread (open_sqlite uses
# check_same_thread=False; single worker serialises so the shared connection
# is never touched by two threads at once) — the event loop stays free to
# service stdio, so a heavy call never stalls the others.
_DISPATCH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-dispatch")


def _dispatch(name, arguments, store, search, agg, reg, perms):
    """Returns (full_result_text, short_summary_for_log). Synchronous —
    invoked via run_in_executor so it never blocks the stdio event loop."""

    if name == "brain_search":
        return _handle_search(arguments, search, perms)
    if name == "brain_get":
        return _handle_get(arguments, store, perms)
    if name == "brain_aggregate":
        return _handle_aggregate(arguments, agg, perms)
    if name == "brain_remember":
        return _handle_remember(arguments, store, perms)
    if name == "brain_create_folder":
        return _handle_create_folder(arguments, store)
    if name == "brain_delete_folder":
        return _handle_delete_folder(arguments, store)
    if name == "brain_move_memory":
        return _handle_move_memory(arguments, store)
    if name == "brain_create_agent":
        return _handle_create_agent(arguments, store)
    if name == "brain_link":
        return _handle_link(arguments, store)
    if name == "brain_update_memory":
        return _handle_update_memory(arguments, store)
    if name == "brain_delete_memory":
        return _handle_delete_memory(arguments, store)
    if name == "brain_status":
        return _handle_status(store, reg, perms)
    if name == "brain_sitemap":
        return _handle_sitemap(store, reg, perms)
    if name == "code_search":
        return _handle_code_search(arguments, store)
    if name == "find_relevant_code":
        return _handle_find_relevant_code(arguments, store)
    if name == "code_find_function":
        return _handle_code_find_function(arguments, store)
    if name == "code_find_callers":
        return _handle_code_find_callers(arguments, store)
    if name == "code_explain_project":
        return _handle_code_explain_project(arguments, store)
    if name == "code_get_symbol":
        return _handle_code_get_symbol(arguments, store)
    if name == "code_callees":
        return _handle_code_callees(arguments, store)
    if name == "code_uses":
        return _handle_code_uses(arguments, store)
    if name == "code_hierarchy":
        return _handle_code_hierarchy(arguments, store)
    if name == "code_explain":
        return _handle_code_explain(arguments, store)
    if name == "code_skeleton":
        return _handle_code_skeleton(arguments, store)
    if name == "code_context_bundle":
        return _handle_code_context_bundle(arguments, store)
    if name == "code_diff":
        return _handle_code_diff(arguments, store)
    if name == "code_trace_value":
        return _handle_code_trace_value(arguments, store)
    if name == "code_analyze_refactor_impact":
        return _handle_code_analyze_refactor_impact(arguments, store)
    if name == "code_boundaries":
        return _handle_code_boundaries(arguments, store)
    if name == "cosmos_get_preamble":
        return _handle_cosmos_get_preamble(arguments, store)
    if name == "cosmos_get_design_context":
        return _handle_cosmos_get_design_context(arguments, store)
    if name == "cosmos_design_audit":
        return _handle_cosmos_design_audit(arguments, store)
    if name == "brain_session_context":
        return _handle_brain_session_context(arguments, store)
    if name == "brain_pattern_recall":
        return _handle_brain_pattern_recall(arguments, store)
    if name == "code_reindex":
        return _handle_code_reindex(arguments)
    if name == "cosmos_refresh_map":
        return _handle_cosmos_refresh_map(arguments, store)
    if name == "claude_log_task":
        return _handle_claude_log_task(arguments, store)
    if name == "claude_report":
        return _handle_claude_report(arguments, store)
    if name == "brain_rebuild_links":
        return _handle_brain_rebuild_links(arguments, store)
    if name == "code_remember_error":
        return _handle_code_remember_error(arguments)
    if name == "code_list_errors":
        return _handle_code_list_errors(arguments)
    if name == "code_find_file":
        return _handle_code_find_file(arguments, store)
    return f"Unknown tool: {name}", "unknown_tool"


def _handle_code_remember_error(arguments):
    """Save an error+fix entry, auto-routing to the watched project that owns
    the supplied path (cwd or any file inside the project)."""
    from core.code_indexer.errors import get_code_errors, resolve_project_id
    from core.code_indexer.project_registry import get_project_registry

    symptom = (arguments.get("symptom") or "").strip()
    if not symptom:
        return "❌ symptom is required", "missing_symptom"

    project_id = arguments.get("project_id")
    path = arguments.get("path")
    files_affected = arguments.get("files_affected") or []

    resolved_pid = None
    resolved_ppath = None
    if path:
        _, resolved_pid, resolved_ppath, _ = _normalize_file_path(None, path)

    normalized_files = []
    for f in files_affected:
        norm_f, f_pid, f_ppath, _ = _normalize_file_path(f, path)
        if norm_f:
            normalized_files.append(norm_f)
            if not resolved_pid and f_pid:
                resolved_pid = f_pid
                resolved_ppath = f_ppath
        else:
            normalized_files.append(f)

    if resolved_ppath:
        path = resolved_ppath
    if not project_id and resolved_pid:
        project_id = resolved_pid
    if not project_id and path:
        project_id = resolve_project_id(path)
    if not project_id:
        return ("❌ Cannot route this error to a watched project. "
                "Pass `path` (cwd or file inside a watched project) "
                "or explicit `project_id`.", "no_project_match")

    proj = get_project_registry().get(project_id)
    if not proj:
        return f"❌ Unknown project_id: {project_id}", "unknown_project"

    entry = get_code_errors().add(
        project_id=project_id,
        symptom=symptom,
        root_cause=arguments.get("root_cause"),
        fix=arguments.get("fix"),
        files_affected=normalized_files,
        tags=arguments.get("tags") or [],
        severity=int(arguments.get("severity") or 2),
    )
    summary = f"saved error in {proj['name']}: {symptom[:60]}"
    text = (f"✅ Saved to project '{proj['name']}' (id={project_id})\n"
            f"error_id: {entry['id']}\n"
            f"severity: {entry['severity']}  files: {entry['files_affected']}")
    return text, summary


def _staleness_label(project_path, commit_hash, threshold=10) -> str:
    """Render a small staleness flag for a lesson based on commits since saved.
    Returns '' when unknown (no git, no hash, etc.) so output stays clean."""
    from core.code_indexer.errors import commits_since
    if not project_path or not commit_hash:
        return ""
    n = commits_since(project_path, commit_hash)
    if n is None:
        return ""
    if n == 0:
        return " · ✅ same commit"
    if n < threshold:
        return f" · 🟢 {n} commits since"
    return f" · ⚠️ {n} commits since (may be stale)"


def _project_last_indexed(scope_path: str) -> str | None:
    """Project's last_indexed_at (UTC-Z) for a scope path, or None when the
    path isn't a watched project. Cheap registry lookup — call once per tool
    invocation, not per result row."""
    if not scope_path:
        return None
    try:
        from core.code_indexer.project_registry import get_project_registry
        proj = get_project_registry().find_by_path(scope_path)
        return (proj or {}).get("last_indexed_at")
    except Exception:
        return None


def _dirty_flag(scope_path: str, rel_path: str, last_indexed_at: str | None) -> str:
    """' · ⚠️ dirty' when the on-disk file changed after the project was last
    indexed — tells the reader the indexed view may be behind, read fresh.

    Empty string when undecidable (no scope / no timestamp / file gone) so
    absence reads as "can't tell", never a false "clean". Best-effort: never
    throws, never blocks. `last_indexed_at` is UTC-Z (project_registry._now_iso),
    truncated to whole seconds — hence the +1s guard below to avoid a false
    dirty on files modified within the same second the index recorded."""
    if not (scope_path and rel_path and last_indexed_at):
        return ""
    import os
    from datetime import datetime
    try:
        idx = datetime.fromisoformat(last_indexed_at.replace("Z", "+00:00")).timestamp()
        if os.stat(os.path.join(scope_path, rel_path)).st_mtime > idx + 1.0:
            return " · ⚠️ dirty (edited after index — read fresh)"
    except (OSError, ValueError):
        pass
    return ""


# Real HTML *presentation* tags only — the ones TipTap (tiptap-markdown
# html:true) can leak into stored memory content. Deliberately an allowlist,
# NEVER a blind `<[^>]+>`: the live corpus is full of `<your-name>` prompt
# placeholders, `"<string>"` JSON-schema stubs, and `Vec<String>` generics
# (measured: 29 of 30 angle-bracket rows are exactly these; only 1 is real
# HTML). A blind stripper would silently corrupt all 29. The `\b` after each
# tag name is load-bearing — it stops `<s>` from eating `<string>` and `<a>`
# from eating `<article>`. Mirrors core.code_indexer.errors._strip_xml_tags'
# proven "conservative allowlist so prose `<`/`>` survives" pattern, but for
# presentation tags rather than MCP-protocol tags.
_HTML_PRESENTATION_TAG_RE = re.compile(
    r"</?(?:p|div|span|strong|b|em|i|u|s|ul|ol|li|br|hr|h[1-6]|a|img|"
    r"blockquote|code|pre|table|thead|tbody|tfoot|tr|td|th|figure|"
    r"figcaption|mark|sub|sup|del|ins|small|caption)\b[^>]*>",
    re.IGNORECASE,
)


def _to_plaintext(s):
    """Read-time projection of stored memory content (Markdown that may carry
    embedded TipTap HTML) into plain text for the AI-facing MCP boundary.

    This is the lightest form of "AI view = a projection of the user's view":
    the stored content is never mutated — only what crosses into an MCP result
    is shaped, so the AI reads the *meaning*, not the user's rich-text markup.

    Conservative by construction: strips ONLY real HTML presentation tags (see
    _HTML_PRESENTATION_TAG_RE) then decodes entities, so prose angle brackets
    survive. No-op fast path when the text has no `<`/`&` at all — the ~99.9%
    case in the live corpus — so clean Markdown pays nothing."""
    if not s or ("<" not in s and "&" not in s):
        return s
    import html as _html
    return _html.unescape(_HTML_PRESENTATION_TAG_RE.sub("", s))


def _norm_text(t):
    """Normalize text for near-duplicate detection: drop markdown markers
    (#, *, `), collapse the FTS ellipsis and all whitespace, lowercase. Used to
    tell when one rendered line just echoes another (e.g. a Summary that merely
    repeats a Snippet) despite cosmetic differences in markers/truncation."""
    if not t:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[#*`]", "", t).replace("...", " ")).strip().lower()


# Relationship edges split into two kinds. auto_temporal ("written near each
# other") and auto_folder ("same folder") are LAYOUT edges — they exist so the
# 2D graph view looks connected and carry no semantic signal for an AI reader
# (the folder path is already shown; time-adjacency means nothing). Every other
# type — auto_tag (shared topic), code_dep (code dependency), and any future
# explicit/manual link — is a KNOWLEDGE edge worth surfacing. We exclude the
# layout set and include the complement, so new link types light up
# automatically without touching this list. (Measured on the live corpus:
# 94% of edges are layout noise; only ~13% of memories have a knowledge edge,
# so most brain_get calls show no Related section — that is correct, not a gap.)
_LAYOUT_RELATION_TYPES = ("auto_temporal", "auto_folder")


def _related_memories(conn, mid, limit=5):
    """Knowledge-graph neighbors of a memory, for the AI view.

    The app's graph shows the user how a note connects to the rest of their
    brain; brain_get otherwise hands the AI a flat blob with that context
    stripped. This restores it — the highest-weight *semantically meaningful*
    neighbors (layout-noise edges excluded), each as {id, relation, gist}.

    Edges are stored directionally, so a node's neighbors live under either
    source_id or target_id — union both ends. Over-fetch then dedupe so a pair
    joined by several edge types (or an unresolvable id) doesn't crowd out real
    neighbors. Returns [] on any error or when the node has no knowledge edge —
    the caller then emits nothing rather than padding with noise."""
    placeholders = ",".join("?" * len(_LAYOUT_RELATION_TYPES))
    try:
        rows = conn.execute(
            "SELECT CASE WHEN source_id=? THEN target_id ELSE source_id END AS other, "
            "relation_type, weight FROM relationships "
            f"WHERE (source_id=? OR target_id=?) AND relation_type NOT IN ({placeholders}) "
            "ORDER BY weight DESC LIMIT ?",
            (mid, mid, mid, *_LAYOUT_RELATION_TYPES, limit * 3),
        ).fetchall()
    except Exception:
        return []
    out, seen = [], set()
    for other, rtype, _w in rows:
        if not other or other == mid or other in seen:
            continue
        seen.add(other)
        r = conn.execute("SELECT summary FROM memories_v2 WHERE id=?", (other,)).fetchone()
        if not r:
            continue
        gist = _to_plaintext((r[0] or "").strip())
        out.append({"id": other, "relation": rtype, "gist": gist[:160]})
        if len(out) >= limit:
            break
    return out


def _handle_code_list_errors(arguments):
    """List error+fix entries for a project. Auto-routes via `path` if no
    `project_id`. Filter by `file_path` to get only entries touching that file.
    Pass `cross_project=true` to recall lessons across ALL watched projects."""
    from core.code_indexer.errors import (
        get_code_errors,
        lesson_hygiene_nudge,
        resolve_project_id,
    )
    from core.code_indexer.project_registry import get_project_registry

    cross = bool(arguments.get("cross_project"))
    file_path = arguments.get("file_path")
    min_sev = int(arguments.get("min_severity") or 1)
    limit = int(arguments.get("limit") or 50)
    registry = get_project_registry()
    errors = get_code_errors()

    # Normalize file_path and path
    path_arg = arguments.get("path")
    norm_file, resolved_pid, resolved_ppath, _ = _normalize_file_path(file_path or None, path_arg or None)
    if norm_file:
        file_path = norm_file

    if cross:
        # Walk every watched project, merge lessons, group by project name.
        if hasattr(registry, "list"):
            projects = registry.list()
        else:
            # Fallback: scrape distinct project_ids straight from code_errors.
            cur = errors.conn.cursor()
            cur.execute("SELECT DISTINCT project_id FROM code_errors")
            project_ids = [r[0] for r in cur.fetchall()]
            projects = [registry.get(pid) for pid in project_ids if registry.get(pid)]

        grouped = []
        total = 0
        for proj in projects:
            if not proj:
                continue
            rows = errors.list_for_project(
                proj["id"], file_path=file_path,
                min_severity=min_sev, limit=limit,
            )
            if rows:
                grouped.append((proj["name"], rows))
                total += len(rows)

        if total == 0:
            return ("No past errors recorded across watched projects.", "no_errors_cross")

        lines = [f"# Past errors across {len(grouped)} project(s) — {total} entries total", ""]
        # Per-project path lookup for staleness — projects list is small, build a map
        proj_path_map = {p["name"]: p.get("path") for p in projects if p}
        for proj_name, rows in grouped:
            lines.append(f"## 📁 {proj_name} ({len(rows)})")
            lines.append("")
            ppath = proj_path_map.get(proj_name)
            for r in rows:
                sev = {1: "🔴", 2: "🟡", 3: "🟢"}.get(r["severity"], "⚪")
                stale = _staleness_label(ppath, r.get("commit_hash"))
                lines.append(f"### {sev} sev{r['severity']} · seen×{r['times_seen']} · {r['last_seen_at']}{stale}")
                lines.append(f"**Symptom:** {r['symptom']}")
                if r.get("root_cause"):
                    lines.append(f"**Root cause:** {r['root_cause']}")
                if r.get("fix"):
                    lines.append(f"**Fix:** {r['fix']}")
                if r.get("files_affected"):
                    lines.append(f"**Files:** {', '.join(r['files_affected'])}")
                if r.get("tags"):
                    lines.append(f"**Tags:** {', '.join(r['tags'])}")
                lines.append(f"_id: {r['id']}_")
                lines.append("")
        return "\n".join(lines), f"cross_project listed {total} errors across {len(grouped)} projects"

    # Single-project path (original behavior)
    project_id = arguments.get("project_id")
    path = arguments.get("path")
    if resolved_ppath:
        path = resolved_ppath
    if not project_id and resolved_pid:
        project_id = resolved_pid
    if not project_id and path:
        project_id = resolve_project_id(path)
    if not project_id:
        return ("❌ Pass `path` (any path inside a watched project), "
                "`project_id`, or `cross_project=true` for global lookup.",
                "no_project_match")

    proj = registry.get(project_id)
    if not proj:
        return f"❌ Unknown project_id: {project_id}", "unknown_project"

    rows = errors.list_for_project(
        project_id, file_path=file_path,
        min_severity=min_sev, limit=limit,
    )
    if not rows:
        return f"No past errors recorded for '{proj['name']}'.", "no_errors"

    lines = [f"# Past errors in '{proj['name']}' ({len(rows)})", ""]
    ppath = proj.get("path")
    for r in rows:
        sev = {1: "🔴", 2: "🟡", 3: "🟢"}.get(r["severity"], "⚪")
        stale = _staleness_label(ppath, r.get("commit_hash"))
        lines.append(f"## {sev} sev{r['severity']} · seen×{r['times_seen']} · {r['last_seen_at']}{stale}")
        lines.append(f"**Symptom:** {r['symptom']}")
        if r.get("root_cause"):
            lines.append(f"**Root cause:** {r['root_cause']}")
        if r.get("fix"):
            lines.append(f"**Fix:** {r['fix']}")
        if r.get("files_affected"):
            lines.append(f"**Files:** {', '.join(r['files_affected'])}")
        if r.get("tags"):
            lines.append(f"**Tags:** {', '.join(r['tags'])}")
        lines.append(f"_id: {r['id']}_")
        lines.append("")

    # Auto-memory nudge — only when not filtered to one file (the user is
    # looking at the project's lesson set as a whole, so it's the right
    # moment to flag drift since the last recorded lesson).
    if not file_path:
        nudge = lesson_hygiene_nudge(ppath, project_id)
        if nudge:
            lines.append(nudge)
            lines.append("")

    return "\n".join(lines), f"listed {len(rows)} errors"


def _handle_code_find_file(arguments, store):
    """Filename / path-fragment lookup over indexed code. Ranked: exact filename
    match → suffix match → substring match. Returns distinct file paths only."""
    name = (arguments.get("name") or "").strip()
    if not name:
        return ("❌ Pass a filename or path fragment via `name`.", "no_name")

    extension = (arguments.get("extension") or "").strip().lstrip(".")
    limit = int(arguments.get("limit") or 20)
    needle = name.lower()
    scope_path = (arguments.get("path") or "").strip()
    if scope_path:
        _, _, resolved_ppath, _ = _normalize_file_path(None, scope_path)
        if resolved_ppath:
            scope_path = resolved_ppath
        else:
            scope_path = os.path.realpath(os.path.expanduser(scope_path))

    cursor = store().conn.cursor()
    sql = "SELECT DISTINCT file_path FROM code_index WHERE LOWER(file_path) LIKE ?"
    params = [f"%{needle}%"]
    if extension:
        sql += " AND LOWER(file_path) LIKE ?"
        params.append(f"%.{extension.lower()}")
    cursor.execute(sql, params)
    rows = [r[0] for r in cursor.fetchall() if r[0]]

    # Drop matches that don't resolve to a file on disk under scope_path —
    # without project_id on code_index, the relative-path namespace mixes
    # files from every watched project (e.g. searching `App.tsx` would
    # also return webview-ui/src/App.tsx from a sibling repo).
    if scope_path:
        rows = [p for p in rows if os.path.isfile(os.path.join(scope_path, p))]

    if not rows:
        return (f"No indexed file matches '{name}'.", f"file_q='{name}' results=0")

    def rank(path: str) -> tuple:
        base = os.path.basename(path).lower()
        if base == needle:                  return (0, len(path))
        if base.startswith(needle):         return (1, len(path))
        if needle in base:                  return (2, len(path))
        return (3, len(path))
    rows.sort(key=rank)
    rows = rows[:limit]

    lines = [_scope_header() + f"Found {len(rows)} file(s) matching '{name}':", ""]
    for p in rows:
        lines.append(f"- {p}")
    return ("\n".join(lines), f"file_q='{name}' results={len(rows)}")


def _handle_search(arguments, search, perms):
    folder = arguments.get("folder")
    if folder:
        ok, reason = perms.can_read_folder(folder)
        if not ok:
            raise PermissionError(reason)

    cat = arguments.get("category")
    if cat and not perms.category_allowed(cat):
        raise PermissionError(f"Category '{cat}' is hidden by user policy.")

    filters = {}
    if folder: filters["folder_path"] = folder
    if arguments.get("tags"): filters["tags"] = arguments["tags"]
    if cat: filters["category"] = cat

    # Issue #13: route NL queries through _fts_safe_query like code_search does.
    # Without this, "Where in our work did smart reminders come up?" returns 0
    # because FTS5's AND-default conjoins stopwords ("where", "in", "our",
    # "did", "come", "up") that are not present in any memory's content.
    # _fts_safe_query strips stopwords + OR-joins quoted tokens, mirroring
    # what code_search / find_relevant_code already do.
    raw_q = arguments["query"]
    safe_q = _fts_safe_query(raw_q)
    results = search().search(safe_q, filters, arguments.get("limit", 5))

    # Filter out denied folders/categories from results
    visible = []
    for r in results:
        rcat = r.get("category", "")
        if rcat and not perms.category_allowed(rcat):
            continue
        rpath = r.get("folder_path", "")
        if rpath:
            ok_read, _ = perms.can_read_folder(rpath)
            if not ok_read:
                continue
        visible.append(r)

    if not visible:
        # Helpful nudge: brain_search only hits memories, not code.
        # If the query looks code-shaped (camelCase, snake_case, file
        # extension, ::scope syntax) the user almost certainly wanted
        # find_relevant_code. Don't overdo it — only suggest when the
        # query strongly looks like an identifier rather than free
        # text, otherwise it's noise on every empty search.
        q = arguments.get("query", "")
        looks_codey = bool(
            re.search(r"\b\w*[A-Z]\w*[A-Z]\w*\b", q)            # camelCase / PascalCase
            or re.search(r"\b\w+_\w+\b", q)                      # snake_case
            or re.search(r"\.(py|ts|tsx|js|jsx|rs|sql|sh)\b", q) # file extension
            or "::" in q                                          # Rust scope
            or q.startswith(("def ", "class ", "function ", "fn "))
        )
        if looks_codey:
            tip = (
                "\n\n💡 Looks like a code-shaped query. "
                "Try `find_relevant_code` instead — it searches your AST "
                "index + project lessons, not just memories."
            )
            return ("No results found." + tip, f"q='{q}' results=0 (code-tip)")
        return ("No results found.", f"q='{q}' results=0")

    lines = [f"Found {len(visible)} results for '{arguments['query']}':\n"]
    citations = []
    for i, r in enumerate(visible, 1):
        lines.append(f"--- Result {i} ---")
        lines.append(f"ID: {r['id']} | Category: {r.get('category', '?')}")
        if r.get("folder_path"):
            lines.append(f"Folder: {r['folder_path']}")
        if r.get("tags"):
            lines.append(f"Tags: {', '.join('#' + t for t in r['tags'])}")
        snippet = _to_plaintext(r.get("snippet") or (r.get("content", "") or "")[:200])
        # AI-lean view: surface the memory's gist (the extractive summary, present
        # on every row) so a search result is self-describing — the AI gets what
        # the memory is ABOUT without a follow-up brain_get. Distinct from Snippet,
        # which is only the FTS match fragment ("why this matched").
        #
        # Echo-suppression: skip Summary when it just repeats the Snippet. For
        # SHORT structured notes (trades, journal, calendar — the bulk of many
        # folders) the extractive summary IS the top-of-content, and when the
        # match is also at the top the snippet shows that same head, so the two
        # run ~95% identical (only markers/truncation differ — which a literal
        # substring test misses). Compare on normalized text and drop Summary
        # when one contains the other OR they share the same ~64-char head. Long
        # notes — summary = a deep extract, snippet = a different fragment — keep
        # both, which is exactly where Summary earns its place.
        summary = _to_plaintext((r.get("summary") or "").strip())
        ns, nsnip = _norm_text(summary), _norm_text(snippet)
        echo = bool(ns) and (ns in nsnip or nsnip in ns or ns[:64] == nsnip[:64])
        if summary and not echo:
            lines.append(f"Summary: {summary}")
        lines.append(f"Snippet: {snippet}")
        td = {k: v for k, v in (r.get("typed_data") or {}).items() if v not in (None, "", 0, 0.0)}
        if td:
            lines.append(f"Data: {json.dumps(td, ensure_ascii=False)}")
        lines.append("")
        citations.append({
            "memory_id": r["id"],
            "category": r.get("category"),
            "folder": r.get("folder_path"),
            "confidence": "exact" if "snippet" in r else "fuzzy",
        })

    # Embed citations as a structured footer — AI can verify each claim
    lines.append(f"**Citations** ({len(citations)} verified sources):")
    for c in citations:
        loc = c.get("folder") or "<root>"
        lines.append(f"- `{c['memory_id'][:8]}…` in {loc} ({c['confidence']})")

    return ("\n".join(lines),
            f"q='{arguments['query']}' results={len(visible)}")


def _handle_get(arguments, store, perms):
    mid = arguments.get("memory_id")
    if not mid:
        raise ValueError("memory_id required")
    mem = store().get(mid)
    if not mem:
        return ("Memory not found.", f"id={mid} not_found")

    fp = mem.get("folder_path", "")
    if fp:
        ok, reason = perms.can_read_folder(fp)
        if not ok:
            raise PermissionError(reason)

    cat = mem.get("category", "")
    if cat and not perms.category_allowed(cat):
        raise PermissionError(f"Category '{cat}' is hidden.")

    # AI-facing projection: stored content may carry embedded TipTap HTML;
    # render it to plain text at this boundary so the AI's view is the meaning,
    # not the user's rich-text markup. Shallow-copy — the store cache is never
    # mutated. All other fields pass through verbatim.
    mem = {**mem, "content": _to_plaintext(mem.get("content"))}

    # Knowledge-graph context: surface how this memory connects to the rest of
    # the brain (shared-tag / code-dep neighbors) — the same relationships the
    # app's graph shows the user, which a flat read would otherwise drop. Only
    # attached when real edges exist (no fabricated relations), letting the AI
    # traverse the graph the way grep fundamentally cannot.
    related = _related_memories(store().conn, mid)
    if related:
        mem = {**mem, "related": related}

    # Citation footer — explicit authoritative-source marker for the AI
    body = json.dumps(mem, ensure_ascii=False, indent=2)
    footer = (
        f"\n\n**Source:** memory `{mid[:8]}…` "
        f"(folder={mem.get('folder_path') or '<root>'}, category={cat or '?'})  "
        f"\n**Verified:** direct read from authoritative store — quote freely."
    )
    return (body + footer, f"id={mid} cat={cat}")


def _handle_aggregate(arguments, agg, perms):
    cat = arguments.get("category")
    if not perms.category_allowed(cat):
        raise PermissionError(f"Category '{cat}' is hidden.")

    result = agg().compute(
        cat,
        arguments["operation"],
        arguments.get("field"),
        arguments.get("group_field"),
        arguments.get("filters"),
    )
    return (json.dumps(result, ensure_ascii=False, indent=2),
            f"agg cat={cat} op={arguments['operation']}")


def _handle_remember(arguments, store, perms):
    folder_path = arguments.get("folder")
    folder_id = None

    if folder_path:
        ok, reason = perms.can_write_folder(folder_path)
        if not ok:
            raise PermissionError(reason)
        # Resolve to a folder_id, creating any missing segments. Use the
        # shared path-walker rather than a manual get_by_path/create: the old
        # inline `ft.create(...)` returned the whole folder DICT
        # (FolderTree.create -> dict), which then got bound straight into the
        # memories_v2 INSERT and crashed with
        #   sqlite3.ProgrammingError: type 'dict' is not supported (parameter 6)
        # — parameter 6 of that INSERT is folder_id. _ensure_folder_path
        # always returns a scalar id (or None) AND handles nested paths like
        # '/A/B/C' with correct parent links instead of creating a detached
        # leaf folder. (Bug repro: brain_remember folder='/Decisions' when
        # /Decisions didn't exist yet → get_by_path miss → create() dict.)
        folder_id = _ensure_folder_path(store().conn, folder_path)

    cat = arguments.get("category", "note")
    if not perms.category_allowed(cat):
        raise PermissionError(f"Category '{cat}' is hidden.")

    mid = store().store(
        content=arguments["content"],
        category=cat,
        typed_data=arguments.get("typed_data"),
        tags=arguments.get("tags"),
        folder_id=folder_id,
        source="mcp",
    )
    return (f"✅ Saved memory: {mid}",
            f"saved id={mid} cat={cat}")


def _handle_sitemap(store, reg, perms):
    """Return everything an external AI needs to know about this brain's layout."""
    from core.setup.brain_manifest import get_manifest

    manifest = get_manifest()
    manifest.reload()

    # Build folder list with live counts + effective permissions
    folder_rows = store().conn.execute("""
        SELECT f.id, f.path, f.name, COUNT(m.id) as memory_count
        FROM folders f
        LEFT JOIN memories_v2 m ON m.folder_id = f.id
        GROUP BY f.id
        ORDER BY f.path
    """).fetchall()

    folders_info = []
    for fid, path, name, count in folder_rows:
        rule = manifest.folder_rule(path) or {}
        readable = manifest.is_readable(path)
        writable = manifest.is_writable(path)
        if perms:
            ok_read, _ = perms.can_read_folder(path)
            ok_write, _ = perms.can_write_folder(path)
            # If permission engine is stricter, defer to it
            readable = readable and ok_read
            writable = writable and ok_write
        folders_info.append({
            "path": path,
            "name": name,
            "memory_count": count,
            "purpose": rule.get("purpose", ""),
            "category_hint": rule.get("category_hint"),
            "auto_subfolder": rule.get("auto_subfolder"),
            "ai_readable": readable,
            "ai_writable": writable,
        })

    can = []
    cannot = []
    for f in folders_info:
        if f["ai_writable"]: can.append(f"write to {f['path']}")
        if not f["ai_readable"]: cannot.append(f"read {f['path']}")

    sitemap = {
        "manifest_version": manifest.data.get("version"),
        "owner": manifest.data.get("owner"),
        "language_pref": manifest.data.get("language_pref"),
        "folders": folders_info,
        "category_routing": manifest.data.get("category_routing", {}),
        "naming_conventions": manifest.data.get("naming_conventions", {}),
        "categories_available": [c["name"] for c in reg().list_categories()],
        "ai_hints": manifest.data.get("ai_hints", []),
        "you_can": can[:20],         # cap for prompt economy
        "you_cannot": cannot[:20],
    }
    return (json.dumps(sitemap, ensure_ascii=False, indent=2),
            f"sitemap folders={len(folders_info)}")


def _handle_status(store, reg, perms):
    stats = store().stats()
    status = {
        "tier": 0,
        "tier_name": "No-AI (BM25 + SQL)",
        "capabilities": [
            "bm25_search", "folder_hierarchy", "tag_system",
            "sql_aggregation", "schema_validation", "code_indexing"
        ],
        "stats": stats,
        "categories": [c["name"] for c in reg().list_categories()],
        "permissions_summary": {
            "tools": perms.config["tools"],
            "deny_categories": perms.config.get("deny_categories", []),
            "folder_rules": list(perms.config["folders"].keys()),
        },
    }
    return (json.dumps(status, ensure_ascii=False, indent=2),
            "status_query")

# ─────────────────────────────────────────────
# Code Indexer Handlers (Phase 5.2)
# ─────────────────────────────────────────────

def _handle_code_search(arguments, store):
    import os
    q = arguments["query"]
    limit = arguments.get("limit", 5)
    scope_path = (arguments.get("path") or "").strip()
    if scope_path:
        _, _, resolved_ppath, _ = _normalize_file_path(None, scope_path)
        if resolved_ppath:
            scope_path = resolved_ppath
        else:
            scope_path = os.path.realpath(os.path.expanduser(scope_path))

    # Route through _fts_safe_query so hyphenated, Thai, and punctuation-heavy
    # queries don't get interpreted as FTS5 NOT/column operators
    # (Issue #12: "ab-test cosmos measure effectiveness" → "no such column: test").
    fts_q = _fts_safe_query(q)

    conn = store().conn
    cursor = conn.cursor()

    # ── Indexing-readiness check (Friction #1, 2026-05-14) ──
    # When the user has just registered a project the index may be
    # populating asynchronously. If we return "no matches" the AI
    # interprets that as "tool doesn't have data" and falls back to
    # raw grep/ReadFile — destroying Cosmos's value-prop. Instead,
    # surface a structured "still indexing" signal so the AI knows
    # to retry rather than abandon the tool.
    try:
        cursor.execute("SELECT COUNT(*) FROM code_index")
        total_symbols = cursor.fetchone()[0]
    except Exception:
        total_symbols = 0
    if total_symbols == 0:
        return (
            "⏳ **Code index is empty or still indexing.**\n\n"
            "No symbols have been recorded yet. This usually means:\n"
            "  1. No project has been registered → call "
            "`code_reindex(path=<cwd>)` to register the current directory.\n"
            "  2. A scan is in progress → wait ~3 seconds and call "
            "this tool again.\n\n"
            "Do NOT fall back to raw grep / ReadFile — call "
            "`code_reindex()` with no args first to see watched "
            "projects, or pass `path=<cwd>` to register + index now.",
            f"code_q='{q}' fts='{fts_q}' results=0 reason=empty_index",
        )

    # Over-fetch when scope_path is set so the post-filter has enough
    # candidates to fill `limit` — without project_id on code_index, the
    # FTS results pool across every watched project sharing the same
    # relative-path namespace, and a naive LIMIT would mostly fill with
    # foreign-project rows in a multi-project setup.
    fetch_n = limit * 6 if scope_path else limit
    cursor.execute("""
        SELECT ci.symbol_name, ci.symbol_type, ci.file_path, ci.content, ci.docstring, code_fts.rank
        FROM code_fts
        JOIN code_index ci ON ci.id = code_fts.id
        WHERE code_fts MATCH ?
        ORDER BY code_fts.rank LIMIT ?
    """, (fts_q, fetch_n))

    raw_results = cursor.fetchall()
    if scope_path:
        results = []
        for r in raw_results:
            file_path = r[2]
            if os.path.isfile(os.path.join(scope_path, file_path)):
                results.append(r)
                if len(results) >= limit:
                    break
    else:
        results = raw_results
    if not results:
        # ── Better-than-empty response (Friction #2, 2026-05-14) ──
        # AI got nothing back and decided Cosmos was broken. Help it
        # decide what to try NEXT instead of abandoning the tool:
        # show the actual FTS query, suggest sibling tools that match
        # different access patterns, hint at common gotchas (short
        # tokens, exact-symbol lookup).
        tokens_used = [t.strip('"') for t in fts_q.split(' OR ') if t.startswith('"')]
        next_steps = [
            f"`code_find_file(name=\"{q}\")` — if `{q}` is a filename / path fragment",
            f"`code_get_symbol(symbol_name=\"{q}\")` — if `{q}` is a known function/class name",
            f"`find_relevant_code(symptom=\"...\", path=<cwd>)` — for natural-language symptoms",
        ]
        return (
            f"No code matches found for query `{q}`.\n\n"
            f"FTS query attempted: `{fts_q}`\n"
            f"Tokens after stop-word + Thai pre-tokenization: {tokens_used or '(none)'}\n\n"
            f"Try one of these instead:\n  - " + "\n  - ".join(next_steps) + "\n\n"
            "Do NOT fall back to raw grep — broader Cosmos tools cover "
            "every grep pattern with smaller token cost.",
            f"code_q='{q}' fts='{fts_q}' results=0 total_index={total_symbols}",
        )

    last_idx = _project_last_indexed(scope_path)
    lines = [_scope_header() + f"Code search results for '{q}':\n"]
    for r in results:
        flag = _dirty_flag(scope_path, r[2], last_idx)
        lines.append(f"[{r[1].upper()}] {r[0]} (in {r[2]}){flag}")
        lines.append(f"Signature: {r[3]}")
        if r[4]: lines.append(f"Docstring: {r[4]}")
        lines.append("---")

    return ("\n".join(lines), f"code_q='{q}' fts='{fts_q}' results={len(results)}")


def _scope_header() -> str:
    """Universal scope warning prepended to project-scoped tool responses.

    Until V1.1 ships `project_id` on `code_index`, Cosmos pools symbols from
    every watched project into one index with RELATIVE file paths. A search
    or skeleton call that returns `tests/test_foo.py` could be from any of
    the watched projects — there is no way to attribute it reliably at
    query time. Without this warning, an AI confidently labels the result
    as belonging to whichever project the user just named (the Gemini
    "fastapi master → AI-Bran's REST API" failure mode).

    Returns an empty string when only 0–1 projects are watched (no
    ambiguity possible).
    """
    try:
        from core.code_indexer.project_registry import get_project_registry
        projects = get_project_registry().list()
    except Exception:
        return ""
    if len(projects) < 2:
        return ""
    names = ", ".join(f"`{p.get('name','?')}`" for p in projects)
    return (
        f"📍 **Scope warning:** Cosmos's code index currently pools symbols "
        f"from {len(projects)} watched projects ({names}). File paths shown "
        f"below are RELATIVE — a path like `tests/test_x.py` may belong to "
        f"any of these. Before reporting a symbol as belonging to a specific "
        f"project, verify it appears under that project's expected directory "
        f"structure (e.g. via `code_find_file`).\n\n"
    )


def _fts_safe_query(symptom: str) -> str:
    """Build an FTS5-safe MATCH query from a free-text symptom.

    Strategy: pre-tokenize Thai segments via pythainlp (mirrors what indexer
    does on the content side), then split into keyword tokens, drop stop
    words, OR them together. Without Thai pre-tokenization, "ตัวอักษรไทย"
    is one token both in query and in content but neighbours like
    "ตัวอักษร" + "ไทย" stored separately wouldn't match.
    """
    import re
    from core.memory.store_v2 import pre_tokenize as _pretok
    normalized = _pretok(symptom)   # splits Thai phrases on word boundaries
    stop = {
        "the", "a", "an", "is", "are", "of", "to", "in", "on", "at", "and", "or", "but",
        "with", "i", "you", "we", "it", "this", "that", "ใน", "และ", "หรือ", "ของ",
        "ที่", "ก็", "ไม่", "เป็น", "มี", "จะ", "ให้",
    }
    tokens = re.findall(r"[A-Za-z0-9_]{2,}|[฀-๿]+", normalized)
    tokens = [t for t in tokens if t.lower() not in stop]
    if not tokens:
        return symptom.strip().replace('"', '""') or "*"
    quoted = [f'"{t}"' for t in tokens[:8]]
    return " OR ".join(quoted)


def _handle_find_relevant_code(arguments, store):
    """Hybrid router: free-text symptom → ranked candidates from code FTS +
    past errors. Designed to be the FIRST call when investigating a problem
    instead of jumping straight to grep / Read.
    """
    from core.code_indexer.errors import (
        get_code_errors,
        lesson_hygiene_nudge,
        resolve_project_id,
    )
    from core.code_indexer.project_registry import get_project_registry

    symptom = (arguments.get("symptom") or "").strip()
    if not symptom:
        return "❌ symptom is required", "missing_symptom"
    limit = int(arguments.get("limit") or 6)
    fts_query = _fts_safe_query(symptom)

    cursor = store().conn.cursor()

    # 1. Code FTS — file-level entries surface for plain-text matches; symbol
    # entries surface for identifier matches. Both relevant.
    # Scope to caller-supplied `path` so FTS hits from sibling watched
    # projects (relative-path namespace collision until project_id lands)
    # don't dilute the candidate list. Over-fetch by 6× to compensate for
    # rows the filter drops.
    scope_path = arguments.get("path") or ""
    if scope_path:
        _, _, resolved_ppath, _ = _normalize_file_path(None, scope_path)
        if resolved_ppath:
            scope_path = resolved_ppath
        else:
            scope_path = os.path.realpath(os.path.expanduser(scope_path.strip()))
    code_hits: list = []
    try:
        fetch_n = limit * 6 if scope_path else limit * 2
        cursor.execute("""
            SELECT ci.symbol_name, ci.symbol_type, ci.file_path, ci.start_line, code_fts.rank
            FROM code_fts
            JOIN code_index ci ON ci.id = code_fts.id
            WHERE code_fts MATCH ?
            ORDER BY code_fts.rank
            LIMIT ?
        """, (fts_query, fetch_n))
        raw = cursor.fetchall()
        if scope_path:
            for row in raw:
                if os.path.isfile(os.path.join(scope_path, row[2])):
                    code_hits.append(row)
                    if len(code_hits) >= limit * 2:
                        break
        else:
            code_hits = raw
    except Exception as e:
        return f"❌ search error: {e}", "fts_error"

    # 2. Past errors — score with the shared ranker so the same logic
    # powers both this MCP path and any future test runners that probe
    # find_relevant_code's behavior. Filters disabled lessons out at the
    # source (list_for_project default).
    error_rows: list = []
    path = arguments.get("path")
    proj_id = None
    try:
        proj_id = resolve_project_id(path) if path else None
        errors_svc = get_code_errors()
        all_errors = (
            errors_svc.list_for_project(proj_id, limit=200)
            if proj_id
            else _list_all_errors(cursor)
        )
        from core.code_indexer.errors import (
            score_lesson_for_query,
            _path_matches_globs,
        )
        import re
        sym_tokens = set(re.findall(r"[A-Za-z0-9_]{3,}|[฀-๿]{2,}", symptom.lower()))

        def _matched_tokens(e):
            # Recompute the exact haystack score_lesson_for_query tokenises
            # against, so the "matched:" annotation is faithful to WHY a
            # lesson scored — not an independent re-derivation that could
            # disagree with the ranker.
            haystack = " ".join([
                (e.get("symptom") or ""),
                (e.get("root_cause") or ""),
                (e.get("fix") or ""),
                " ".join(e.get("tags") or []),
            ]).lower()
            return [t for t in sym_tokens if t in haystack]

        def _structural(e):
            # "Always-remind" signals that justify surfacing a lesson even
            # with weak token overlap: user pinned it, or its scope_globs
            # match the current path.
            if e.get("pinned"):
                return True
            return bool(path and _path_matches_globs(path, e.get("scope_globs") or []))

        scored: list = []
        for e in all_errors:
            s = score_lesson_for_query(e, symptom_tokens=sym_tokens, current_path=path)
            scored.append((s, e))
        max_score = max((s for s, _ in scored), default=0.0)

        kept: list = []
        for s, e in scored:
            matched = _matched_tokens(e)
            # Raised floor. The old `s > 0` let lone substring/recency hits
            # leak in ("store" matching "storage", a recent same-dir lesson
            # with zero real overlap). Admit only when there's real signal:
            #   • ≥2 distinct token hits, OR
            #   • a structural signal (pin / scope-glob), OR
            #   • the lesson is in the top half of this query's score range.
            if len(matched) >= 2 or _structural(e) or (max_score > 0 and s >= max_score * 0.5):
                e = dict(e)
                e["_matched"] = matched
                kept.append((s, e))
        kept.sort(key=lambda x: -x[0])
        error_rows = [e for _, e in kept[:5]]
    except Exception:
        error_rows = []

    # 3. Project registry for friendly path display
    try:
        registry = get_project_registry()
        proj_map = {p["id"]: p for p in registry.list()}
    except Exception:
        proj_map = {}

    # 4. Format
    lines = [f"# Relevant code for: {symptom}\n"]

    if code_hits:
        lines.append("## 🎯 Code candidates")
        seen_files = set()
        shown = 0
        last_idx = _project_last_indexed(scope_path)
        for sym_name, sym_type, file_path, start_line, _rank in code_hits:
            # Dedupe to one entry per file (file-level + symbol-level often co-occur)
            if file_path in seen_files:
                continue
            seen_files.add(file_path)
            loc = f"{file_path}:{start_line}" if start_line and sym_type != "file" else file_path
            flag = _dirty_flag(scope_path, file_path, last_idx)
            lines.append(f"- `{loc}` — **{sym_name}** [{sym_type}]{flag}")
            shown += 1
            if shown >= limit:
                break
        lines.append("")
    else:
        lines.append("## 🎯 Code candidates\n_No code matches — try broader terms or check `code_search`._\n")

    if error_rows:
        lines.append("## 🧠 Past lessons (errors+fixes)")
        for e in error_rows:
            sev = {1: "🔴", 2: "🟡", 3: "🟢"}.get(e.get("severity", 2), "·")
            proj = proj_map.get(e.get("project_id", ""), {}).get("name", "?")
            tags = ", ".join(e.get("tags") or [])
            lines.append(
                f"- {sev} `{e['id'][:8]}` ({proj}) — {(e.get('symptom') or '')[:90]}"
            )
            matched = e.get("_matched") or []
            if matched:
                # Why this lesson surfaced — lets the reader judge relevance
                # instead of trusting an opaque rank.
                lines.append(f"  · matched: {', '.join(matched)}")
            else:
                # Admitted on a non-token signal (pin / scope-glob / top of a
                # weak score range). State it plainly — the thinnest-evidence
                # entry is exactly the one the reader most needs flagged.
                why = "pinned" if e.get("pinned") else "recency/path"
                lines.append(f"  · surfaced: {why} — no term overlap (low confidence)")
            if tags:
                lines.append(f"  tags: {tags}")
            if e.get("fix"):
                lines.append(f"  fix: {(e['fix'])[:140].strip()}")
        lines.append("")
    else:
        lines.append("## 🧠 Past lessons\n_No matching past errors. (Could be a fresh issue.)_\n")

    # Auto-memory nudge — only when this call resolved to a watched project
    # AND lessons have drifted since the most recent record (threshold check
    # lives inside the helper). Returns None when not warranted.
    if proj_id:
        proj_path = proj_map.get(proj_id, {}).get("path")
        nudge = lesson_hygiene_nudge(proj_path, proj_id)
        if nudge:
            lines.append(nudge)
            lines.append("")

    lines.append(
        "## ▶ Next step\n"
        "Read the top 1-2 code candidates first. If a past lesson has the same root cause, "
        "apply that fix — don't re-derive."
    )

    return "\n".join(lines), f"symptom='{symptom[:40]}' code={len(code_hits)} errors={len(error_rows)}"


def _list_all_errors(cursor):
    """All errors across all projects, ordered by recency. Used when caller
    didn't supply a `path` to scope to one project."""
    cursor.execute("""
        SELECT id, project_id, symptom, root_cause, fix, files_affected, tags,
               severity, last_seen_at
        FROM code_errors
        ORDER BY last_seen_at DESC
        LIMIT 200
    """)
    import json as _json
    cols = ["id", "project_id", "symptom", "root_cause", "fix", "files_affected",
            "tags", "severity", "last_seen_at"]
    out = []
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        try:
            d["files_affected"] = _json.loads(d.get("files_affected") or "[]")
        except Exception:
            d["files_affected"] = []
        try:
            d["tags"] = _json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        out.append(d)
    return out


def _handle_code_find_function(arguments, store):
    name = arguments["symbol_name"]
    cursor = store().conn.cursor()
    
    # 1. Get definitions
    cursor.execute("""
        SELECT id, file_path, symbol_type, scope, body, language, start_line, end_line 
        FROM code_index 
        WHERE symbol_name = ?
    """, (name,))
    
    defs = cursor.fetchall()
    if not defs:
        return (f"Function/Class '{name}' not found.", f"code_func='{name}' not_found")
        
    lines = [f"Definitions for '{name}':\n"]
    for d in defs:
        did = d[0]
        lines.append(f"File: {d[1]} | Type: {d[2]} | Scope: {d[3]} | Lines {d[6]}-{d[7]}")
        lines.append("```" + d[5])
        lines.append(d[4])
        lines.append("```\n")
        
        # 2. Get calls made by this function
        cursor.execute("""
            SELECT c.symbol_name, c.file_path 
            FROM code_links l
            JOIN code_index c ON l.target_id = c.id
            WHERE l.source_id = ? AND l.link_type = 'call'
        """, (did,))
        calls = cursor.fetchall()
        if calls:
            lines.append("Calls made to:")
            for c in set(calls): # deduplicate
                lines.append(f"  - {c[0]} (in {c[1]})")
        lines.append("\n" + "="*40 + "\n")

    # P0 #3 — lesson auto-injection on function-level lookups too.
    first_file = defs[0][1] if defs else None
    lesson = _fetch_lesson_section(symptom=name, path=first_file, store=store, limit=2)
    if lesson:
        lines.append(lesson)

    return ("\n".join(lines), f"code_func='{name}' defs={len(defs)}")

def _handle_code_find_callers(arguments, store):
    """B-layer decorated: returns confidence tier + edge_kind +
    boundary_crossing per edge so LLM clients following the
    cosmos-connector skill A-layer can pick the right verification
    strategy. See core/code_indexer/b_layer.py for the schema.

    0.2.16 fix Issue #4: accept optional `scope_path` to filter target
    symbol matches by file_path prefix — disambiguates same-named
    symbols (e.g. `authenticate_user` across multiple tutorials).
    """
    from core.code_indexer.b_layer import (
        decorate_edges, render_edges_markdown, index_metadata,
    )
    name = arguments["symbol_name"]
    # Accept either `path` (canonical, matches every other tool) or
    # legacy `scope_path` for clients that already passed it.
    scope_path = (arguments.get("path") or arguments.get("scope_path") or "").strip()
    _, resolved_pid, resolved_ppath, resolved_rel_scope = _normalize_file_path(None, scope_path or None)
    if resolved_ppath:
        scope_path = resolved_ppath
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))
    conn = store().conn
    cursor = conn.cursor()

    # 1. Get targets, filtered by scope_path if provided. Switched from
    # `file_path LIKE scope_path%` to `os.path.isfile(scope_path/file_path)`
    # because code_index stores RELATIVE paths in the multi-project case
    # (no project_id column), so the prefix LIKE never matched.
    cursor.execute(
        "SELECT id, file_path FROM code_index WHERE symbol_name = ?", (name,)
    )
    all_targets = cursor.fetchall()
    if scope_path:
        target_ids = [
            tid for tid, fp in all_targets
            if os.path.isfile(os.path.join(scope_path, fp))
        ]
    else:
        target_ids = [tid for tid, _ in all_targets]

    # 0.2.17 Issue #6 — if multiple targets resolve under scope_path or
    # at all, surface the warning so the LLM knows results may be a
    # mix of unrelated same-named functions. Cosmos's link table
    # currently cross-pollinates across files (logged as code_errors
    # ee8ab30a), so the warning is the only signal the user gets.
    multi_def_warning = ""
    if not scope_path:
        cursor.execute(
            "SELECT COUNT(DISTINCT file_path) FROM code_index WHERE symbol_name = ?",
            (name,),
        )
        nfiles = cursor.fetchone()[0]
        if nfiles > 1:
            multi_def_warning = (
                f"⚠ `{name}` is defined in {nfiles} files. Results may "
                "include callers of unrelated same-named functions. Pass "
                "`scope_path` to disambiguate.\n\n"
            )

    if not target_ids:
        return (
            render_edges_markdown(
                [], title="Callers", target_name=name,
                index_metadata=index_metadata(conn),
            ),
            f"code_callers='{name}' not_found",
        )

    # 2. Get callers WITH link_type (B-layer needs it to classify)
    raw_callers = []
    caller_bodies = {}
    for tid in target_ids:
        cursor.execute("""
            SELECT c.symbol_name, c.file_path, c.start_line, l.link_type, c.body
            FROM code_links l
            JOIN code_index c ON l.source_id = c.id
            WHERE l.target_id = ?
        """, (tid,))
        for row in cursor.fetchall():
            # 0.2.17 Issue #6 — also apply scope filter to result rows,
            # not just target lookup. Without this, cross-file links in
            # code_index (indexer bug ee8ab30a) leak into the answer.
            if scope_path:
                if resolved_rel_scope is not None:
                    if resolved_rel_scope and not (row[1] or "").startswith(resolved_rel_scope):
                        continue
                else:
                    abs_row = os.path.join(scope_path, row[1] or "")
                    if not abs_row.startswith(scope_path):
                        continue
            raw_callers.append((row[0], row[1], row[2], row[3]))
            if row[4]:
                caller_bodies[row[0]] = row[4]

    # Dedupe by (caller, file, line, link_type)
    seen = set()
    unique = []
    for r in sorted(raw_callers, key=lambda x: (x[1], x[2])):
        key = (r[0], r[1], r[2], r[3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    edges = decorate_edges(unique, has_link_type=True, bodies=caller_bodies)
    md_body = render_edges_markdown(
        edges, title="Callers", target_name=name,
        index_metadata=index_metadata(conn),
    )
    return (multi_def_warning + md_body,
            f"code_callers='{name}' results={len(edges)} scope={scope_path or 'all'}")

def _handle_code_explain_project(arguments, store):
    """Render a Markdown project overview.

    Tries the cached overview written by the indexer first
    (code_index row with symbol_type='overview'), and falls back to
    a live ProjectAnalyzer pass when the cache is missing or stale.

    Schema-defensive: any SQL error (e.g. an old DB without code_index
    or a future column rename) returns a readable message instead of
    crashing the MCP request — caller can still re-index to recover.

    `arguments.path` (optional) scopes the `modules` list to top-level
    dirs that actually exist under that path. Without it, code_index's
    pooled relative-path namespace surfaces dirs from sibling projects
    (e.g. `cli/` or `webview-ui/` from another repo showing up in
    AI-Bran's overview). Falls back to cwd when arguments is missing.
    """
    import json as _json
    import os
    from core.code_indexer.project_analyzer import ProjectAnalyzer

    arguments = arguments or {}
    scope_path = (arguments.get("path") or os.getcwd()).strip()
    if scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))

    s = store()
    overview = None

    # ── Cache hit? — indexer.py writes the json'd overview into
    # code_index.content with symbol_type='overview'. Older code here
    # filtered on a non-existent `category` column (no such column:
    # category) which crashed the entire tool. Now schema-tolerant.
    try:
        cursor = s.conn.cursor()
        cursor.execute(
            "SELECT content FROM code_index "
            "WHERE symbol_type = 'overview' "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row and row[0]:
            try:
                overview = _json.loads(row[0])
            except Exception:
                overview = None  # malformed cache → fall through to live
    except Exception:
        # Schema drift / table missing — fall through to live analyzer.
        overview = None

    # ── Live fallback. Pass the sqlite Connection, not the MemoryStore
    # wrapper (the wrapper has no .cursor()). Earlier code passed
    # `store()` which crashed inside ProjectAnalyzer when no cache row
    # existed.
    if not overview:
        try:
            analyzer = ProjectAnalyzer(os.getcwd())
            overview = analyzer.analyze(s.conn) or {}
        except Exception as e:
            return (
                f"# Project Overview\n\n"
                f"_Couldn't analyze project: {type(e).__name__}: {e}_\n\n"
                f"Try re-indexing the repo and call this tool again.",
                "code_explain_project",
            )

    lines = [_scope_header() + "# Project Overview\n"]
    if overview.get("frameworks"):
        lines.append(f"**Frameworks/Languages**: {', '.join(overview['frameworks'])}")
    if overview.get("entry_points"):
        lines.append("\n**Likely Entry Points**:")
        for ep in overview["entry_points"]:
            lines.append(f"  - {ep}")
    if overview.get("stats"):
        lines.append("\n**Codebase Stats**:")
        for k, v in overview["stats"].items():
            lines.append(f"  - {k}: {v}")
    if overview.get("modules"):
        # Filter modules to those that exist on disk under scope_path.
        # code_index has no project_id column, so modules from sibling
        # watched projects share the relative-path namespace and would
        # otherwise leak in. The on-disk check is the workaround until
        # a `project_id` column lands; rows the filter drops still live
        # in the DB but they belong to a different project.
        filtered = []
        dropped = []
        for mod, count in overview["modules"].items():
            if mod in (".", "/", "root", ""):
                filtered.append((mod, count))
                continue
            if scope_path and os.path.isdir(os.path.join(scope_path, mod)):
                filtered.append((mod, count))
            else:
                dropped.append(mod)
        filtered.sort(key=lambda x: x[1], reverse=True)
        lines.append("\n**Top-level Modules**:")
        for mod, count in filtered:
            lines.append(f"  - {mod}/ ({count} symbols)")
        if dropped:
            lines.append(
                f"\n_Filtered out {len(dropped)} module(s) that don't exist "
                f"under `{scope_path}` — likely from a sibling watched project: "
                f"{', '.join(sorted(dropped)[:8])}{'…' if len(dropped) > 8 else ''}._"
            )

    return ("\n".join(lines), "code_explain_project")


def _normalize_file_path(file_path: str | None, scope_path: str | None = None) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Normalize file_path (which may be absolute) to a project-relative path.
    Also resolves scope_path to the registered project root path if it is inside a watched project.
    Returns: (resolved_relative_file_path, resolved_project_id, resolved_project_path, resolved_relative_scope_path)
    """
    import os
    from core.code_indexer.project_registry import get_project_registry
    from core.code_indexer.errors import resolve_project_id

    resolved_pid = None
    resolved_ppath = None
    resolved_rel_scope = None

    # First, let's normalize scope_path if provided
    if scope_path:
        scope_path = scope_path.strip()
        if scope_path:
            abs_scope = os.path.realpath(os.path.expanduser(scope_path))
            pid = resolve_project_id(abs_scope)
            if pid:
                registry = get_project_registry()
                proj = registry.get(pid)
                if proj and proj.get("path"):
                    resolved_pid = pid
                    resolved_ppath = os.path.realpath(proj["path"])
                    # Calculate scope_path relative to project root
                    rel_scope = os.path.relpath(abs_scope, resolved_ppath)
                    if rel_scope == ".":
                        resolved_rel_scope = ""
                    else:
                        resolved_rel_scope = rel_scope

    # If file_path is provided, normalize it
    if file_path:
        file_path = file_path.strip()
        if file_path:
            if os.path.isabs(file_path) or file_path.startswith("~/"):
                abs_file = os.path.realpath(os.path.expanduser(file_path))
                # Resolve project from file_path directly
                pid_from_file = resolve_project_id(abs_file)
                if pid_from_file:
                    registry = get_project_registry()
                    proj = registry.get(pid_from_file)
                    if proj and proj.get("path"):
                        resolved_pid = pid_from_file
                        resolved_ppath = os.path.realpath(proj["path"])
                        rel_path = os.path.relpath(abs_file, resolved_ppath)
                        return rel_path, resolved_pid, resolved_ppath, resolved_rel_scope
                # If absolute but not in a watched project, return as is
                return abs_file, None, None, resolved_rel_scope
            else:
                # file_path is relative.
                # If we have resolved_ppath, we can make sure it's relative to resolved_ppath
                if resolved_ppath and scope_path:
                    abs_scope = os.path.realpath(os.path.expanduser(scope_path))
                    abs_file = os.path.realpath(os.path.join(abs_scope, file_path))
                    if abs_file.startswith(resolved_ppath + os.sep) or abs_file == resolved_ppath:
                        rel_path = os.path.relpath(abs_file, resolved_ppath)
                        return rel_path, resolved_pid, resolved_ppath, resolved_rel_scope
                return file_path, resolved_pid, resolved_ppath, resolved_rel_scope

    return None, resolved_pid, resolved_ppath, resolved_rel_scope


def _extract_signature_meta(content: str, language: str) -> dict:
    """Pull parameter types + return type from a signature line.

    Supports python / typescript / javascript / rust. Best-effort regex —
    returns {} when the signature shape isn't recognized."""
    import re
    if not content:
        return {}
    head = content.split("\n", 1)[0].strip()
    # Grab the first balanced (...) group
    depth = 0
    start = end = -1
    for i, ch in enumerate(head):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if start < 0 or end < 0:
        return {}
    params_blob = head[start + 1:end]
    tail = head[end + 1:].strip()

    # Split params on commas at depth 0 (so `Dict[str, int]` stays one param)
    parts = []
    buf = ""
    d = 0
    for ch in params_blob:
        if ch in "([{<":
            d += 1
        elif ch in ")]}>":
            d -= 1
        if ch == "," and d == 0:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())

    params = []
    for p in parts:
        if p in ("self", "cls", "&self", "&mut self"):
            continue
        # `name: Type = default`  or  `name: Type`  or  `name`
        m = re.match(r"^([A-Za-z_*&][A-Za-z0-9_]*)\s*:\s*(.+?)(\s*=\s*.+)?$", p)
        if m:
            params.append({"name": m.group(1).lstrip("*&"), "type": m.group(2).strip()})
        else:
            m2 = re.match(r"^([A-Za-z_*&][A-Za-z0-9_]*)", p)
            if m2:
                params.append({"name": m2.group(1).lstrip("*&"), "type": "any"})

    return_type = None
    if language == "python" and tail.startswith("->"):
        rt = tail[2:].strip().rstrip(":").strip()
        if rt:
            return_type = rt
    elif language in ("typescript", "javascript") and tail.startswith(":"):
        rt = tail[1:].strip().rstrip("{").strip().rstrip(";").strip()
        if rt:
            return_type = rt
    elif language == "rust" and tail.startswith("->"):
        rt = tail[2:].split("{", 1)[0].split("where", 1)[0].strip()
        if rt:
            return_type = rt

    out = {}
    if params:
        out["params"] = params
    if return_type:
        out["return_type"] = return_type
    return out


def _handle_code_get_symbol(arguments, store):
    name = arguments.get("symbol_name", "").strip()
    file_path = arguments.get("file_path")
    scope_path = (arguments.get("path") or "").strip()
    norm_file, resolved_pid, resolved_ppath, _ = _normalize_file_path(file_path or None, scope_path or None)
    if norm_file:
        file_path = norm_file
    if resolved_ppath:
        scope_path = resolved_ppath
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))
    # mode='header' suppresses the body block — useful for "where is X"
    # questions where the caller only needs the signature + location.
    # The 2026-05-05 benchmark showed default-full mode bleeding 30x more
    # tokens than grep on point queries; header mode closes that gap.
    mode = (arguments.get("mode") or "full").strip().lower()
    if mode not in ("header", "full"):
        mode = "full"
    if not name:
        raise ValueError("symbol_name required")

    cursor = store().conn.cursor()
    if file_path:
        cursor.execute("""
            SELECT id, symbol_name, symbol_type, scope, content, body, docstring,
                   language, file_path, start_line, end_line
            FROM code_index
            WHERE symbol_name = ? AND file_path = ?
            LIMIT 1
        """, (name, file_path))
        row = cursor.fetchone()
    else:
        # Without scope_path the relative-path namespace pools matches
        # across every watched project — same-named symbols (e.g. `User`)
        # collide and ORDER BY length(body) DESC arbitrarily picks
        # whichever project happens to have the bigger body. Over-fetch
        # candidates and keep the first one whose file_path resolves on
        # disk under scope_path.
        limit = 20 if scope_path else 1
        cursor.execute("""
            SELECT id, symbol_name, symbol_type, scope, content, body, docstring,
                   language, file_path, start_line, end_line
            FROM code_index
            WHERE symbol_name = ?
            ORDER BY length(body) DESC
            LIMIT ?
        """, (name, limit))
        candidates = cursor.fetchall()
        row = None
        for c in candidates:
            if not scope_path or os.path.isfile(os.path.join(scope_path, c[8])):
                row = c
                break
    if not row:
        return (f"Symbol '{name}' not found.", f"code_get_symbol='{name}' not_found")

    cols = ["id", "symbol_name", "symbol_type", "scope", "content", "body",
            "docstring", "language", "file_path", "start_line", "end_line"]
    sym = dict(zip(cols, row))

    # Caller count — single roundtrip, gives AI impact estimate
    cursor.execute(
        "SELECT COUNT(*) FROM code_links WHERE target_id = ?", (sym["id"],)
    )
    caller_count = cursor.fetchone()[0]

    meta = _extract_signature_meta(sym.get("content") or "", sym["language"])

    header = _scope_header()
    flag = _dirty_flag(scope_path, sym['file_path'], _project_last_indexed(scope_path))
    lines = [
        (header + f"# {sym['symbol_name']}") if header else f"# {sym['symbol_name']}",
        f"**Type:** {sym['symbol_type']} | **Language:** {sym['language']}",
        f"**Location:** {sym['file_path']}:{sym['start_line']}-{sym['end_line']}{flag}",
        f"**Callers:** {caller_count}",
    ]
    if sym["scope"]:
        lines.append(f"**Scope:** {sym['scope']}")
    if meta.get("params"):
        lines.append("\n**Parameters:**")
        for p in meta["params"]:
            lines.append(f"- `{p['name']}`: `{p['type']}`")
    if meta.get("return_type"):
        lines.append(f"\n**Returns:** `{meta['return_type']}`")
    if sym["content"]:
        lines.append(f"\n**Signature:**\n```{sym['language']}\n{sym['content']}\n```")
    if sym["docstring"]:
        lines.append(f"\n**Docstring:** {sym['docstring']}")
    if mode == "full" and sym["body"]:
        lines.append(f"\n**Body:**\n```{sym['language']}\n{sym['body']}\n```")
    elif mode == "header" and sym["body"]:
        lines.append(
            f"\n_Body omitted (mode='header'). Pass mode='full' to read it._"
        )

    # P0 #3 — lesson auto-injection. Symbol lookups are precursors to
    # edits more often than not; surface any matching lesson inline so
    # the AI doesn't have to know to call find_relevant_code first.
    lesson = _fetch_lesson_section(
        symptom=name, path=sym.get("file_path"), store=store, limit=2,
    )
    if lesson:
        lines.append("\n" + lesson)

    return ("\n".join(lines), f"code_get_symbol='{name}' mode={mode} callers={caller_count}")


def _handle_code_callees(arguments, store):
    """B-layer decorated: same schema as code_callers — confidence,
    edge_kind, boundary_crossing per callee.

    0.2.16 fix Issue #4: `scope_path` to disambiguate same-name symbols.
    """
    from core.code_indexer.b_layer import (
        decorate_edges, render_edges_markdown, index_metadata,
    )
    name = arguments.get("symbol_name", "").strip()
    scope_path = (arguments.get("path") or arguments.get("scope_path") or "").strip()
    _, resolved_pid, resolved_ppath, resolved_rel_scope = _normalize_file_path(None, scope_path or None)
    if resolved_ppath:
        scope_path = resolved_ppath
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))
    if not name:
        raise ValueError("symbol_name required")

    conn = store().conn
    cursor = conn.cursor()
    # Switched from `file_path LIKE scope_path%` to per-row on-disk
    # check because code_index file_path is RELATIVE in multi-project
    # setups (no project_id column).
    cursor.execute(
        "SELECT id, file_path FROM code_index WHERE symbol_name = ?", (name,)
    )
    sources = cursor.fetchall()
    if scope_path:
        sources = [s for s in sources if os.path.isfile(os.path.join(scope_path, s[1]))]
        if resolved_rel_scope:
            sources = [s for s in sources if (s[1] or "").startswith(resolved_rel_scope)]
    if not sources:
        return (
            render_edges_markdown(
                [], title="Callees", target_name=name,
                index_metadata=index_metadata(conn),
            ),
            f"code_callees='{name}' not_found",
        )
    source_id = sources[0][0]

    cursor.execute("""
        SELECT c.symbol_name, c.file_path, c.start_line, l.link_type, c.body
        FROM code_links l
        JOIN code_index c ON l.target_id = c.id
        WHERE l.source_id = ?
        ORDER BY c.file_path, c.start_line
    """, (source_id,))
    raw = cursor.fetchall()
    # Drop callees whose file isn't under scope_path.
    if scope_path:
        raw = [r for r in raw if os.path.isfile(os.path.join(scope_path, r[1] or ""))]
        if resolved_rel_scope:
            raw = [r for r in raw if (r[1] or "").startswith(resolved_rel_scope)]
    raw_callees = [(r[0], r[1], r[2], r[3]) for r in raw]
    bodies = {r[0]: r[4] for r in raw if r[4]}

    edges = decorate_edges(raw_callees, has_link_type=True, bodies=bodies)
    md = render_edges_markdown(
        edges, title="Callees", target_name=name,
        index_metadata=index_metadata(conn),
    )
    return (md, f"code_callees='{name}' results={len(edges)} scope={scope_path or 'all'}")


def _handle_code_uses(arguments, store):
    """Find every place an identifier appears (file:line). Uses FTS5 + body scan."""
    ident = arguments.get("identifier", "").strip()
    scope_path = (arguments.get("path") or "").strip()
    _, resolved_pid, resolved_ppath, resolved_rel_scope = _normalize_file_path(None, scope_path or None)
    if resolved_ppath:
        scope_path = resolved_ppath
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))
    if not ident:
        raise ValueError("identifier required")

    cursor = store().conn.cursor()
    # Over-fetch when scope_path is set so the per-row filter has enough
    # surviving rows after dropping sibling-project hits.
    fetch_limit = 400 if scope_path else 100
    cursor.execute("""
        SELECT symbol_name, symbol_type, file_path, start_line, body
        FROM code_index
        WHERE body LIKE ?
        ORDER BY file_path, start_line
        LIMIT ?
    """, (f"%{ident}%", fetch_limit))
    rows = cursor.fetchall()
    if scope_path:
        rows = [r for r in rows if os.path.isfile(os.path.join(scope_path, r[2] or ""))]
        if resolved_rel_scope:
            rows = [r for r in rows if (r[2] or "").startswith(resolved_rel_scope)]
        rows = rows[:100]

    if not rows:
        return (f"Identifier '{ident}' not found in any indexed body.",
                f"code_uses='{ident}' results=0")

    # Compute line offsets within each body
    usages = []
    for sym_name, sym_type, file_path, start_line, body in rows:
        if not body:
            continue
        for i, line in enumerate(body.splitlines()):
            if ident in line:
                usages.append((file_path, (start_line or 1) + i, sym_name, line.strip()[:80]))
                if len(usages) >= 200:
                    break
        if len(usages) >= 200:
            break

    if not usages:
        return (f"Identifier '{ident}' matched bodies but no exact lines.",
                f"code_uses='{ident}' results=0")

    # Group by file for readability
    by_file = {}
    for fp, ln, sn, snippet in usages:
        by_file.setdefault(fp, []).append((ln, sn, snippet))

    # Graceful defer for hot identifiers. For pervasive names (content, store,
    # update — hundreds of refs) a flat list is unscannable AND loses to grep.
    # Rather than pretend, state the limit: surface WHERE refs concentrate (the
    # one thing grep doesn't hand you cheaply) and defer enumeration to grep.
    # Third instance of the "Cosmos states its own limits" annotation slot.
    HOT_FILES, HOT_USES = 15, 80
    truncated = len(usages) >= 200          # hit the scan cap → certainly hot
    if truncated or len(by_file) > HOT_FILES or len(usages) >= HOT_USES:
        from collections import Counter
        area = Counter()
        for fp, hits in by_file.items():
            area[fp.split("/")[0] if "/" in fp else "."] += len(hits)
        exts = {"." + fp.rsplit(".", 1)[1] for fp in by_file if "." in fp}
        inc = f" --include='*{next(iter(exts))}'" if len(exts) == 1 else ""
        total = f"{len(usages)}{'+' if truncated else ''}"
        lines = [
            f"# '{ident}' — hot identifier: {total} occurrence(s) in "
            f"{len(by_file)} file(s). Flat list suppressed (not scannable).\n",
            "## Concentrated in",
        ]
        for a, c in area.most_common(12):
            lines.append(f"- `{a}/` — {c}")
        lines.append("\n## Densest files")
        for fp, hits in sorted(by_file.items(), key=lambda kv: -len(kv[1]))[:5]:
            lines.append(f"- `{fp}` — {len(hits)}")
        lines.append(
            f"\n## ▶ Exhaustive enumeration — grep wins here\n"
            f"`grep -rn{inc} '{ident}' {scope_path or '.'}`\n"
            f"(code_uses is tuned for sparse identifiers, not pervasive ones)"
        )
        return ("\n".join(lines),
                f"code_uses='{ident}' hot=1 files={len(by_file)} hits={total}")

    lines = [f"# '{ident}' used in {len(by_file)} file(s), {len(usages)} occurrence(s):\n"]
    for fp, hits in list(by_file.items())[:20]:
        lines.append(f"\n**{fp}**")
        for ln, sn, snippet in hits[:10]:
            lines.append(f"  - L{ln} (in {sn}): {snippet}")
        if len(hits) > 10:
            lines.append(f"  ... +{len(hits) - 10} more")

    return ("\n".join(lines), f"code_uses='{ident}' files={len(by_file)} hits={len(usages)}")


def _handle_code_hierarchy(arguments, store):
    """
    Drill-in browser: pass empty path → top-level folders;
    pass folder path → subfolders + files;
    pass file path → symbols in file.
    """
    path = (arguments.get("path") or "").strip()
    if path:
        norm_path, _, _, _ = _normalize_file_path(path)
        if norm_path:
            path = norm_path
    path = path.strip("/")
    cursor = store().conn.cursor()

    # Detect: is `path` a file (has extension) or folder/empty?
    is_file = "." in os.path.basename(path) and path

    if is_file:
        # File level — list symbols
        cursor.execute("""
            SELECT symbol_name, symbol_type, scope, start_line, end_line
            FROM code_index
            WHERE file_path = ?
            ORDER BY start_line
        """, (path,))
        rows = cursor.fetchall()
        if not rows:
            return (f"No symbols indexed for file '{path}'.",
                    f"code_hierarchy file={path} results=0")
        lines = [_scope_header() + f"# {path} — {len(rows)} symbol(s):\n"]
        for sn, st, scope, sl, el in rows:
            scope_str = f" (in {scope})" if scope else ""
            lines.append(f"  - L{sl}-{el}: {st} `{sn}`{scope_str}")
        return ("\n".join(lines), f"code_hierarchy file={path} symbols={len(rows)}")

    # Folder level — list subfolders + files containing symbols
    if path:
        prefix = path + "/"
        cursor.execute("""
            SELECT DISTINCT file_path FROM code_index
            WHERE file_path LIKE ?
        """, (prefix + "%",))
    else:
        cursor.execute("SELECT DISTINCT file_path FROM code_index")

    file_paths = [r[0] for r in cursor.fetchall()]
    if not file_paths:
        return (f"No code indexed at '{path or '<root>'}'.",
                f"code_hierarchy path={path} results=0")

    # Group by next path segment
    children: dict[str, dict] = {}  # name → {is_file, full_path, count}
    for fp in file_paths:
        rel = fp[len(prefix):] if path else fp
        first_seg = rel.split("/", 1)[0]
        if "/" in rel:
            # It's inside a subfolder
            sub_path = (path + "/" + first_seg) if path else first_seg
            entry = children.setdefault(first_seg, {"is_file": False, "full_path": sub_path, "count": 0})
            entry["count"] += 1
        else:
            children[first_seg] = {"is_file": True, "full_path": fp, "count": 0}

    # Count symbols per file
    for name, info in children.items():
        if info["is_file"]:
            cursor.execute("SELECT COUNT(*) FROM code_index WHERE file_path = ?", (info["full_path"],))
            info["count"] = cursor.fetchone()[0]

    title = f"# {path or '<project root>'} — {len(children)} entries:\n"
    lines = [_scope_header() + title]
    folders = sorted([(n, i) for n, i in children.items() if not i["is_file"]])
    files = sorted([(n, i) for n, i in children.items() if i["is_file"]])
    for n, i in folders:
        lines.append(f"  📁 {n}/  ({i['count']} files indexed)")
    for n, i in files:
        lines.append(f"  📄 {n}  ({i['count']} symbols)")

    return ("\n".join(lines),
            f"code_hierarchy path='{path or '<root>'}' entries={len(children)}")


def _handle_code_explain(arguments, store):
    """LLM-powered natural-language explanation. Tier 2 or Cloud only."""
    name = arguments.get("symbol_name", "").strip()
    if not name:
        raise ValueError("symbol_name required")

    # Fetch symbol body first (no LLM needed)
    body_text, _ = _handle_code_get_symbol({"symbol_name": name}, store)
    if "not found" in body_text.lower():
        return (body_text, f"code_explain='{name}' not_found")

    # Check tier
    try:
        from core.ai.tier_manager import get_tier_manager
        tm = get_tier_manager()
        if not tm.has_any_llm:
            return (
                f"⚠️ `code_explain` requires Tier 2 (local LLM) or Cloud.\n"
                f"Current tier: **{tm.active_tier}** — semantic explanation unavailable.\n\n"
                f"Here is the raw symbol instead:\n\n{body_text}",
                f"code_explain='{name}' tier_unavailable",
            )

        llm = tm.get_llm()
        prompt = (
            f"Explain the following code symbol clearly and concisely. "
            f"Focus on what it does, why it exists, and any notable design choices.\n\n"
            f"{body_text}\n\n"
            f"Explanation:"
        )
        if tm.active_tier == "tier2":
            resp = llm.chat(prompt, system_prompt="You are a senior software engineer.")
            explanation = resp.get("response", "") if isinstance(resp, dict) else str(resp)
        else:  # cloud
            explanation = llm.complete(prompt, system_prompt="You are a senior software engineer.", max_tokens=400)

        out = (
            f"# AI Explanation of `{name}`\n\n{explanation.strip()}\n\n"
            f"---\n\n{body_text}"
        )
        return (out, f"code_explain='{name}' tier={tm.active_tier}")
    except Exception as e:
        return (
            f"⚠️ Explanation failed: {e}\n\n{body_text}",
            f"code_explain='{name}' error",
        )


# ─────────────────────────────────────────────
# Stdio runner — used by Claude Desktop / Cursor
# ─────────────────────────────────────────────

def _extract_jsx_outline(body: str, max_depth: int = 3, max_lines: int = 40) -> str | None:
    """Walk tree-sitter TSX AST to extract a top-level JSX element tree.
    Returns None if the parser is unavailable, body has no JSX, or output is empty.
    Caller is responsible for guarding to .tsx/.jsx files."""
    try:
        import tree_sitter_typescript as tsts
        from tree_sitter import Language, Parser
        lang = Language(tsts.language_tsx())
        parser = Parser(lang)
        body_bytes = body.encode("utf-8")
        tree = parser.parse(body_bytes)
    except Exception:
        return None

    def tag_name(node) -> str:
        target = node
        if node.type == "jsx_element":
            for c in node.children:
                if c.type == "jsx_opening_element":
                    target = c
                    break
        # In jsx_opening_element / jsx_self_closing_element, the tag name is
        # the first identifier-like child (NOT property_identifier — that's an
        # attribute name).
        for c in target.children:
            if c.type in ("identifier", "jsx_identifier",
                          "nested_identifier", "member_expression"):
                return body_bytes[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return "?"

    out: list[str] = []

    def walk(node, depth: int):
        if len(out) >= max_lines:
            return
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            if depth > max_depth:
                return
            out.append(f"{'  ' * depth}- <{tag_name(node)}>")
            for c in node.children:
                walk(c, depth + 1)
        elif node.type == "jsx_fragment":
            if depth > max_depth:
                return
            out.append(f"{'  ' * depth}- <>")
            for c in node.children:
                walk(c, depth + 1)
        else:
            for c in node.children:
                walk(c, depth)

    walk(tree.root_node, 0)
    if not out:
        return None
    if len(out) >= max_lines:
        out.append(f"  _… (truncated; showing first {max_lines} elements)_")
    return "\n".join(out)


def _handle_code_skeleton(arguments, store):
    """
    Return SIGNATURES ONLY — no bodies. 95% token reduction vs reading files.
    Groups methods under their parent class. For .tsx/.jsx files (when caller
    requests a single file via `file_path`), also appends a JSX element tree.
    """
    file_path = (arguments.get("file_path") or "").strip()
    max_symbols = int(arguments.get("max_symbols") or 200)
    scope_path = (arguments.get("path") or "").strip()
    norm_file, resolved_pid, resolved_ppath, _ = _normalize_file_path(file_path or None, scope_path or None)
    if norm_file:
        file_path = norm_file
    if resolved_ppath:
        scope_path = resolved_ppath
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))

    cur = store().conn.cursor()
    if file_path:
        cur.execute("""
            SELECT file_path, symbol_name, symbol_type, scope, content,
                   docstring, language, start_line, end_line
            FROM code_index
            WHERE file_path = ? AND symbol_type != 'overview'
            ORDER BY start_line
            LIMIT ?
        """, (file_path, max_symbols))
        scope_label = file_path
    else:
        # Over-fetch when scope_path is set so the per-row filter has
        # enough candidates after dropping rows that belong to a sibling
        # watched project (relative-path namespace collision; same root
        # cause as in code_search / warm_tier).
        fetch_n = max_symbols * 4 if scope_path else max_symbols
        cur.execute("""
            SELECT file_path, symbol_name, symbol_type, scope, content,
                   docstring, language, start_line, end_line
            FROM code_index
            WHERE symbol_type != 'overview'
            ORDER BY file_path, start_line
            LIMIT ?
        """, (fetch_n,))
        scope_label = scope_path or "<project>"

    rows = cur.fetchall()
    if scope_path and not file_path:
        filtered: list = []
        for row in rows:
            if os.path.isfile(os.path.join(scope_path, row[0])):
                filtered.append(row)
                if len(filtered) >= max_symbols:
                    break
        rows = filtered
    if not rows:
        return (f"No symbols indexed for {scope_label}.",
                f"code_skeleton scope={scope_label} results=0")

    # Group by file then by class scope
    by_file: dict[str, dict] = {}
    for fp, sn, st, scope, content, doc, lang, sl, el in rows:
        f = by_file.setdefault(fp, {"language": lang or "?", "classes": {}, "free": []})
        sig = (content or sn or "").strip().split("\n")[0][:200]
        entry = {
            "name": sn, "type": st, "sig": sig,
            "doc": (doc or "").strip().splitlines()[0][:80] if doc else None,
            "lines": f"{sl}-{el}" if sl else None,
        }
        if scope and st == "method":
            f["classes"].setdefault(scope, []).append(entry)
        else:
            f["free"].append(entry)

    # Render compact markdown — scope warning first so AI sees the
    # multi-project pooling caveat before consuming the symbol list.
    lines = [_scope_header() + f"# 📋 Code Skeleton — {scope_label}\n"]
    total_syms = 0
    for fp, data in by_file.items():
        lines.append(f"## `{fp}` ({data['language']})")
        for cls_name, methods in data["classes"].items():
            lines.append(f"### class {cls_name}")
            for m in methods:
                doc = f"  _{m['doc']}_" if m["doc"] else ""
                lines.append(f"  - `{m['sig']}`{doc}")
                total_syms += 1
        for s in data["free"]:
            doc = f"  _{s['doc']}_" if s["doc"] else ""
            lines.append(f"- `{s['sig']}` _{s['type']}_{doc}")
            total_syms += 1
        lines.append("")

    # JSX outline — only when caller asked for a specific TSX/JSX file
    # (skip for project-wide skeleton to keep output bounded)
    if file_path and file_path.lower().endswith((".tsx", ".jsx")):
        cur.execute(
            "SELECT body FROM code_index WHERE file_path = ? AND symbol_type = 'file' LIMIT 1",
            (file_path,),
        )
        row = cur.fetchone()
        if row and row[0]:
            outline = _extract_jsx_outline(row[0])
            if outline:
                lines.append(f"### 🌲 JSX outline")
                lines.append("```")
                lines.append(outline)
                lines.append("```")
                lines.append("")

    sources = [{"file": fp} for fp in by_file.keys()][:10]
    out = "\n".join(lines)
    out += f"\n\n_Returned {total_syms} signatures from {len(by_file)} file(s)._"
    if sources:
        out += "\n\n**Citations:** " + ", ".join(f"`{s['file']}`" for s in sources)
    return (out, f"code_skeleton scope={scope_label} symbols={total_syms} files={len(by_file)}")


def _handle_code_context_bundle(arguments, store):
    """
    One-shot aggregator. Returns: top symbols + their callers + callees + related decisions.
    Saves 5-10 round-trips. Each chunk is small for token economy.
    """
    query = (arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query required")
    depth = int(arguments.get("depth") or 1)

    cur = store().conn.cursor()
    # 1. Top matching symbols (FTS5).
    # Use _fts_safe_query so multi-word queries become OR-of-tokens
    # instead of FTS5's default AND. The default mode rejected
    # "stripe webhook subscription update" (4 mandatory tokens) where
    # find_relevant_code happily returned the stripe-webhook handler
    # via _fts_safe_query. The two tools should behave consistently
    # against the same FTS index — see BUG-03 in the QA report.
    fts_q = _fts_safe_query(query)
    try:
        cur.execute("""
            SELECT id, symbol_name, symbol_type, file_path, content, start_line
            FROM code_fts
            WHERE code_fts MATCH ?
            ORDER BY rank LIMIT 5
        """, (fts_q,))
        top_rows = cur.fetchall()
    except Exception:
        top_rows = []

    if not top_rows:
        # Fallback to LIKE search using the original (un-tokenized) query.
        cur.execute("""
            SELECT id, symbol_name, symbol_type, file_path, content, start_line
            FROM code_index
            WHERE symbol_name LIKE ? OR content LIKE ?
            LIMIT 5
        """, (f"%{query}%", f"%{query}%"))
        top_rows = cur.fetchall()

    sources = []
    bundle = {"query": query, "primary": [], "callers": [], "callees": [], "decisions": []}
    seen_ids = set()

    for sid, sname, stype, fp, content, sl in top_rows:
        seen_ids.add(sid)
        sig = (content or sname or "")[:150].split("\n")[0]
        bundle["primary"].append({
            "id": sid, "name": sname, "type": stype,
            "file": fp, "line": sl, "signature": sig,
        })
        sources.append({"file": fp, "line": sl})

    # 2. Direct callers + callees for top symbols
    if seen_ids and depth >= 1:
        ph = ",".join(["?"] * len(seen_ids))
        # Callers (reverse)
        cur.execute(f"""
            SELECT c.symbol_name, c.symbol_type, c.file_path, c.start_line, l.target_id
            FROM code_links l
            JOIN code_index c ON l.source_id = c.id
            WHERE l.target_id IN ({ph}) AND l.link_type = 'call'
            LIMIT 20
        """, list(seen_ids))
        for sn, st, fp, sl, target_id in cur.fetchall():
            target_name = next((p["name"] for p in bundle["primary"] if p["id"] == target_id), "?")
            bundle["callers"].append({
                "calls": target_name, "from": sn, "type": st,
                "file": fp, "line": sl,
            })
            sources.append({"file": fp, "line": sl})

        # Callees (forward)
        cur.execute(f"""
            SELECT c.symbol_name, c.symbol_type, c.file_path, c.start_line, l.source_id
            FROM code_links l
            JOIN code_index c ON l.target_id = c.id
            WHERE l.source_id IN ({ph}) AND l.link_type = 'call'
            LIMIT 20
        """, list(seen_ids))
        for sn, st, fp, sl, source_id in cur.fetchall():
            source_name = next((p["name"] for p in bundle["primary"] if p["id"] == source_id), "?")
            bundle["callees"].append({
                "called_by": source_name, "name": sn, "type": st,
                "file": fp, "line": sl,
            })

    # 3. Related decisions/notes from memories_v2
    cur.execute("""
        SELECT id, content, category, created_at FROM memories_v2
        WHERE content LIKE ? OR tags LIKE ?
        ORDER BY created_at DESC LIMIT 5
    """, (f"%{query}%", f"%{query}%"))
    for mid, content, cat, ts in cur.fetchall():
        first = (content or "")[:120].split("\n")[0]
        bundle["decisions"].append({
            "id": mid, "category": cat or "note",
            "snippet": first, "ts": (ts or "")[:10],
        })
        sources.append({"memory_id": mid})

    # Render markdown
    lines = [f"# 📦 Context Bundle — `{query}`\n"]
    if bundle["primary"]:
        lines.append("## 🎯 Primary symbols")
        for p in bundle["primary"]:
            lines.append(f"- **{p['name']}** ({p['type']}) — `{p['file']}:{p['line']}`")
            lines.append(f"  `{p['signature']}`")
    if bundle["callers"]:
        lines.append("\n## 📞 Callers")
        for c in bundle["callers"][:10]:
            lines.append(f"- {c['from']} ({c['file']}:{c['line']}) → calls `{c['calls']}`")
    if bundle["callees"]:
        lines.append("\n## 📤 Callees")
        for c in bundle["callees"][:10]:
            lines.append(f"- `{c['called_by']}` calls → {c['name']} ({c['file']}:{c['line']})")
    if bundle["decisions"]:
        lines.append("\n## 🧠 Related memories")
        for d in bundle["decisions"]:
            lines.append(f"- [{d['ts']}] [{d['category']}] {d['snippet']}")

    if not (bundle["primary"] or bundle["decisions"]):
        lines.append("_No matches in code or memories._")

    out = "\n".join(lines)
    out += f"\n\n**Citations:** {len(sources)} sources verified by AST + DB"
    return (out,
            f"code_bundle q='{query}' primary={len(bundle['primary'])} "
            f"callers={len(bundle['callers'])} callees={len(bundle['callees'])} "
            f"decisions={len(bundle['decisions'])}")


def _handle_code_trace_value(arguments, store):
    """C.1 — Limited static taint analysis. Trace `symbol`'s return value
    through callers, stopping when a caller crosses a serialization
    boundary or max_depth is reached.

    0.2.16 fixes:
      - Issue #2: dedup reached edges by (file, line, caller_name)
      - Issue #3: also inspect the trace target's OWN body for
        boundaries (covers the case where the function being traced
        contains the boundary call itself)
      - Issue #4: accept optional `scope_path` to filter target rows
        by file_path prefix (disambiguate same-named symbols across
        tutorials/test/etc.)
    """
    from core.code_indexer.b_layer import (
        decorate_edges, detect_boundary_in_body, index_metadata,
        render_edges_markdown,
    )
    name = (arguments.get("symbol_name") or "").strip()
    if not name:
        raise ValueError("symbol_name required")
    max_depth = int(arguments.get("max_depth", 3))
    scope_path = (arguments.get("scope_path") or arguments.get("path") or "").strip()
    _, resolved_pid, resolved_ppath, resolved_rel_scope = _normalize_file_path(None, scope_path or None)
    if resolved_rel_scope is not None:
        scope_path = resolved_rel_scope
    elif scope_path:
        scope_path = os.path.realpath(os.path.expanduser(scope_path))

    conn = store().conn
    cur = conn.cursor()
    # If scope_path resolves to a relative scope (either specific subdirectory or project root)
    if scope_path:
        cur.execute(
            "SELECT id, file_path, start_line, body FROM code_index "
            "WHERE symbol_name = ? AND file_path LIKE ?",
            (name, f"{scope_path}%"),
        )
    else:
        cur.execute(
            "SELECT id, file_path, start_line, body FROM code_index "
            "WHERE symbol_name = ?",
            (name,),
        )
    target_rows = cur.fetchall()
    if not target_rows:
        scope_note = f" (scope_path={scope_path!r})" if scope_path else ""
        return (f"Symbol '{name}' not found in index{scope_note}.",
                f"code_trace_value='{name}' not_found")

    # Note: if no scope_path and multiple defs exist, warn user
    if not scope_path and len(target_rows) > 1:
        files = sorted({r[1] for r in target_rows})
        warning = (
            f"⚠ `{name}` has {len(target_rows)} definitions across:\n"
            + "\n".join(f"  - {f}" for f in files[:10])
            + "\n\nPass `scope_path` to disambiguate. Continuing with all "
              "definitions — results may conflate unrelated functions.\n\n"
        )
    else:
        warning = ""

    targets = [r[0] for r in target_rows]

    # 0.2.17 — single set of terminated entries keyed by (file, line, kind)
    # so the same boundary isn't reported once per caller (Issue #5a).
    terminated_set: set[tuple[str, int, str]] = set()
    terminated_at: list[dict] = []

    def _record_boundary(file_path, line, kind, sym, where):
        # 0.2.17 — dedup before append (Issue #5a)
        # 0.2.18 Issue #9 — also apply scope_path filter to boundary
        # entries. Previously boundary entries leaked across scope
        # (e.g. trace scoped to tutorial005_py310 still reported
        # boundary at tutorial005_an_py310.py:104 because the 2-hop
        # peek crossed into other files).
        if scope_path:
            if resolved_rel_scope is not None:
                if resolved_rel_scope and not (file_path or "").startswith(resolved_rel_scope):
                    return
            else:
                abs_file = os.path.join(scope_path, file_path or "")
                if not abs_file.startswith(scope_path):
                    return
        key = (file_path, line, kind)
        if key in terminated_set:
            return
        terminated_set.add(key)
        terminated_at.append({
            "file": file_path, "line": line, "kind": kind,
            "symbol": sym, "where": where,
        })

    # Target may itself contain a boundary
    for tid, tfile, tline, tbody in target_rows:
        is_b, bkind, bline = detect_boundary_in_body(tbody or "")
        if is_b:
            # 0.2.17 Issue #5b — report the ACTUAL line of the boundary
            # call, not the function's start_line. tline + bline - 1.
            actual_line = (tline or 1) + max(bline, 1) - 1
            _record_boundary(tfile, actual_line, bkind, name, "in_target_body")

    visited = set()
    reached_set: set[tuple[str, str, int, str]] = set()
    reached: list[tuple[str, str, int, str]] = []
    frontier = list(targets)
    depth = 0
    while frontier and depth < max_depth:
        next_frontier = []
        for tid in frontier:
            if tid in visited:
                continue
            visited.add(tid)
            cur.execute("""
                SELECT c.id, c.symbol_name, c.file_path, c.start_line,
                       c.body, l.link_type
                FROM code_links l
                JOIN code_index c ON l.source_id = c.id
                WHERE l.target_id = ?
            """, (tid,))
            for cid, csym, cfile, cline, cbody, ltype in cur.fetchall():
                # 0.2.17 Issue #6 — apply scope_path filter to result rows,
                # not just target lookup. If scope is set and this caller
                # lives outside that path, skip it.
                if scope_path:
                    if resolved_rel_scope is not None:
                        if resolved_rel_scope and not (cfile or "").startswith(resolved_rel_scope):
                            continue
                    else:
                        abs_file = os.path.join(scope_path, cfile or "")
                        if not abs_file.startswith(scope_path):
                            continue
                key = (csym, cfile, cline, ltype)
                if key in reached_set:
                    continue
                reached_set.add(key)
                is_b, bkind, bline = detect_boundary_in_body(cbody or "")
                if is_b:
                    # 0.2.17 Issue #5b — actual boundary line, not caller decl
                    actual_line = (cline or 1) + max(bline, 1) - 1
                    _record_boundary(cfile, actual_line, bkind, csym, "caller_body")
                else:
                    # 2-hop fallback: check what THIS caller calls
                    cur2 = conn.cursor()
                    cur2.execute("""
                        SELECT c2.symbol_name, c2.file_path, c2.start_line, c2.body
                        FROM code_links l2
                        JOIN code_index c2 ON l2.target_id = c2.id
                        WHERE l2.source_id = ?
                    """, (cid,))
                    for csym2, cfile2, cline2, cbody2 in cur2.fetchall():
                        is_b2, bkind2, bline2 = detect_boundary_in_body(cbody2 or "")
                        if is_b2:
                            actual2 = (cline2 or 1) + max(bline2, 1) - 1
                            _record_boundary(cfile2, actual2, bkind2, csym2,
                                             f"called_by_{csym}")
                            is_b = True
                            break
                reached.append((csym, cfile, cline, ltype))
                if not is_b:
                    next_frontier.append(cid)
        frontier = next_frontier
        depth += 1

    edges = decorate_edges(reached, has_link_type=True)
    md = render_edges_markdown(
        edges, title=f"Value trace (depth ≤ {max_depth})",
        target_name=name,
        paths_terminated=terminated_at,
        index_metadata=index_metadata(conn),
    )
    return (warning + md,
            f"code_trace_value='{name}' reached={len(edges)} terminated={len(terminated_at)} scope={scope_path or 'all'}")


def _handle_code_analyze_refactor_impact(arguments, store):
    """C.2 — Composite tool. Bundles callers + boundary-respecting trace
    into one response so LLM clients don't fan out to 5 small calls.

    0.2.16 fix Issue #4: accept optional `scope_path` and forward it to
    every sub-call so same-name symbols across tutorials/tests don't
    pollute the impact analysis.
    """
    name = (arguments.get("symbol_name") or "").strip()
    change_kind = (arguments.get("change_kind") or "return_type").strip()
    scope_path = (arguments.get("scope_path") or "").strip()
    if not name:
        raise ValueError("symbol_name required")

    sub_args = {"symbol_name": name}
    if scope_path:
        sub_args["scope_path"] = scope_path

    callers_out, _ = _handle_code_find_callers(sub_args, store)
    # max_depth 3→5: tutorial-shaped repos (FastAPI, Django docs) commonly
    # wrap auth/serialization 3-4 hops below the route handler. Depth 3
    # missed jwt.encode in tutorial004's create_access_token; depth 5
    # reaches the actual boundary. Cost is O(branching × depth) but
    # gather-then-rank already caps the rendered output.
    trace_out, _ = _handle_code_trace_value(
        {**sub_args, "max_depth": 5}, store
    )
    callees_out, _ = _handle_code_callees(sub_args, store)

    # ── Auto-fetch lessons (P0 #2, 2026-05-14) ──
    # The compound-lesson loop is Cosmos's killer feature, but it only
    # fires if the AI thinks to call find_relevant_code first. Composite
    # tools should NOT depend on AI initiative — surface relevant lessons
    # inline so the value lands even when the AI never asked for them.
    lesson_section = _fetch_lesson_section(
        symptom=f"change {change_kind} of {name}",
        path=scope_path or None,
        store=store,
        limit=3,
    )

    scope_note = f" (scope: `{scope_path}*`)" if scope_path else ""
    md = [
        f"# Refactor impact analysis: `{name}` ({change_kind}){scope_note}\n",
        "## Direct callers", callers_out,
        "## Value trace (boundary-respecting, depth=5)", trace_out,
        "## Downstream callees", callees_out,
    ]
    if lesson_section:
        md.append(lesson_section)
    md += [
        "\n---",
        "_Composite reply assembled by code_analyze_refactor_impact. "
        "Prefer this over orchestrating callers + uses + trace + callees "
        "yourself._",
    ]
    if not scope_path:
        md.insert(1, "_Tip: pass `scope_path` (e.g. `docs_src/security/`) "
                     "to constrain results to one tutorial / package and "
                     "avoid same-name conflation across the repo._\n")
    return ("\n\n".join(md),
            f"code_analyze_refactor_impact='{name}' change={change_kind} scope={scope_path or 'all'}")


def _fetch_lesson_section(symptom: str, path, store, limit: int = 3) -> str:
    """Find past lessons matching a free-text symptom and render them as
    a markdown section ready to append to a tool response.

    Used by composite/refactor tools to ensure the lesson loop fires even
    when the AI never explicitly called `find_relevant_code` or
    `code_list_errors`. Returns an empty string when no lessons score
    above zero — never adds noise.
    """
    try:
        from core.code_indexer.errors import (
            get_code_errors, resolve_project_id, score_lesson_for_query,
        )
        from core.code_indexer.project_registry import get_project_registry
        import re

        proj_id = resolve_project_id(path) if path else None
        errors_svc = get_code_errors()
        if proj_id:
            all_errors = errors_svc.list_for_project(proj_id, limit=200)
        else:
            cursor = store().conn.cursor()
            all_errors = _list_all_errors(cursor)

        sym_tokens = set(re.findall(r"[A-Za-z0-9_]{3,}|[฀-๿]{2,}", symptom.lower()))
        scored = []
        for e in all_errors:
            s = score_lesson_for_query(e, symptom_tokens=sym_tokens, current_path=path)
            if s > 0:
                scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        top = [e for _, e in scored[:limit]]
        if not top:
            return ""

        proj_map = {p["id"]: p for p in get_project_registry().list()}
        lines = ["## ⚠️ Related lessons (auto-surfaced — applied before, may apply here)"]
        for e in top:
            sev = {1: "🔴", 2: "🟡", 3: "🟢"}.get(e.get("severity", 2), "·")
            pn = proj_map.get(e.get("project_id", ""), {}).get("name", "?")
            lines.append(f"- {sev} `{e['id'][:8]}` ({pn}) — {(e.get('symptom') or '')[:120]}")
            if e.get("fix"):
                lines.append(f"  fix: {(e['fix'])[:200].strip()}")
        return "\n".join(lines)
    except Exception:
        return ""


def _handle_code_boundaries(arguments, store):
    """C.3 — List boundary call sites in a path. Useful pre-flight before
    refactor questions to know where the call graph is cut."""
    from core.code_indexer.b_layer import detect_boundary_in_body, index_metadata
    path = (arguments.get("path") or "").strip()
    if not path:
        raise ValueError("path required")

    conn = store().conn
    cur = conn.cursor()
    # 0.2.19 Issue #11 — filter out file/module-level "symbols" so the
    # report shows only actual call sites. The indexer stamps a synthetic
    # entry per source file with start_line=1 and body=entire file
    # content; that always matches every boundary regex in the file and
    # produces noise like "tutorial005.py:1 (type_erasing)" alongside
    # the real boundary functions.
    cur.execute("""
        SELECT symbol_name, file_path, start_line, body, symbol_type
        FROM code_index
        WHERE file_path LIKE ?
          AND symbol_type IN ('function','method','class','async_function')
          AND start_line > 1
        ORDER BY file_path, start_line
    """, (f"{path}%",))

    hits = []
    for sym, fpath, line, body, _stype in cur.fetchall():
        if not body:
            continue
        is_b, kind, bline = detect_boundary_in_body(body)
        if is_b:
            # 0.2.19 — report the actual boundary call line (same fix as
            # trace_value Issue #5b), not the function declaration line.
            actual_line = (line or 1) + max(bline, 1) - 1
            hits.append((sym, fpath, actual_line, kind))

    if not hits:
        return (f"No serialization boundaries detected in `{path}`.\n"
                f"_Cosmos reports: **unable to determine** if path is unindexed "
                f"or genuinely boundary-free. Re-run code_reindex if recent code._",
                f"code_boundaries='{path}' results=0")

    by_kind = {}
    for sym, fpath, line, kind in hits:
        by_kind.setdefault(kind, []).append(f"  - `{sym}` ({fpath}:{line})")

    lines = [f"# Boundaries in `{path}`\n"]
    for k, items in by_kind.items():
        lines.append(f"## {k} ({len(items)})")
        lines.extend(items)
        lines.append("")

    meta = index_metadata(conn)
    lines.append(f"---\n_indexed: {meta.get('last_indexed')} · "
                 f"hash: {meta.get('content_hash')}_  ")
    lines.append(f"_count: {len(hits)} (numerical invariant verified)_")
    return ("\n".join(lines), f"code_boundaries='{path}' results={len(hits)}")


def _handle_cosmos_get_preamble(arguments, store):
    """D.3/D.4 — Serve the 3-tier project preamble. Auto-resolves the
    project from `path` (cwd or any path inside a watched project)."""
    from core.code_indexer import preamble
    from core.code_indexer.errors import resolve_project_id
    from core.code_indexer.project_registry import get_project_registry

    path = (arguments.get("path") or "").strip()
    if not path:
        raise ValueError("path required")
    tier = (arguments.get("tier") or "hot").strip().lower()
    known_hash = (arguments.get("known_hash") or "").strip()
    intent = (arguments.get("intent") or "").strip()

    project_id = resolve_project_id(path)
    if not project_id:
        return (
            f"No watched project contains path: {path}\n"
            f"_Cosmos reports: **unable to determine** — register this path "
            f"via code_reindex first, then re-request the preamble._",
            f"cosmos_get_preamble path={path} no_project",
        )

    proj = get_project_registry().get(project_id) or {}
    project_path = proj.get("path") or path

    h, body = preamble.get_preamble(store().conn, project_id, project_path, tier, intent=intent)
    if known_hash and known_hash == h:
        return (
            f"_unchanged (hash={h})_ — your cached preamble is still current.",
            f"cosmos_get_preamble project={project_id[:8]} tier={tier} unchanged",
        )

    # Prepend a small metadata header so the client knows the hash for next call
    header = (
        f"<!-- cosmos_preamble tier={tier} hash={h} project={project_id[:8]} -->\n\n"
    )
    return (header + body,
            f"cosmos_get_preamble project={project_id[:8]} tier={tier} bytes={len(body)}")


def _handle_cosmos_get_design_context(arguments, store):
    """Retrieve the visual theme and design styling tokens for the project."""
    from core.code_indexer.errors import resolve_project_id
    from core.code_indexer.project_registry import get_project_registry
    from core.code_indexer.preamble import _format_design_context_markdown

    path = (arguments.get("path") or "").strip()
    if not path:
        raise ValueError("path required")

    project_id = resolve_project_id(path)
    if not project_id:
        return (
            f"No watched project contains path: {path}\n"
            f"_Cosmos reports: **unable to determine** — register this path "
            f"via code_reindex first, then re-request the design context._",
            f"cosmos_get_design_context path={path} no_project",
        )

    proj = get_project_registry().get(project_id) or {}
    project_path = proj.get("path") or path

    conn = store().conn
    cur = conn.cursor()
    cur.execute("SELECT content FROM code_index WHERE id = ?", (f"project_overview:{project_path}",))
    row = cur.fetchone()
    
    if not row:
        try:
            from core.code_indexer.project_analyzer import ProjectAnalyzer
            analyzer = ProjectAnalyzer(project_path)
            overview = analyzer.analyze(conn)
            
            # Save it to the database so it's cached!
            cur.execute("""
                INSERT OR REPLACE INTO code_index (id, file_path, symbol_name, symbol_type, content, body, language, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                f"project_overview:{project_path}",
                project_path,
                "Project Overview",
                "overview",
                json.dumps(overview),
                "", # body placeholder
                "markdown"
            ))
            conn.commit()
            
            design_ctx = overview.get("design_context")
        except Exception as e:
            return (
                f"Failed to generate overview dynamically: {str(e)}",
                f"cosmos_get_design_context dynamically_generate_error={str(e)}",
            )
    else:
        try:
            overview = json.loads(row[0])
            design_ctx = overview.get("design_context")
        except Exception as e:
            return (
                f"Failed to parse overview JSON: {str(e)}",
                f"cosmos_get_design_context json_parse_error={str(e)}",
            )

    try:
        if not design_ctx:
            # Let's try to extract on-the-fly if not indexed yet
            from core.code_indexer.design_extractor import DesignExtractor
            design_ctx = DesignExtractor(project_path).extract()
            # Proactively save it back so it's cached!
            overview["design_context"] = design_ctx
            cur.execute(
                "UPDATE code_index SET content = ? WHERE id = ?",
                (json.dumps(overview), f"project_overview:{project_path}")
            )
            conn.commit()
        
        body = _format_design_context_markdown(design_ctx)
        
        # Read the human-readable DESIGN.md
        contract_path = os.path.join(project_path, "DESIGN.md")
        if os.path.isfile(contract_path):
            try:
                with open(contract_path, "r", encoding="utf-8") as f:
                    contract_content = f.read()
                body += f"\n\n## 📜 Active Design Contract (DESIGN.md)\n{contract_content}"
            except Exception:
                pass
                
        # Read design.tokens.json
        tokens_path = os.path.join(project_path, "design.tokens.json")
        if os.path.isfile(tokens_path):
            try:
                with open(tokens_path, "r", encoding="utf-8") as f:
                    tokens_content = f.read()
                body += f"\n\n## 🔢 Design Tokens (design.tokens.json)\n```json\n{tokens_content}\n```"
            except Exception:
                pass

        # Read docs/design_taste_memory.md
        taste_path = os.path.join(project_path, "docs/design_taste_memory.md")
        if os.path.isfile(taste_path):
            try:
                with open(taste_path, "r", encoding="utf-8") as f:
                    taste_content = f.read()
                body += f"\n\n## 🧠 Cosmos Design Taste Memory (docs/design_taste_memory.md)\n{taste_content}"
            except Exception:
                pass

        if not body:
            body = "_No CSS variables, Tailwind configurations, or UI stylesheets detected for this project._"
            
        return (body, f"cosmos_get_design_context project={project_id[:8]} bytes={len(body)}")
    except Exception as e:
        return (f"Failed to load design context: {str(e)}", f"cosmos_get_design_context error={str(e)}")


def _handle_cosmos_refresh_map(arguments, store):
    """Regenerate the Obsidian-style MOC at <project>/.cosmos/project_summary.md.

    Day-1 architectural view. Auto-derived from code_index — does NOT
    depend on accumulated lessons (which is what made requests-corpus
    Day-1 hit-rate weaker in replication; see brain memory d09fbfcd).
    """
    from core.code_indexer import project_map
    from core.code_indexer.errors import resolve_project_id
    from core.code_indexer.project_registry import get_project_registry

    path = (arguments.get("path") or "").strip()
    if not path:
        raise ValueError("path required")

    project_id = resolve_project_id(path)
    proj = get_project_registry().get(project_id) if project_id else None
    project_path = (proj or {}).get("path") or path
    if not os.path.isdir(project_path):
        return (f"❌ Project path does not exist: {project_path}",
                f"cosmos_refresh_map bad_path")

    out_path = project_map.write_moc(project_path, project_id, store().conn)
    try:
        size = out_path.stat().st_size
    except Exception:
        size = 0
    return (f"✅ Project map refreshed: `{out_path}`\n"
            f"\n_{size} bytes written. The block between `COSMOS:MOC:BEGIN` "
            f"and `COSMOS:MOC:END` markers is auto-generated; anything else "
            f"in the file is preserved across regen._",
            f"cosmos_refresh_map project={(project_id or '?')[:8]} bytes={size}")


def _handle_code_diff(arguments, store):
    """
    Git-aware diff for a symbol since `since` ref. Returns concise diff hunks
    that intersect the symbol's line range.
    """
    import subprocess
    name = (arguments.get("symbol_name") or "").strip()
    if not name:
        raise ValueError("symbol_name required")
    since = (arguments.get("since") or "HEAD~1").strip()

    # 1. Locate the symbol
    cur = store().conn.cursor()
    cur.execute("""
        SELECT file_path, start_line, end_line FROM code_index
        WHERE symbol_name = ? AND symbol_type != 'overview'
        ORDER BY length(body) DESC LIMIT 1
    """, (name,))
    row = cur.fetchone()
    if not row:
        return (f"Symbol '{name}' not found in index.",
                f"code_diff='{name}' not_found")
    file_path, start, end = row

    # 2. Run git diff
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", since, "--", file_path],
            capture_output=True, text=True, timeout=10,
        )
        diff_text = result.stdout
        if not diff_text:
            return (
                f"No changes in `{file_path}` since `{since}`.",
                f"code_diff='{name}' no_changes",
            )
    except FileNotFoundError:
        return ("Git not available in this environment.", f"code_diff='{name}' no_git")
    except subprocess.TimeoutExpired:
        return ("Git diff timed out.", f"code_diff='{name}' timeout")

    # 3. Filter hunks intersecting symbol's line range
    relevant = []
    cur_hunk = []
    in_relevant = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            if cur_hunk and in_relevant:
                relevant.extend(cur_hunk)
            cur_hunk = [line]
            # Parse @@ -a,b +c,d @@ → check if [c..c+d] intersects [start..end]
            try:
                plus_part = line.split("+")[1].split(" ")[0]
                if "," in plus_part:
                    new_start, new_count = map(int, plus_part.split(","))
                else:
                    new_start, new_count = int(plus_part), 1
                hunk_end = new_start + new_count
                in_relevant = (start is None or end is None or
                               (hunk_end >= (start or 0) and new_start <= (end or 999999)))
            except Exception:
                in_relevant = True
        elif cur_hunk is not None:
            cur_hunk.append(line)
    if cur_hunk and in_relevant:
        relevant.extend(cur_hunk)

    if not relevant:
        return (
            f"`{name}` (lines {start}-{end}) — no changes in that range since `{since}`.",
            f"code_diff='{name}' no_relevant_changes",
        )

    out = (
        f"# 🔀 Diff — `{name}` since `{since}`\n"
        f"**Location:** `{file_path}:{start}-{end}`\n\n"
        f"```diff\n{chr(10).join(relevant[:200])}\n```\n\n"
        f"**Citations:** `{file_path}:{start}-{end}` · git ref `{since}`"
    )
    return (out, f"code_diff='{name}' since='{since}' lines_changed={len(relevant)}")


def _handle_brain_session_context(arguments, store):
    """
    Auto-context loader. Call FIRST in a new conversation.
    Returns: recent activity, recently-edited folders, last decisions.
    Token target: ~200 tokens for typical brain.
    """
    from datetime import datetime, timedelta
    lookback = int(arguments.get("lookback_days") or 7)
    cutoff = (datetime.utcnow() - timedelta(days=lookback)).isoformat()

    cur = store().conn.cursor()

    # 1. Recent memory activity by category + folder
    cur.execute("""
        SELECT m.category, COALESCE(f.path, '<root>'), COUNT(*) as n
        FROM memories_v2 m
        LEFT JOIN folders f ON m.folder_id = f.id
        WHERE m.created_at >= ?
        GROUP BY m.category, f.path
        ORDER BY n DESC LIMIT 10
    """, (cutoff,))
    activity = [
        {"category": cat or "note", "folder": fp, "count": n}
        for cat, fp, n in cur.fetchall()
    ]

    # 2. Most recent 5 memories (actual content snippets)
    cur.execute("""
        SELECT id, category, content, created_at FROM memories_v2
        WHERE created_at >= ?
        ORDER BY created_at DESC LIMIT 5
    """, (cutoff,))
    recent = [
        {"id": mid, "category": cat or "note",
         "snippet": (content or "")[:80].split("\n")[0],
         "ts": (ts or "")[:10]}
        for mid, cat, content, ts in cur.fetchall()
    ]

    # 3. Recently-touched code files (from code_index updated_at)
    cur.execute("""
        SELECT DISTINCT file_path
        FROM code_index
        WHERE updated_at >= ? AND symbol_type != 'overview'
        ORDER BY updated_at DESC LIMIT 10
    """, (cutoff,))
    recent_code = [r[0] for r in cur.fetchall()]

    # 4. Open TODOs
    cur.execute("""
        SELECT content, created_at FROM memories_v2
        WHERE category = 'task'
          AND (typed_data IS NULL
               OR json_extract(typed_data, '$.done') IS NOT 1)
        ORDER BY created_at DESC LIMIT 10
    """)
    open_todos = [
        {"snippet": (c or "")[:60].split("\n")[0], "ts": (ts or "")[:10]}
        for c, ts in cur.fetchall()
    ]

    # 5. Latest job results
    try:
        from core.jobs.scheduler import get_scheduler
        last_week = get_scheduler().latest_result("weekly_summary")
        weekly_brief = last_week.get("result", {}) if last_week else None
    except Exception:
        weekly_brief = None

    # Render compact context
    lines = [f"# 🧭 Session Context — last {lookback} days\n"]

    if activity:
        lines.append("## 📊 Activity by area")
        for a in activity[:5]:
            lines.append(f"- {a['category']} in {a['folder']}: {a['count']}")

    if recent:
        lines.append("\n## 🕐 Most recent")
        for r in recent:
            lines.append(f"- [{r['ts']}] [{r['category']}] {r['snippet']}")

    if recent_code:
        lines.append("\n## 💻 Recently-touched code")
        for fp in recent_code[:5]:
            lines.append(f"- `{fp}`")

    if open_todos:
        lines.append(f"\n## ✅ Open TODOs ({len(open_todos)})")
        for t in open_todos[:5]:
            lines.append(f"- {t['snippet']}")

    if weekly_brief:
        lines.append(f"\n## 📅 Weekly brief")
        if "total_new" in weekly_brief:
            lines.append(f"- {weekly_brief['total_new']} new memories")
        for c in (weekly_brief.get("by_category") or [])[:3]:
            lines.append(f"  - {c.get('category', '?')}: {c.get('count', 0)}")

    if not any([activity, recent, recent_code, open_todos]):
        lines.append("_No activity in the lookback window. Brain is quiet._")

    return ("\n".join(lines),
            f"session_context days={lookback} recent={len(recent)} todos={len(open_todos)} "
            f"code={len(recent_code)}")


def _handle_brain_pattern_recall(arguments, store):
    """
    Recall user's saved patterns/preferences. Patterns are stored as memories
    with category='pattern'. Optional sub-category filter via tags.
    """
    category = (arguments.get("category") or "").strip()
    cur = store().conn.cursor()
    if category:
        cur.execute("""
            SELECT id, content, tags, created_at FROM memories_v2
            WHERE category = 'pattern' AND tags LIKE ?
            ORDER BY importance_score DESC, created_at DESC LIMIT 30
        """, (f"%{category}%",))
    else:
        cur.execute("""
            SELECT id, content, tags, created_at FROM memories_v2
            WHERE category = 'pattern'
            ORDER BY importance_score DESC, created_at DESC LIMIT 30
        """)
    rows = cur.fetchall()

    if not rows:
        # Empty-state used to be a single discouraging line. Replaced with
        # a worked-example primer so the AI/user can see what a useful
        # pattern looks like and copy the shape — without seeding real
        # records that would later pollute brain_search results.
        guidance = (
            "_No patterns stored yet._\n\n"
            "💡 Patterns are short rules / preferences / conventions you want "
            "the AI to follow when generating code or text. Save with "
            "`brain_remember` using `category='pattern'` + a topical tag.\n\n"
            "**Example patterns to copy/adapt:**\n"
            "```\n"
            "brain_remember(\n"
            "  content=\"Use 4-space indent, no tabs in Python files.\",\n"
            "  category=\"pattern\", tags=[\"style\", \"python\"]\n"
            ")\n\n"
            "brain_remember(\n"
            "  content=\"Comments in Thai are OK in personal scripts but \"\n"
            "          \"English-only for files under core/.\",\n"
            "  category=\"pattern\", tags=[\"i18n\", \"convention\"]\n"
            ")\n\n"
            "brain_remember(\n"
            "  content=\"All API endpoints return JSON. Errors include a \"\n"
            "          \"`error` string + optional `detail` and HTTP code.\",\n"
            "  category=\"pattern\", tags=[\"api\", \"architecture\"]\n"
            ")\n"
            "```\n\n"
            "Common tag buckets: `naming`, `style`, `architecture`, "
            "`testing`, `i18n`, `convention`."
        )
        return (guidance, f"pattern_recall cat='{category}' results=0")

    by_tag: dict[str, list] = {}
    sources = []
    for mid, content, tags_raw, ts in rows:
        tags = []
        if tags_raw:
            try:
                import json as _json
                tags = _json.loads(tags_raw) if tags_raw.startswith("[") else \
                       [t.strip() for t in tags_raw.split(",")]
            except Exception:
                tags = []
        primary_tag = next((t for t in tags if t not in ("pattern",)), "general")
        body = (content or "").strip()
        by_tag.setdefault(primary_tag, []).append({
            "id": mid, "snippet": body[:200],
            "ts": (ts or "")[:10],
        })
        sources.append({"memory_id": mid})

    label = f" ({category})" if category else ""
    lines = [f"# 🎨 User Patterns{label}\n"]
    for tag, items in sorted(by_tag.items()):
        lines.append(f"## #{tag}")
        for it in items[:8]:
            lines.append(f"- {it['snippet']}")
        lines.append("")

    lines.append(f"_Returned {len(rows)} pattern(s) from {len(by_tag)} categories_")
    lines.append(f"\n**Citations:** {len(sources)} memory IDs verified")
    return ("\n".join(lines),
            f"pattern_recall cat='{category or 'all'}' results={len(rows)}")


def _handle_code_reindex(arguments):
    """Trigger a re-index of a watched project. Returns status + project info."""
    from core.code_indexer.project_registry import get_project_registry
    from core.code_indexer.watcher_manager import get_watcher_manager

    registry = get_project_registry()
    manager = get_watcher_manager()
    project_id = (arguments.get("project_id") or "").strip()
    path = (arguments.get("path") or "").strip()

    if not project_id and not path:
        projects = manager.status()
        if not projects:
            return ("_No projects are registered for indexing yet._\n\n"
                    "Add one via the Cosmos UI (Settings → Indexed Projects) "
                    "or POST /api/v1/projects.",
                    "code_reindex projects=0")
        lines = ["# 📂 Indexed Projects", ""]
        for p in projects:
            badge = "🟢 watching" if p["is_watching"] else "⚪️ idle"
            stats = p.get("stats") or {}
            stats_str = (
                f"{stats.get('files', 0)} files · "
                f"{stats.get('symbols', 0)} symbols · "
                f"{stats.get('links', 0)} links"
            )
            last = p.get("last_indexed_at") or "never"
            lines.append(f"- **{p['name']}** ({badge})")
            lines.append(f"  - id: `{p['id']}`")
            lines.append(f"  - path: `{p['path']}`")
            lines.append(f"  - {stats_str} · last indexed: {last}")
        lines.append("")
        lines.append("To re-index a specific one, call again with `project_id` "
                     "or `path`.")
        return ("\n".join(lines),
                f"code_reindex listed={len(projects)}")

    target = registry.get(project_id) if project_id else registry.find_by_path(path)
    auto_registered = False
    if not target and path:
        # Auto-register if a usable path was supplied
        try:
            target = registry.add(
                path=path,
                auto_watch=bool(arguments.get("auto_watch", True)),
            )
            manager.sync()  # spin up the watcher for the new project
            auto_registered = True
        except ValueError as e:
            return (f"❌ Cannot register `{path}`: {e}", "code_reindex bad_path")

    if not target:
        return (f"❌ Project not found "
                f"(project_id='{project_id}' path='{path}'). "
                f"Pass `path` to register and watch a new project.",
                "code_reindex not_found")

    ok = manager.trigger_reindex(target["id"])
    if not ok:
        return ("❌ Failed to queue re-index.", "code_reindex failed")

    prefix = ("🆕 Registered + indexing **{name}** (`{p}`).\n"
              "Auto-watch is **{aw}** — future edits will refresh the brain in "
              "~2 seconds."
              if auto_registered else
              "✅ Re-index queued for **{name}** (`{p}`).")
    msg = prefix.format(
        name=target["name"], p=target["path"],
        aw="ON" if target.get("auto_watch") else "OFF",
    )
    msg += ("\nRun `code_reindex` again in a few seconds to see updated stats.")
    return (msg, f"code_reindex {'registered' if auto_registered else 'queued'} "
                 f"id={target['id']}")


# ────────────────────────────────────────────────────────────────
# Claude Code dogfooding telemetry — Phase 8
# ────────────────────────────────────────────────────────────────

def _ensure_folder_path(conn, full_path: str) -> str | None:
    """Resolve a slash-path to a folder_id, creating any missing segments.

    Examples: '/Reports/Claude-Code' or '/Reports/Claude-Code/2026-04'.
    Returns the leaf folder's id, or None on failure."""
    try:
        from core.memory.folder import FolderTree
        ft = FolderTree(conn)
        ft.ensure_defaults()

        existing = ft.get_by_path(full_path)
        if existing:
            return existing["id"]

        segments = [s for s in full_path.split("/") if s]
        parent_id = None
        cur_path = ""
        for seg in segments:
            cur_path = f"{cur_path}/{seg}"
            node = ft.get_by_path(cur_path)
            if node is None:
                node = ft.create(name=seg, parent_id=parent_id)
            parent_id = node["id"] if node else parent_id
        return parent_id
    except Exception as e:
        print(f"[_ensure_folder_path] failed for {full_path}: {e}", file=sys.stderr)
        return None


def _read_recent_activity(since_iso: str = None, exclude_tools=None) -> list:
    """Read mcp_activity.jsonl entries newer than `since_iso`.
    `exclude_tools` are tool names whose calls should be filtered out
    (e.g. the logging tools themselves)."""
    import os
    # Frozen-aware (App Support in the bundled app), NOT cwd-relative — the
    # installed sidecar's cwd is the .app Resources dir, so the old relative
    # "data/brain_v2/..." never existed and this silently returned [] (broke
    # claude_log_task's tools-auto-detect + claude_report). Same bug as the
    # Outcome dashboard's activity path.
    from core.runtime_config import activity_log_path
    log_path = str(activity_log_path())
    if not os.path.exists(log_path):
        return []
    excluded = set(exclude_tools or [])
    out = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since_iso and (rec.get("ts") or "") <= since_iso:
                continue
            if rec.get("tool") in excluded:
                continue
            out.append(rec)
    return out


def _handle_claude_log_task(arguments, store):
    """Record a Claude Code task into /Reports/Claude-Code/.

    Auto-detects which MCP tools were called since the *previous* claude_log_task
    entry, so the user only has to supply task description + token counts."""
    from core.setup.brain_manifest import get_manifest
    from datetime import datetime, timezone

    task = (arguments.get("task") or "").strip()
    if not task:
        raise ValueError("`task` is required and cannot be empty")

    s = store()

    # Find the timestamp of the previous claude_log_task call to scope tools_used
    cur = s.conn.cursor()
    cur.execute("""
        SELECT created_at FROM memories_v2
        WHERE category = 'claude_session'
        ORDER BY created_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    prev_ts = row[0] if row else None

    activity = _read_recent_activity(
        since_iso=prev_ts,
        exclude_tools=["claude_log_task", "claude_report"],
    )

    # Auto-detect tools_used + count errors observed since last log
    tools_seen = []
    seen_set = set()
    auto_errors = 0
    for rec in activity:
        t = rec.get("tool")
        if t and t not in seen_set:
            seen_set.add(t)
            tools_seen.append(t)
        if rec.get("status") in ("error", "denied"):
            auto_errors += 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    files_edited = list(arguments.get("files_edited") or [])

    typed_data = {
        "task":           task,
        "tokens_input":   int(arguments.get("tokens_input") or 0),
        "tokens_output":  int(arguments.get("tokens_output") or 0),
        "tools_used":     tools_seen,
        "files_edited":   files_edited,
        "compile_errors": int(arguments.get("compile_errors") or 0),
        "retries":        int(arguments.get("retries") or 0),
        "semantic_bugs":  int(arguments.get("semantic_bugs") or 0),
        "duration_min":   float(arguments.get("duration_min") or 0.0),
        "outcome":        (arguments.get("outcome") or "success"),
        "ended_at":       now,
        "auto_mcp_errors": auto_errors,
    }
    notes = arguments.get("notes") or ""

    # Build a readable markdown body
    parts = [f"# 🤖 {task}", ""]
    parts.append(f"**Outcome:** {typed_data['outcome']}  ")
    parts.append(f"**Tokens:** {typed_data['tokens_input']} in + "
                 f"{typed_data['tokens_output']} out  ")
    if tools_seen:
        parts.append(f"**Tools used ({len(tools_seen)}):** "
                     + ", ".join(f"`{t}`" for t in tools_seen) + "  ")
    if files_edited:
        parts.append(f"**Files edited ({len(files_edited)}):** "
                     + ", ".join(f"`{f}`" for f in files_edited) + "  ")
    parts.append(
        f"**Errors:** compile={typed_data['compile_errors']}, "
        f"retries={typed_data['retries']}, "
        f"semantic={typed_data['semantic_bugs']}, "
        f"mcp={auto_errors}  "
    )
    if typed_data["duration_min"]:
        parts.append(f"**Duration:** {typed_data['duration_min']:.1f} min  ")
    if notes:
        parts.extend(["", "## Notes", notes])
    body = "\n".join(parts)

    # Resolve target folder via manifest (auto-expands YYYY-MM)
    manifest = get_manifest()
    target_folder = manifest.expand_path("/Reports/Claude-Code")

    folder_id = _ensure_folder_path(s.conn, target_folder)

    mem_id = s.store(
        content=body,
        category="claude_session",
        typed_data=typed_data,
        tags=["claude-code", "mcp", "dogfood", typed_data["outcome"]],
        folder_id=folder_id,
        source="claude_log_task",
    )

    return (
        f"✅ Logged task **{task}**\n"
        f"- Outcome: {typed_data['outcome']}\n"
        f"- Tools auto-detected: {len(tools_seen)}\n"
        f"- Files: {len(files_edited)}\n"
        f"- Tokens: {typed_data['tokens_input']} in / "
        f"{typed_data['tokens_output']} out\n"
        f"- MCP errors during task: {auto_errors}\n"
        f"- Saved to `{target_folder}` (id `{mem_id[:8]}…`)",
        f"claude_log_task '{task[:40]}' tools={len(tools_seen)} "
        f"errs={auto_errors}",
    )


def _handle_claude_report(arguments, store):
    """Aggregate claude_session memories into a markdown report."""
    from datetime import datetime, timezone, timedelta

    period = (arguments.get("period") or "week").lower()
    limit = int(arguments.get("limit") or 50)

    cutoffs = {
        "day":   timedelta(days=1),
        "week":  timedelta(days=7),
        "month": timedelta(days=30),
        "all":   None,
    }
    delta = cutoffs.get(period, timedelta(days=7))
    cutoff_iso = None
    if delta is not None:
        cutoff_iso = (datetime.now(timezone.utc) - delta) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")

    s = store()
    cur = s.conn.cursor()
    if cutoff_iso:
        cur.execute("""
            SELECT id, content, typed_data, created_at, tags
            FROM memories_v2
            WHERE category = 'claude_session' AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
        """, (cutoff_iso, limit))
    else:
        cur.execute("""
            SELECT id, content, typed_data, created_at, tags
            FROM memories_v2
            WHERE category = 'claude_session'
            ORDER BY created_at DESC LIMIT ?
        """, (limit,))
    rows = cur.fetchall()

    if not rows:
        return (
            f"_No `claude_session` logs in period **{period}**._\n\n"
            "Tip: call `claude_log_task` after each task to start collecting "
            "telemetry.",
            f"claude_report period={period} results=0",
        )

    sessions = []
    for mem_id, content, td_json, created_at, tags_json in rows:
        try:
            td = json.loads(td_json) if td_json else {}
        except Exception:
            td = {}
        sessions.append({
            "id": mem_id,
            "created_at": created_at,
            "td": td,
        })

    # Aggregate
    total_in = sum(s_["td"].get("tokens_input", 0) for s_ in sessions)
    total_out = sum(s_["td"].get("tokens_output", 0) for s_ in sessions)
    total_compile = sum(s_["td"].get("compile_errors", 0) for s_ in sessions)
    total_retries = sum(s_["td"].get("retries", 0) for s_ in sessions)
    total_semantic = sum(s_["td"].get("semantic_bugs", 0) for s_ in sessions)
    total_mcp_err = sum(s_["td"].get("auto_mcp_errors", 0) for s_ in sessions)

    by_outcome = {}
    for s_ in sessions:
        o = s_["td"].get("outcome", "unknown")
        by_outcome[o] = by_outcome.get(o, 0) + 1

    # Top tools
    tool_counts = {}
    for s_ in sessions:
        for t in (s_["td"].get("tools_used") or []):
            tool_counts[t] = tool_counts.get(t, 0) + 1
    top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:8]

    # Cost estimate at a rough $3/M input + $15/M output (Sonnet)
    cost_usd = total_in * 3 / 1_000_000 + total_out * 15 / 1_000_000

    accuracy_pct = 0.0
    if sessions:
        clean = sum(1 for s_ in sessions
                    if s_["td"].get("compile_errors", 0) == 0
                    and s_["td"].get("retries", 0) == 0)
        accuracy_pct = clean / len(sessions) * 100

    avg_in  = total_in  / len(sessions) if sessions else 0
    avg_out = total_out / len(sessions) if sessions else 0

    lines = [
        f"# 📊 Claude Code + MCP Report — period: **{period}**",
        f"_{len(sessions)} task(s) logged · generated "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "## Summary",
        f"- Tasks: **{len(sessions)}**",
        f"- Tokens: **{total_in:,}** in + **{total_out:,}** out  "
        f"(avg {avg_in:.0f} / {avg_out:.0f})",
        f"- Estimated cost: **${cost_usd:.4f}**",
        f"- First-try accuracy (no retry, no compile error): "
        f"**{accuracy_pct:.0f}%**",
        f"- Errors: compile={total_compile}, retries={total_retries}, "
        f"semantic={total_semantic}, mcp={total_mcp_err}",
        "",
        "## Outcomes",
    ]
    for outcome, n in sorted(by_outcome.items(), key=lambda x: -x[1]):
        emoji = {"success": "✅", "fixed-after-retry": "🔧",
                 "failed": "❌", "abandoned": "🚫"}.get(outcome, "•")
        lines.append(f"- {emoji} {outcome}: {n}")

    if top_tools:
        lines.extend(["", "## Top MCP Tools Used"])
        for t, n in top_tools:
            lines.append(f"- `{t}`: {n} call(s)")

    lines.extend(["", "## Recent Tasks"])
    lines.append("| When | Task | Tokens | Retries | Outcome |")
    lines.append("|------|------|--------|---------|---------|")
    for s_ in sessions[:20]:
        td = s_["td"]
        when = (s_["created_at"] or "")[:16].replace("T", " ")
        task = (td.get("task") or "?")[:50]
        toks = td.get("tokens_input", 0) + td.get("tokens_output", 0)
        retries = td.get("retries", 0)
        outcome = td.get("outcome", "?")
        lines.append(f"| {when} | {task} | {toks} | {retries} | {outcome} |")

    return ("\n".join(lines),
            f"claude_report period={period} sessions={len(sessions)} "
            f"tokens={total_in + total_out}")


# ─────────── Phase 0 Control Center handlers ───────────
#
# All four tools are sync (no AI) and idempotent where reasonable. They
# wrap existing primitives in core/memory/folder.py + store_v2 + agents
# registry — keeping logic thin so the security-critical pieces (scope
# enforcement, token hashing) live in one audited place.


def _walk_path_create(tree, path: str) -> dict:
    """Walk an absolute brain path '/A/B/C', creating any missing
    segments. Returns the leaf folder dict. Idempotent: a path that
    already exists in full is a no-op + returns the existing leaf."""
    p = path.strip()
    if not p.startswith("/"):
        raise ValueError("path must start with '/'")
    segments = [s for s in p.strip("/").split("/") if s]
    parent_id = None
    cur_path = ""
    leaf = None
    for seg in segments:
        cur_path = f"{cur_path}/{seg}"
        # Check if folder exists at this path
        cur = tree.conn.cursor()
        cur.execute("SELECT id, parent_id, name, path FROM folders WHERE path = ? LIMIT 1",
                    (cur_path,))
        row = cur.fetchone()
        if row:
            parent_id = row[0]
            leaf = {"id": row[0], "parent_id": row[1], "name": row[2], "path": row[3]}
            continue
        leaf = tree.create(seg, parent_id=parent_id)
        parent_id = leaf["id"]
    if leaf is None:
        raise ValueError(f"empty path: {path!r}")
    return leaf


def _handle_create_folder(arguments, store):
    from core.memory.folder import FolderTree
    path = (arguments or {}).get("path", "").strip()
    if not path:
        raise ValueError("path is required")
    tree = FolderTree(store().conn)
    leaf = _walk_path_create(tree, path)
    text = (
        f"# 📁 Folder ready\n\n"
        f"- path: `{leaf['path']}`\n"
        f"- id:   `{leaf['id']}`\n\n"
        f"Use this id with `brain_remember(folder=…)` to drop memories here."
    )
    return text, f"create_folder path={leaf['path']}"


def _handle_delete_folder(arguments, store):
    path = (arguments or {}).get("path", "").strip()
    cascade = bool((arguments or {}).get("cascade", False))
    if not path:
        raise ValueError("path is required")
    s = store()
    cur = s.conn.cursor()
    cur.execute("SELECT id FROM folders WHERE path = ?", (path,))
    row = cur.fetchone()
    if not row:
        return f"❌ Folder not found: `{path}`", f"delete_folder miss={path}"
    folder_id = row[0]
    # Refuse non-cascade delete on a non-empty folder so an agent can't
    # silently nuke 1K memories — operator must opt in explicitly.
    cur.execute("SELECT COUNT(*) FROM memories_v2 WHERE folder_id = ?", (folder_id,))
    n_mems = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM folders WHERE parent_id = ?", (folder_id,))
    n_subs = cur.fetchone()[0]
    if (n_mems > 0 or n_subs > 0) and not cascade:
        return (
            f"❌ Refusing to delete `{path}` — contains {n_mems} memories + "
            f"{n_subs} sub-folders. Pass `cascade=true` to delete recursively.",
            f"delete_folder blocked path={path} mems={n_mems} subs={n_subs}",
        )
    if cascade:
        # Delete memories first (FK ON DELETE not configured for memories→folders).
        cur.execute(
            "DELETE FROM memories_v2 WHERE folder_id IN ("
            "  WITH RECURSIVE descendants(fid) AS ("
            "    SELECT id FROM folders WHERE id = ? "
            "    UNION ALL "
            "    SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.fid"
            "  ) SELECT fid FROM descendants)",
            (folder_id,),
        )
        deleted_mems = cur.rowcount
        cur.execute(
            "DELETE FROM folders WHERE id IN ("
            "  WITH RECURSIVE descendants(fid) AS ("
            "    SELECT id FROM folders WHERE id = ? "
            "    UNION ALL "
            "    SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.fid"
            "  ) SELECT fid FROM descendants)",
            (folder_id,),
        )
        deleted_folders = cur.rowcount
        s.conn.commit()
        text = (
            f"# 🗑 Cascade delete complete\n\n"
            f"- removed: {deleted_folders} folders, {deleted_mems} memories\n"
            f"- root path: `{path}`\n\n"
            f"Disk files (if any of these were universal-index entries) "
            f"are untouched."
        )
        return text, f"delete_folder cascade path={path} folders={deleted_folders} mems={deleted_mems}"
    cur.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    s.conn.commit()
    return (f"# 🗑 Deleted empty folder\n\n- path: `{path}`",
            f"delete_folder empty path={path}")


def _handle_move_memory(arguments, store):
    memory_id = (arguments or {}).get("memory_id", "").strip()
    target_path = (arguments or {}).get("target_folder", "").strip()
    if not memory_id or not target_path:
        raise ValueError("memory_id and target_folder are required")
    s = store()
    cur = s.conn.cursor()
    cur.execute("SELECT id FROM folders WHERE path = ?", (target_path,))
    row = cur.fetchone()
    if not row:
        return (f"❌ Target folder not found: `{target_path}`. "
                f"Use brain_create_folder first.",
                f"move_memory miss target={target_path}")
    target_folder_id = row[0]
    s.update(memory_id, folder_id=target_folder_id)
    return (f"# 📦 Moved memory\n\n- memory_id: `{memory_id}`\n"
            f"- new folder: `{target_path}`",
            f"move_memory id={memory_id[:8]} → {target_path}")


def _handle_create_agent(arguments, store):
    """High-trust: provisions a new sub-agent + returns the plaintext
    token ONCE in the response. Caller (the operator, via the agent
    invoking this on their behalf) pastes the snippet into Claude
    Desktop config."""
    from core.memory.folder import FolderTree
    name = (arguments or {}).get("name", "").strip()
    template = (arguments or {}).get("template", "strict").strip()
    if not name:
        raise ValueError("name is required")
    s = store()
    # Ensure /Agents/<name> exists so the new agent has somewhere to write.
    tree = FolderTree(s.conn)
    scope_path = f"/Agents/{name}"
    _walk_path_create(tree, scope_path)
    # Provision the agent record + token.
    agent, plaintext = _agent_registry.create_agent(
        s.conn, name=name, scope_path=scope_path, template=template,
    )
    snippet = json.dumps({
        "mcpServers": {
            f"cosmos-{name.lower()}": {
                "command": "cosmos-mcp",
                "args": ["--agent-token", plaintext],
            }
        }
    }, indent=2)
    text = (
        f"# 🔐 Agent provisioned: {name}\n\n"
        f"- scope: `{scope_path}`\n"
        f"- template: `{template}`\n"
        f"- tools: {len(agent.tools_whitelist)} whitelisted\n\n"
        f"## One-time token (save now — not shown again)\n\n"
        f"```\n{plaintext}\n```\n\n"
        f"## Claude Desktop config snippet\n\n"
        f"```json\n{snippet}\n```\n\n"
        f"Paste into `~/Library/Application Support/Claude/"
        f"claude_desktop_config.json` then restart Claude Desktop."
    )
    return text, f"create_agent name={name} scope={scope_path}"


def _handle_link(arguments, store):
    """Create an EXPLICIT, user-intent relationship between two memories.

    Unlike the background relationship_builder (auto_temporal/folder/tag) and the
    semantic indexer, this is a DELIBERATE link the AI makes while working with
    the user. It uses a user-perspective relation_type (related/references/...)
    so it shows in the user graph, and requires a `why` to discourage spray.
    """
    args = arguments or {}
    source_id = (args.get("source_id") or "").strip()
    target_id = (args.get("target_id") or "").strip()
    why = (args.get("why") or "").strip()
    rtype = (args.get("relation_type") or "related").strip()
    try:
        weight = float(args.get("weight", 0.8))
    except (TypeError, ValueError):
        weight = 0.8
    weight = max(0.0, min(1.0, weight))

    if not source_id or not target_id:
        raise ValueError("brain_link needs both source_id and target_id "
                         "(search first to get the memory ids)")
    if source_id == target_id:
        raise ValueError("brain_link: source and target are the same memory")
    if not why:
        raise ValueError("brain_link needs a one-line `why` — link deliberately, not by spray")
    # User-perspective types only — never let an auto_*/semantic type slip in,
    # or this just re-creates the noise the user-view filter exists to hide.
    allowed = {"related", "references", "elaborates", "contradicts", "follows"}
    if rtype not in allowed:
        rtype = "related"

    s = store()

    def _peek(mid):
        return s.conn.execute(
            "SELECT substr(content, 1, 40) FROM memories_v2 WHERE id = ?", (mid,)
        ).fetchone()

    rs, rt = _peek(source_id), _peek(target_id)
    if rs is None or rt is None:
        missing = source_id if rs is None else target_id
        raise ValueError(f"brain_link: no memory with id {missing} "
                         "(search first to get a valid id)")

    # Persist the why as the edge `note` + stamp provenance so the node detail
    # panel can render it as a sentence ("↔ X — follows · 'why' · you directed")
    # and the graph can make user links stand out from inferred noise.
    s.add_relationship(source_id, target_id, rtype, weight,
                       note=why, origin="user_directed")
    text = (
        f"🔗 Linked [{rtype}]: \"{rs[0]}\"  ↔  \"{rt[0]}\"\n\n"
        f"why: {why}\n\n"
        f"This is a user-perspective edge — it shows in the graph "
        f"(weight {weight:.2f})."
    )
    return text, f"link {source_id}->{target_id} type={rtype}"


def _handle_update_memory(arguments, store):
    """Edit an existing memory IN PLACE — content / typed_data (e.g. title) /
    tags / category. Partial: only the fields passed change. Fills the gap where
    the brain MCP was add-only, so an agent can maintain its own files instead of
    creating duplicates. Same store path as REST PUT /api/v2/memory/{id}."""
    args = arguments or {}
    memory_id = (args.get("memory_id") or "").strip()
    if not memory_id:
        raise ValueError("brain_update_memory needs a memory_id (search first to get it)")

    s = store()
    row = s.conn.execute(
        "SELECT substr(content, 1, 40), typed_data FROM memories_v2 WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"brain_update_memory: no memory with id {memory_id} "
                         "(search first to get a valid id)")

    # Only the fields explicitly supplied change (partial update). store.update
    # serialises typed_data/tags + re-syncs FTS; we peek existence above so the
    # update can't silently no-op on a bad id (store.update returns True even on
    # 0 rows — known gap).
    fields = {}
    for k in ("content", "category", "typed_data", "tags"):
        if k in args and args[k] is not None:
            fields[k] = args[k]
    if not fields:
        raise ValueError("brain_update_memory: nothing to update — pass at least one of "
                         "content / typed_data / tags / category")

    # Guard: never let an agent silently blank the note to empty — store.update
    # would also wipe the summary + rebuild an empty FTS row.
    if "content" in fields and isinstance(fields["content"], str) and not fields["content"].strip():
        raise ValueError("brain_update_memory: refusing to blank content to empty — "
                         "pass real text or omit the `content` field")

    # MERGE typed_data, don't replace. typed_data is a mixed dict (title / slug /
    # agent / path / location / agenda …) read by dedup, code↔memory joins, and the
    # UI — a single-key write like {"title":"X.md"} must NOT wipe its siblings.
    # store.update does SET typed_data=? (full replace), so we merge in here.
    if "typed_data" in fields and isinstance(fields["typed_data"], dict):
        try:
            existing_td = json.loads(row[1]) if row[1] else {}
        except Exception:
            existing_td = {}
        if isinstance(existing_td, dict):
            fields["typed_data"] = {**existing_td, **fields["typed_data"]}

    ok = s.update(memory_id, **fields)
    if not ok:
        raise ValueError(f"brain_update_memory: update failed for {memory_id}")
    changed = ", ".join(sorted(fields.keys()))
    return (f"✏️  Updated memory {memory_id[:8]}… (changed: {changed})\n"
            f"   was: \"{row[0]}\"",
            f"update {memory_id} fields={changed}")


def _handle_delete_memory(arguments, store):
    """Delete a SINGLE memory by id (not a whole folder — that's
    brain_delete_folder). Same store path as REST DELETE /api/v2/memory/{id}."""
    args = arguments or {}
    memory_id = (args.get("memory_id") or "").strip()
    if not memory_id:
        raise ValueError("brain_delete_memory needs a memory_id (search first to get it)")

    s = store()
    row = s.conn.execute(
        "SELECT substr(content, 1, 40) FROM memories_v2 WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"brain_delete_memory: no memory with id {memory_id}")

    ok = s.delete(memory_id)
    if not ok:
        raise ValueError(f"brain_delete_memory: delete failed for {memory_id}")
    return (f"🗑️  Deleted memory {memory_id[:8]}… (\"{row[0]}\")",
            f"delete {memory_id}")


def _handle_brain_rebuild_links(arguments, store):
    """Re-derive memory↔memory edges so the Neural Map can show filaments."""
    from core.memory.relationship_builder import RelationshipBuilder

    s = store()
    builder = RelationshipBuilder(s.conn)
    stats = builder.build_all(
        clear_existing_auto=bool(arguments.get("clear_existing_auto", True)),
        tag_min_overlap=int(arguments.get("tag_min_overlap") or 2),
        folder_top_k=int(arguments.get("folder_top_k") or 6),
        temporal_window_hours=int(arguments.get("temporal_window_hours") or 24),
    )

    # Final unique-edge count (DB de-duplicates same-pair edges across types)
    cur = s.conn.cursor()
    cur.execute(
        "SELECT relation_type, COUNT(*) FROM relationships "
        "WHERE relation_type LIKE 'auto_%' GROUP BY relation_type"
    )
    by_type = dict(cur.fetchall())
    cur.execute(
        "SELECT COUNT(*) FROM relationships WHERE relation_type LIKE 'auto_%'"
    )
    total_in_db = cur.fetchone()[0]

    lines = [
        f"# 🔗 Rebuilt {total_in_db} graph edges across "
        f"{stats['memories']} memories",
        "",
        "## Generated this run",
        f"- 🏷️ Shared tags: {stats['auto_tag']}",
        f"- 📁 Same folder: {stats['auto_folder']}",
        f"- ⏱️ Temporal cluster: {stats['auto_temporal']}",
        f"- **Total proposed:** {stats['total']} (some pairs overlap → "
        f"deduped to {total_in_db} unique edges in DB)",
        "",
        "## Currently in DB by type",
    ]
    for rt in ("auto_tag", "auto_folder", "auto_temporal"):
        lines.append(f"- `{rt}`: {by_type.get(rt, 0)}")
    lines.append("")
    lines.append(
        "💡 The Neural Map will pick these up on its next refresh. "
        "If filaments still look sparse, try lowering `tag_min_overlap` to 1 "
        "or widening `temporal_window_hours`."
    )

    return ("\n".join(lines),
            f"brain_rebuild_links memories={stats['memories']} "
            f"unique_edges={total_in_db}")


async def run_mcp_stdio():
    server = create_mcp_server()
    # ── Bulletproof stdout isolation ─────────────────────────────────────
    # stdout IS the JSON-RPC wire. ONE stray print() from background indexing,
    # the file watcher, or any 3rd-party dependency corrupts a frame and the
    # client drops the entire connection ("request could not be submitted /
    # connection interrupted") — the recurring "MCP closed after reindex" class.
    # Instead of policing every call site, hand the transport the REAL stdout
    # and repoint the process's stdout at stderr, so nothing else can physically
    # reach the wire. Matches mcp.server.stdio's own default stream construction
    # (TextIOWrapper(sys.stdout.buffer, encoding="utf-8")).
    import anyio
    from io import TextIOWrapper
    _orig_stdout = sys.stdout
    _transport_stdout = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
    sys.stdout = sys.stderr
    try:
        async with stdio_server(stdout=_transport_stdout) as (rs, ws):
            await server.run(rs, ws, server.create_initialization_options())
    except Exception as e:
        # Distinguish a normal client disconnect (the AI tool closed the stdio
        # pipe / ended the session) from a real crash. Normal close → exit
        # quietly; anything else → log to STDERR (NEVER stdout — that's the
        # JSON-RPC channel) with a traceback, so a dropped connection is
        # diagnosable instead of a silent death the client only sees as "the
        # MCP server disconnected." (Previously run() had no guard at all.)
        # anyio wraps child-task exceptions in a BaseExceptionGroup (PEP 654),
        # so an abrupt client close that RACES an in-flight handler arrives as a
        # group around a BrokenResourceError/ClosedResourceError — unwrap before
        # classifying, else the benign disconnect logs a scary "crash" + re-raises
        # (defeating this very guard).
        def _is_clean_close(exc):
            if isinstance(exc, (BrokenPipeError, EOFError)) or type(exc).__name__ in (
                    "BrokenResourceError", "EndOfStream", "ClosedResourceError"):
                return True
            if isinstance(exc, BaseExceptionGroup):
                return bool(exc.exceptions) and all(_is_clean_close(x) for x in exc.exceptions)
            return False
        if _is_clean_close(e):
            print("ℹ️  Cosmos MCP: client closed the connection — exiting cleanly.",
                  file=sys.stderr)
            return
        import traceback
        print(f"❌ Cosmos MCP stdio loop crashed: {e}\n{traceback.format_exc()}",
              file=sys.stderr)
        raise
    finally:
        sys.stdout = _orig_stdout


def _bind_agent_from_cli_or_env():
    """Resolve the agent token from --agent-token CLI flag or the
    COSMOS_AGENT_TOKEN env var, then look up + bind the matching
    Agent record in the module-level _AGENT global. Fail fast on a
    bad token so the user sees a clear error instead of silently
    landing in unrestricted operator mode (which would defeat the
    Control Center entirely)."""
    global _AGENT
    import argparse as _argparse

    parser = _argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent-token", default=None,
                        help="Per-agent auth token (issued by AI Control Center). "
                             "Without this flag, the server runs unrestricted.")
    args, _unknown = parser.parse_known_args()
    token = (args.agent_token or os.environ.get("COSMOS_AGENT_TOKEN") or "").strip()
    if not token:
        print("ℹ️  No --agent-token supplied — running in operator mode "
              "(unrestricted; Control Center policies bypassed).",
              file=sys.stderr)
        return
    conn = get_store_v2().conn
    agent = _agent_registry.verify_token(conn, token)
    if not agent:
        # A token that was valid then ROTATED/REVOKED lands here on the next
        # respawn (recycle watchdog or a client stall→respawn), which looked
        # like "the agent connected then dropped forever." Keep refusing (no
        # policy = no run), but tell the user exactly how to recover instead of
        # a cryptic exit-loop.
        print("❌ Agent token is invalid or was revoked/rotated. Refusing to "
              "start without its policy.\n"
              "   → If you regenerated this agent's token in AI Control Center, "
              "update this AI tool's MCP config with the NEW token "
              "(or remove --agent-token to run in operator mode).",
              file=sys.stderr)
        sys.exit(2)
    _AGENT = agent
    print(f"🔐 Agent bound: {agent.name} (scope={agent.scope_path}, "
          f"template={agent.template}, tools={len(agent.tools_whitelist)})",
          file=sys.stderr)


def _handle_cosmos_design_audit(arguments, store):
    """Audit a codebase component or folder against DESIGN.md contract rules and tokens."""
    import os
    import json
    from core.code_indexer.errors import resolve_project_id
    from core.code_indexer.project_registry import get_project_registry
    from core.code_indexer.design_auditor import DesignAuditor

    path = (arguments.get("path") or "").strip()
    target_path = (arguments.get("target_path") or "").strip()
    if not path or not target_path:
        raise ValueError("path and target_path required")

    project_id = resolve_project_id(path)
    if not project_id:
        return (
            f"No watched project contains path: {path}\n"
            f"Please register it via code_reindex first.",
            f"cosmos_design_audit path={path} no_project",
        )

    proj = get_project_registry().get(project_id) or {}
    project_path = proj.get("path") or path

    # Resolve target path relative to project path if needed
    if not os.path.isabs(target_path):
        target_abs = os.path.abspath(os.path.join(project_path, target_path))
    else:
        target_abs = os.path.abspath(target_path)

    if not os.path.exists(target_abs):
        return (
            f"Target path does not exist: {target_path} (Resolved: {target_abs})",
            f"cosmos_design_audit error=path_not_found",
        )

    auditor = DesignAuditor(project_path)
    
    # Audit file or directory
    if os.path.isdir(target_abs):
        audit_res = auditor.audit_directory(target_abs)
        is_dir = True
    else:
        audit_res = auditor.audit_file(target_abs)
        is_dir = False

    if "error" in audit_res:
        return (f"Audit failed: {audit_res['error']}", f"cosmos_design_audit error={audit_res['error']}")

    # Formulate report in Markdown
    report = []
    report.append(f"# 🩺 Cosmos Visual Design System Audit Report\n")
    
    if is_dir:
        report.append(f"### 📊 Directory Audit Summary")
        report.append(f"- **Target Directory**: `{audit_res['directory']}`")
        report.append(f"- **Files Audited**: `{audit_res['files_audited']}`")
        report.append(f"- **Average Compliance Score**: `{audit_res['average_compliance_score']}%`")
        
        # Aggregate statistics
        agg_stats = {
            "hardcoded_colors": 0, "radius_violations": 0, "nested_cards": 0, "icon_drifts": 0, "bug_vulnerabilities": 0
        }
        for file_res in audit_res["details"]:
            for k in agg_stats:
                agg_stats[k] += file_res["stats"].get(k, 0)
        
        report.append("\n#### 🚨 Overall Drifts Found")
        report.append(f"- Hardcoded Colors: `{agg_stats['hardcoded_colors']}`")
        report.append(f"- Border Radius Violations (>8px): `{agg_stats['radius_violations']}`")
        report.append(f"- Forbidden Nested Cards: `{agg_stats['nested_cards']}`")
        report.append(f"- Icon Standard Drifts: `{agg_stats['icon_drifts']}`")
        report.append(f"- Preventable Stacking/Clipping Vulnerabilities: `{agg_stats['bug_vulnerabilities']}`\n")
        
        # Details per file with issues
        report.append("### 📁 Detailed File Audits")
        has_issues = False
        for file_res in audit_res["details"]:
            if file_res["issues"]:
                has_issues = True
                report.append(f"\n#### 📄 `{file_res['file']}` (Compliance: `{file_res['compliance_score']}%`)")
                for issue in file_res["issues"]:
                    sev = "❌ ERROR" if issue["severity"] == "error" else "⚠️ WARNING"
                    report.append(f"- **Line {issue['line']}** [{sev}]: {issue['message']}")
                    report.append(f"  ```tsx\n  {issue['snippet']}\n  ```")
        if not has_issues:
            report.append("`✅ All files conform perfectly to DESIGN.md and design.tokens.json rules!`")
            
    else:
        report.append(f"### 📊 File Audit Summary")
        report.append(f"- **Target File**: `{audit_res['file']}`")
        report.append(f"- **Compliance Score**: `{audit_res['compliance_score']}%`")
        
        stats = audit_res["stats"]
        report.append("\n#### 🚨 Drifts Found")
        report.append(f"- Hardcoded Colors: `{stats['hardcoded_colors']}`")
        report.append(f"- Border Radius Violations (>8px): `{stats['radius_violations']}`")
        report.append(f"- Forbidden Nested Cards: `{stats['nested_cards']}`")
        report.append(f"- Icon Standard Drifts: `{stats['icon_drifts']}`")
        report.append(f"- Preventable Stacking/Clipping Vulnerabilities: `{stats['bug_vulnerabilities']}`\n")
        
        report.append("### 🔎 Issue Breakdown")
        if audit_res["issues"]:
            for issue in audit_res["issues"]:
                sev = "❌ ERROR" if issue["severity"] == "error" else "⚠️ WARNING"
                report.append(f"\n#### 📍 Line {issue['line']} [{sev}]")
                report.append(f"- **Issue**: {issue['message']}")
                report.append(f"- **Snippet**:")
                report.append(f"  ```tsx\n  {issue['snippet']}\n  ```")
                
                # Add contextual remedies
                if issue["type"] == "hardcoded_color":
                    report.append("  *Remedy*: Replace raw hex/rgb with CSS var or use Tailwind's `text-slate-x`, `text-violet-300`, or HSL mapping.")
                elif issue["type"] == "radius_violation":
                    report.append("  *Remedy*: Limit rounded radius to max 8px (use `rounded-lg` or `rounded-xl`). Avoid 2xl or 3xl rounded borders.")
                elif issue["type"] == "nested_card":
                    report.append("  *Remedy*: Remove the outer borders of child container and use visual backgrounds like `bg-slate-900/40` or clean spacing partitions.")
                elif issue["type"] == "icon_drift":
                    report.append("  *Remedy*: Standardize using `@phosphor-icons/react` icon package imports.")
                elif issue["type"] == "bug_vulnerability":
                    report.append("  *Remedy*: Apply **React Portals** (`createPortal`) from `react-dom` to render overlays directly under `document.body` to bypass blurred-backdrop clipping bounds.")
        else:
            report.append("`✅ File conforms perfectly to DESIGN.md and design.tokens.json rules!`")

    # Add design system reference
    report.append("\n---")
    report.append("### 🌟 Cosmos Golden Standards Reminder")
    report.append("- *Layout Reference*: Check `src/components/Layout/Sidebar.tsx` (compact layout) and `src/components/Settings/IndexedProjectsPanel.tsx` (framer-motion top-tabs navigation).")
    report.append("- *Taste & Taste*: Keep UI dense and clean like a professional IDE productivity dashboard. Minimize glowing gradients and shadows.")
    
    body = "\n".join(report)
    return (body, f"cosmos_design_audit target={os.path.basename(target_path)} bytes={len(body)}")


def main():
    if not MCP_AVAILABLE:
        print("❌ MCP SDK not installed. Run: pip install -r requirements-mcp.txt",
              file=sys.stderr)
        print("    Note: requires Python 3.10+", file=sys.stderr)
        sys.exit(1)

    _bind_agent_from_cli_or_env()
    print("🧠 Cosmos MCP Server starting (stdio)...", file=sys.stderr)

    # Boot the file watcher so registered projects auto-update while Claude
    # Code is connected (Phase 7A). Best-effort — never block server start.
    try:
        from core.code_indexer.watcher_manager import get_watcher_manager
        wm = get_watcher_manager()
        wm.sync()
        watched = sum(1 for p in wm.status() if p.get("is_watching"))
        print(f"👁️  Watcher manager ready — {watched} project(s) being watched.",
              file=sys.stderr)
    except Exception as e:
        print(f"⚠️  Watcher manager failed to boot: {e}", file=sys.stderr)

    # Demo-blocker hardening (lesson 78c5b62a): arm the idle process-recycle
    # watchdog so a long-lived stdio server that has drifted into the SDK's
    # hang state gets respawned automatically during an idle gap. Best-effort.
    try:
        _start_recycle_watchdog()
    except Exception as e:
        print(f"⚠️  Recycle watchdog failed to arm: {e}", file=sys.stderr)

    asyncio.run(run_mcp_stdio())


if __name__ == "__main__":
    main()
