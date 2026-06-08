"""
Bridge `code_links` (symbol-level imports/calls) into `relationships`
(memory-to-memory edges that the graph view consumes).

The indexer creates one `code_summary` memory per module. Links between
symbols live in `code_links`. This module aggregates those symbol links to
the module level and writes one undirected edge per (module_A, module_B)
pair so the graph shows real cross-module dependencies.

Usage:
    from core.code_indexer.relationship_sync import sync_module_relationships
    n = sync_module_relationships(conn)   # returns count of edges written

Idempotent — clears existing `code_dep` rows then writes fresh ones.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

RELATION_TYPE = "code_dep"


def path_to_module(file_path: str) -> str | None:
    """Path → module title heuristic, mirrors the indexer's grouping so the
    title matches what `_save_overview_to_memories` puts in code_summary
    `typed_data.title`."""
    if not file_path:
        return None
    parts = [p for p in file_path.split("/") if p and p != "."]
    if not parts:
        return None
    head = parts[0]
    if head == "src-tauri":
        return "Src Tauri"
    if head == "src":
        if len(parts) > 2 and parts[1] == "components":
            return parts[2]
        return "Src"
    if head == "core" and len(parts) > 1:
        return parts[1].replace("_", " ").title()
    return head.title()


def sync_module_relationships(conn: sqlite3.Connection) -> int:
    """Recompute module-level edges from `code_links`. Returns number of
    relationship rows written. Caller is responsible for committing."""
    cur = conn.cursor()

    cur.execute("""
        SELECT id, json_extract(typed_data,'$.title') AS module
        FROM memories_v2
        WHERE category = 'code_summary'
          AND json_extract(typed_data,'$.title') IS NOT NULL
    """)
    module_to_memory: dict[str, str] = {row[1]: row[0] for row in cur.fetchall()}
    if not module_to_memory:
        return 0

    cur.execute("SELECT id, file_path FROM code_index WHERE file_path IS NOT NULL")
    symbol_to_module: dict[str, str] = {}
    for sid, fp in cur.fetchall():
        mod = path_to_module(fp)
        if mod and mod in module_to_memory:
            symbol_to_module[sid] = mod

    cur.execute("SELECT source_id, target_id FROM code_links")
    pair_counts: Counter[tuple[str, str]] = Counter()
    for s_id, t_id in cur.fetchall():
        s_mod = symbol_to_module.get(s_id)
        t_mod = symbol_to_module.get(t_id)
        if not s_mod or not t_mod or s_mod == t_mod:
            continue
        a, b = sorted((s_mod, t_mod))
        pair_counts[(a, b)] += 1

    # Always wipe previous code_dep rows even if no pairs found — that way
    # removing all imports actually clears the graph instead of leaving stale.
    cur.execute("DELETE FROM relationships WHERE relation_type = ?", (RELATION_TYPE,))
    if not pair_counts:
        return 0

    max_count = max(pair_counts.values())
    now = datetime.now().isoformat()
    rows = []
    for (mod_a, mod_b), n in pair_counts.items():
        weight = round(0.3 + (n / max_count) * 0.65, 3)
        rows.append((module_to_memory[mod_a], module_to_memory[mod_b],
                     RELATION_TYPE, weight, now))
    cur.executemany("""
        INSERT OR REPLACE INTO relationships
        (source_id, target_id, relation_type, weight, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, rows)
    return len(rows)


def clear_module_relationships(conn: sqlite3.Connection) -> int:
    """Remove all code-derived relationships. Returns rows deleted."""
    cur = conn.cursor()
    cur.execute("DELETE FROM relationships WHERE relation_type = ?", (RELATION_TYPE,))
    return cur.rowcount
