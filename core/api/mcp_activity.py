"""
MCP Activity Log
━━━━━━━━━━━━━━━━
Append-only log of every MCP tool invocation.
Format: JSON Lines — one event per line, easy to tail/parse.

Each event records:
  - timestamp (ISO 8601)
  - tool name
  - arguments (truncated to keep file size manageable)
  - status (ok / denied / error)
  - duration_ms
  - result_summary (short string preview)
  - client (best-effort identifier)
"""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime
from typing import Optional


class ActivityLog:
    MAX_ARG_LEN = 500
    MAX_RESULT_LEN = 300

    def __init__(self, log_path: str = "data/brain_v2/mcp_activity.jsonl"):
        # Honor COSMOS_DATA_DIR / COSMOS_ACTIVITY_LOG so benchmark
        # sandboxes don't pollute the user's real activity log (which
        # the Outcome dashboard reads). See core/runtime_config.py.
        import os as _os
        if (_os.environ.get("COSMOS_DATA_DIR", "").strip()
                or _os.environ.get("COSMOS_ACTIVITY_LOG", "").strip()):
            from core.runtime_config import activity_log_path
            self.log_path = str(activity_log_path())
        else:
            self.log_path = log_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def record(
        self,
        tool: str,
        arguments: dict,
        status: str = "ok",
        duration_ms: float = 0.0,
        result_summary: str = "",
        client: str = "mcp",
        error: Optional[str] = None,
    ):
        event = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "tool": tool,
            "args": self._truncate_args(arguments),
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "result_summary": result_summary[: self.MAX_RESULT_LEN],
            "client": client,
        }
        if error:
            event["error"] = error[:300]

        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)

    def _truncate_args(self, args: dict) -> dict:
        """Keep args readable but bounded."""
        out = {}
        for k, v in (args or {}).items():
            if isinstance(v, str) and len(v) > self.MAX_ARG_LEN:
                out[k] = v[: self.MAX_ARG_LEN] + "…"
            elif isinstance(v, (dict, list)):
                s = json.dumps(v, ensure_ascii=False)
                out[k] = s if len(s) < self.MAX_ARG_LEN else s[: self.MAX_ARG_LEN] + "…"
            else:
                out[k] = v
        return out

    def tail(self, n: int = 50) -> list[dict]:
        """Return the last n events (newest first)."""
        if not os.path.exists(self.log_path):
            return []
        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        events = []
        for line in lines[-n:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        events.reverse()
        return events

    def clear(self):
        with self._lock:
            if os.path.exists(self.log_path):
                os.remove(self.log_path)


class Timer:
    """Context manager for measuring tool duration."""
    def __enter__(self):
        self._t0 = time.perf_counter()
        return self
    def __exit__(self, *args):
        self.duration_ms = (time.perf_counter() - self._t0) * 1000


_log: ActivityLog | None = None
def get_activity_log() -> ActivityLog:
    global _log
    if _log is None:
        _log = ActivityLog()
    return _log
