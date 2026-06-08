"""
Folder Relationships — "Galaxy Bridges" for Universe View
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Folders are clusters of related memories ("galaxies" in the UI).
Relationships between folders express that two galaxies share themes
even though they live in different parts of the hierarchy.

Three relation_type values:
  - "semantic"   — auto-computed via folder embeddings (Tier 1+ only)
  - "manual"     — user-defined link
  - "co_access"  — heuristic from access patterns (Phase 5+)

Tier 0 supports manual links + co_access heuristic.
Semantic computation needs Tier 1+ (deferred to FolderEmbedder when available).
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from typing import Optional


def _parse_tags(raw: Optional[str]) -> set[str]:
    """Tags column may be JSON array or comma-separated. Be tolerant."""
    if not raw:
        return set()
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return {str(t).strip() for t in json.loads(raw) if str(t).strip()}
        except json.JSONDecodeError:
            pass
    return {t.strip() for t in raw.split(",") if t.strip()}


class FolderRelationships:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ─────────── CRUD ───────────
    def upsert(
        self,
        source_id: str,
        target_id: str,
        weight: float,
        relation_type: str = "semantic",
        bidirectional: bool = True,
    ) -> int:
        """Insert or update a folder relationship. Returns rows affected."""
        if source_id == target_id:
            return 0
        weight = max(0.0, min(1.0, float(weight)))
        now = datetime.utcnow().isoformat()

        pairs = [(source_id, target_id)]
        if bidirectional:
            pairs.append((target_id, source_id))

        cur = self.conn.cursor()
        n = 0
        for s, t in pairs:
            cur.execute("""
                INSERT INTO folder_relationships
                    (source_folder_id, target_folder_id, relation_type, weight, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_folder_id, target_folder_id, relation_type) DO UPDATE SET
                    weight = excluded.weight,
                    updated_at = excluded.updated_at
            """, (s, t, relation_type, weight, now, now))
            n += cur.rowcount or 0
        self.conn.commit()
        return n

    def remove(self, source_id: str, target_id: str, relation_type: Optional[str] = None) -> int:
        cur = self.conn.cursor()
        if relation_type:
            cur.execute("""
                DELETE FROM folder_relationships
                WHERE (source_folder_id=? AND target_folder_id=?)
                   OR (source_folder_id=? AND target_folder_id=?)
                  AND relation_type=?
            """, (source_id, target_id, target_id, source_id, relation_type))
        else:
            cur.execute("""
                DELETE FROM folder_relationships
                WHERE (source_folder_id=? AND target_folder_id=?)
                   OR (source_folder_id=? AND target_folder_id=?)
            """, (source_id, target_id, target_id, source_id))
        self.conn.commit()
        return cur.rowcount or 0

    def list_for(
        self,
        folder_id: str,
        min_weight: float = 0.0,
        relation_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = """
            SELECT source_folder_id, target_folder_id, relation_type, weight, updated_at
            FROM folder_relationships
            WHERE source_folder_id = ?
              AND weight >= ?
        """
        params: list = [folder_id, min_weight]
        if relation_type:
            sql += " AND relation_type = ?"
            params.append(relation_type)
        sql += " ORDER BY weight DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "source_folder_id": r[0],
                "target_folder_id": r[1],
                "relation_type": r[2],
                "weight": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]

    def list_all(self, min_weight: float = 0.0, limit: int = 500) -> list[dict]:
        rows = self.conn.execute("""
            SELECT source_folder_id, target_folder_id, relation_type, weight, updated_at
            FROM folder_relationships
            WHERE weight >= ?
            ORDER BY weight DESC
            LIMIT ?
        """, (min_weight, limit)).fetchall()
        return [
            {
                "source_folder_id": r[0],
                "target_folder_id": r[1],
                "relation_type": r[2],
                "weight": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]

    def clear(self, relation_type: Optional[str] = None) -> int:
        cur = self.conn.cursor()
        if relation_type:
            cur.execute("DELETE FROM folder_relationships WHERE relation_type = ?", (relation_type,))
        else:
            cur.execute("DELETE FROM folder_relationships")
        self.conn.commit()
        return cur.rowcount or 0

    # ─────────── Heuristic computation (Tier 0 — no AI) ───────────
    def compute_co_occurrence(self, min_shared_tags: int = 2) -> int:
        """
        Compute folder relationships from shared tags between memories.
        Tier 0 heuristic: two folders are related if their memories share tags.

        Weight = (shared_tags / max_total_tags) clamped 0..1.
        Returns number of relationships created.
        """
        # Build folder → tag-set in one query
        # Use an INNER JOIN with folders to protect against dangling references in memories
        rows = self.conn.execute("""
            SELECT m.folder_id, m.tags
            FROM memories_v2 m
            INNER JOIN folders f ON m.folder_id = f.id
            WHERE m.folder_id IS NOT NULL
              AND m.tags IS NOT NULL
              AND m.tags != ''
        """).fetchall()

        folder_tags: dict[str, set[str]] = {}
        for folder_id, tags_raw in rows:
            tags = _parse_tags(tags_raw)
            if not tags:
                continue
            folder_tags.setdefault(folder_id, set()).update(tags)

        # Pairwise comparison (small N expected — folders are coarse)
        folder_ids = list(folder_tags.keys())
        n_created = 0
        for i, a in enumerate(folder_ids):
            for b in folder_ids[i + 1:]:
                shared = folder_tags[a] & folder_tags[b]
                if len(shared) < min_shared_tags:
                    continue
                total = max(len(folder_tags[a]), len(folder_tags[b]))
                weight = min(1.0, len(shared) / total) if total else 0.0
                if weight > 0:
                    self.upsert(a, b, weight, relation_type="co_access", bidirectional=True)
                    n_created += 1
        return n_created


def get_folder_relationships(conn: sqlite3.Connection) -> FolderRelationships:
    return FolderRelationships(conn)
