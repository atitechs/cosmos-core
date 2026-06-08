"""
Relationship Builder — Auto-link memories so the Neural Map has edges.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Without this, the graph view is just orphan stars on black space — the
weakness highlighted when comparing Cosmos to Obsidian's filament-rich
visualisation.

Three Tier-0 strategies (no AI required):

1. **Shared tags**     — pairs sharing ≥ N tags get an edge weighted by overlap.
2. **Same folder**     — each memory links to its top-K folder peers by recency.
3. **Temporal cluster**— memories created within a short window (same day) get
                          a soft edge so bursts of activity stay grouped.

All edges are stored with `relation_type` prefixed `auto_*` so a future
manual-link feature can leave them alone (or vice-versa: rebuilding auto
edges should never destroy user-added ones).

Idempotent: calling `build_all()` repeatedly converges on the same graph.
"""
from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple, Optional


AUTO_RELATION_TYPES = ("auto_tag", "auto_folder", "auto_temporal")


def _parse_tags(raw) -> Set[str]:
    if not raw:
        return set()
    if isinstance(raw, list):
        return {str(t).strip().lower() for t in raw if t}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, list):
            return {str(t).strip().lower() for t in parsed if t}
    except Exception:
        pass
    return set()


class RelationshipBuilder:
    """Build memory↔memory edges from observable signals."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── Public API ─────────────────────────────────────
    def build_all(
        self,
        clear_existing_auto: bool = True,
        tag_min_overlap: int = 2,
        folder_top_k: int = 6,
        temporal_window_hours: int = 24,
    ) -> Dict[str, int]:
        """Run all three strategies. Returns counts of edges added per type."""
        if clear_existing_auto:
            self.clear_auto()

        memories = self._load_memories()
        if len(memories) < 2:
            return {"auto_tag": 0, "auto_folder": 0, "auto_temporal": 0,
                    "total": 0, "memories": len(memories)}

        tag_edges = self._build_shared_tags(memories, min_overlap=tag_min_overlap)
        folder_edges = self._build_same_folder(memories, top_k=folder_top_k)
        temporal_edges = self._build_temporal(
            memories, window=timedelta(hours=temporal_window_hours)
        )

        self._upsert_many(tag_edges, "auto_tag")
        self._upsert_many(folder_edges, "auto_folder")
        self._upsert_many(temporal_edges, "auto_temporal")

        return {
            "auto_tag":      len(tag_edges),
            "auto_folder":   len(folder_edges),
            "auto_temporal": len(temporal_edges),
            "total":         len(tag_edges) + len(folder_edges) + len(temporal_edges),
            "memories":      len(memories),
        }

    def clear_auto(self) -> int:
        """Remove only auto-built edges; manual `related` etc. survive."""
        with self.conn:
            cur = self.conn.cursor()
            qmarks = ",".join("?" * len(AUTO_RELATION_TYPES))
            cur.execute(
                f"DELETE FROM relationships WHERE relation_type IN ({qmarks})",
                AUTO_RELATION_TYPES,
            )
            return cur.rowcount

    # ── Strategies ────────────────────────────────────
    def _build_shared_tags(
        self, memories: List[dict], min_overlap: int
    ) -> List[Tuple[str, str, float]]:
        """Inverted index on tags → fast pairwise scoring."""
        tag_to_mems: Dict[str, List[str]] = defaultdict(list)
        for m in memories:
            for tag in m["tags_set"]:
                tag_to_mems[tag].append(m["id"])

        # Skip mega-tags that would explode pair count (e.g., "trade" with 64)
        mega_threshold = 30
        useful_tags = {t: ids for t, ids in tag_to_mems.items()
                       if 2 <= len(ids) <= mega_threshold}

        pair_overlap: Dict[Tuple[str, str], int] = defaultdict(int)
        for tag, ids in useful_tags.items():
            ids_sorted = sorted(ids)
            for i in range(len(ids_sorted)):
                for j in range(i + 1, len(ids_sorted)):
                    pair_overlap[(ids_sorted[i], ids_sorted[j])] += 1

        edges = [
            (a, b, min(1.0, 0.3 + 0.2 * overlap))
            for (a, b), overlap in pair_overlap.items()
            if overlap >= min_overlap
        ]
        return edges

    def _build_same_folder(
        self, memories: List[dict], top_k: int
    ) -> List[Tuple[str, str, float]]:
        """Each memory connects to its top-K folder peers, ranked by recency.

        Top-K rather than full clique avoids visual hairballs when one folder
        holds 60+ items."""
        by_folder: Dict[str, List[dict]] = defaultdict(list)
        for m in memories:
            if m["folder_id"]:
                by_folder[m["folder_id"]].append(m)

        edges: Dict[Tuple[str, str], float] = {}
        for fid, mems in by_folder.items():
            if len(mems) < 2:
                continue
            mems_sorted = sorted(mems, key=lambda m: m["created_at"] or "",
                                 reverse=True)
            for i, m in enumerate(mems_sorted):
                # connect to top_k recent peers, skipping self
                peers = (mems_sorted[max(0, i - top_k):i]
                         + mems_sorted[i + 1:i + 1 + top_k])
                for peer in peers:
                    a, b = sorted([m["id"], peer["id"]])
                    if (a, b) not in edges:
                        edges[(a, b)] = 0.4

        return [(a, b, w) for (a, b), w in edges.items()]

    def _build_temporal(
        self, memories: List[dict], window: timedelta
    ) -> List[Tuple[str, str, float]]:
        """Memories created within `window` get a soft temporal edge.

        Uses sliding window over a sorted list — O(n·k) for small k bursts."""
        with_time = [m for m in memories if m["created_dt"]]
        with_time.sort(key=lambda m: m["created_dt"])

        edges: Dict[Tuple[str, str], float] = {}
        max_pairs_per_memory = 5  # cap, otherwise dense bursts dominate

        for i, m in enumerate(with_time):
            paired = 0
            for j in range(i + 1, len(with_time)):
                other = with_time[j]
                if other["created_dt"] - m["created_dt"] > window:
                    break
                a, b = sorted([m["id"], other["id"]])
                if (a, b) not in edges:
                    edges[(a, b)] = 0.25
                paired += 1
                if paired >= max_pairs_per_memory:
                    break

        return [(a, b, w) for (a, b), w in edges.items()]

    # ── Storage helpers ──────────────────────────────
    def _upsert_many(
        self, edges: List[Tuple[str, str, float]], relation_type: str
    ) -> None:
        if not edges:
            return
        now = datetime.now().isoformat()
        with self.conn:
            self.conn.executemany(
                """INSERT OR REPLACE INTO relationships
                   (source_id, target_id, relation_type, weight, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [(a, b, relation_type, w, now) for (a, b, w) in edges],
            )

    def _load_memories(self) -> List[dict]:
        # Exclude universal-index memories from auto-edge building.
        #
        # Without this filter, watching a 5K-file code repo creates
        # massive noise:
        #   • auto_folder — 200 React components in one folder × top_k=6
        #     peers each = 1.2K edges that say nothing more than "these
        #     files are siblings on disk".
        #   • auto_temporal — all files share an index timestamp (one
        #     scan), 24h window catches every pair, capped at 5/mem ×
        #     5K mems = 25K temporal edges.
        #   • auto_tag — every universal-index memory carries the
        #     'universal-index' tag plus a classification + extension;
        #     mega_threshold=30 already drops the source tag, but
        #     ext-tag clusters (200 .tsx files all sharing the 'tsx'
        #     tag) still pile up.
        #
        # Folder-level galaxy bridges (folder_relationships table) are
        # untouched — universal folders still connect to user folders
        # via shared tag co-occurrence, which is what the user actually
        # wants. Memory-level edges become opt-in via a future
        # "Build semantic links" per-folder action.
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, tags, folder_id, created_at
            FROM memories_v2
            WHERE id IS NOT NULL
              AND (source IS NULL OR source != 'universal-index')
        """)
        out = []
        for mid, tags_raw, folder_id, created_at in cur.fetchall():
            dt = None
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", ""))
                except Exception:
                    pass
            out.append({
                "id": mid,
                "tags_set": _parse_tags(tags_raw),
                "folder_id": folder_id,
                "created_at": created_at,
                "created_dt": dt,
            })
        return out


def get_relationship_builder(conn: sqlite3.Connection) -> RelationshipBuilder:
    return RelationshipBuilder(conn)
