"""Centralized SQLite connection helper for Cosmos.

Every `sqlite3.connect(...)` in the production code path should go
through `open_sqlite()` so the four locking-related PRAGMAs are applied
uniformly. Drifting away from this — opening connections bare — silently
re-introduces the "database is locked" UX failures we saw during heavy
multi-process load (sidecar + MCP server + file watcher + indexer +
background jobs all writing concurrently).

Why these specific PRAGMAs:

  journal_mode=WAL
    Persistent on the DB file — readers don't block writers, writers
    don't block readers. SET ONCE, every later connection inherits it.

  busy_timeout=5000
    Per-connection — SQLite spins for up to 5 s on SQLITE_BUSY before
    surfacing OperationalError. Without it, a 50 ms write contention
    burst returns "database is locked" instantly and the caller sees
    an error pop-up.

  synchronous=NORMAL
    Per-connection — drops one fsync per commit relative to FULL.
    Writes 2-3× faster; the tradeoff is "last 1 sec of writes may be
    lost on power-off" (no corruption risk). Acceptable for a memory
    app; not acceptable for financial ledgers.

  temp_store=MEMORY
    Per-connection — temp tables / index sorts use RAM not disk.
    Eliminates a class of write contention on the temp file.

Plus the `retry_on_lock` decorator for application-level resilience:
even with busy_timeout=5000, sustained contention can still surface
SQLITE_BUSY. The decorator retries with exponential backoff (100 ms,
200 ms, 400 ms, 800 ms, 1.6 s) so callers get transparent recovery.

Diagnosed during the demo lockdown audit (see [[feedback-sqlite-locking-fix]]
in auto-memory) — orphaned SIGSTOP'd MCP children plus the multi-writer
concurrency model meant any bare connection could see locks under load.
"""
from __future__ import annotations

import sqlite3
import time
from functools import wraps
from typing import Any, Callable, TypeVar


F = TypeVar("F", bound=Callable[..., Any])


def open_sqlite(
    path: str,
    *,
    check_same_thread: bool = False,
    timeout: float = 5.0,
    cache_size_kb: int = 64_000,
) -> sqlite3.Connection:
    """Open a SQLite connection with the Cosmos-standard pragmas.

    `path` — absolute path to the .db file (caller resolves location).
    `check_same_thread` — Cosmos shares connections across worker
       threads via a `threading.Lock()`, so default to False.
    `timeout` — sqlite3.connect's own block-wait before raising
       OperationalError. Belt-and-suspenders with busy_timeout.
    `cache_size_kb` — page cache size in KB (negative number =
       kilobytes, positive = pages). 64 MB default — typical Cosmos
       brain is 50–200 MB so this fits the working set in RAM.
    """
    conn = sqlite3.connect(
        path,
        timeout=timeout,
        check_same_thread=check_same_thread,
    )
    # Order matters: WAL must be set before the others so subsequent
    # pragmas apply against the new journal mode.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA cache_size=-{cache_size_kb}")
    # Hygiene: cap the -wal file. Without this a long-running writer can
    # push the WAL past 100 MB which blows read latency (every SELECT
    # has to replay the whole WAL).
    conn.execute("PRAGMA wal_autocheckpoint=1000")          # ~4 MB cap
    conn.execute("PRAGMA journal_size_limit=4194304")       # 4 MB hard limit
    return conn


def retry_on_lock(max_retries: int = 5, base_delay: float = 0.1) -> Callable[[F], F]:
    """Retry a function on `sqlite3.OperationalError: database is locked`.

    Exponential backoff: base_delay, 2x, 4x, 8x, 16x. With defaults that's
    100 ms / 200 ms / 400 ms / 800 ms / 1.6 s = up to 3.1 s total wait
    before giving up. Combined with the connection's busy_timeout=5000,
    callers get effectively 5 s + 3.1 s = ~8 s of patience before a
    user-visible error.

    Only retries on "locked"/"busy"; other OperationalErrors (schema
    mismatches, syntax errors) re-raise immediately.

    Usage:
        @retry_on_lock()
        def write_memory(memory: dict) -> str:
            ...
    """
    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries):
                try:
                    return fn(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if ("locked" in msg or "busy" in msg) and attempt < max_retries - 1:
                        time.sleep(base_delay * (2 ** attempt))
                        continue
                    raise
            # Unreachable — the loop either returns or raises.
            raise RuntimeError("retry_on_lock: exhausted without return")
        return wrapper  # type: ignore[return-value]
    return decorator
