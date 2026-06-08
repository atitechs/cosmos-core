from __future__ import annotations
"""
Cosmos v5 — Memory Store v2 (SQLite Only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pure SQLite storage — NO LanceDB, NO AI dependencies.
FTS5 for BM25 search, JSON for typed_data.
RAM: ~50-100 MB | Startup: <1s
"""
import os
import re
import json
import uuid
import threading
from datetime import datetime

from core.memory.tags import extract_tags, tags_to_json, tags_to_fts, tags_from_json
from core.memory.summarizer import summarize
from core.memory.db_conn import retry_on_lock


# ═══════════════════════════════════════════════════════
# Thai tokenization (optional — graceful fallback)
# ═══════════════════════════════════════════════════════

_thai_available = True
try:
    from pythainlp import word_tokenize as _thai_tokenize
except ImportError:
    _thai_available = False

# Thai character detection — C-level regex scan. The old
# `any(ord(c) in _THAI_RANGE for c in text)` was a per-character Python loop;
# on the 50k-file indexer it walked ~450 MB of source bodies char-by-char.
_THAI_RE = re.compile("[฀-๿]")


def _has_thai(text: str) -> bool:
    """Check if text contains Thai characters."""
    return _THAI_RE.search(text) is not None


# pythainlp's newmm is dictionary-based word segmentation meant for short Thai
# PROSE (notes, queries). Running it on a multi-MB blob is pathological: a
# single Thai character in a comment/string of an 8 MB DefinitelyTyped .d.ts
# made newmm segment the entire file, and that one path was 99.8% of the 50k-
# file indexer's "linking" time (543s for 5k files). Above this size we skip
# segmentation and let FTS5's unicode61 tokenizer handle the raw text — Thai
# word-search inside a huge code blob is not a real use case, and every
# legitimate caller (notes, search queries, folder paths) is far smaller.
_THAI_TOKENIZE_MAX_CHARS = 100_000


def pre_tokenize(text: str) -> str:
    """
    Pre-tokenize text for FTS5 insertion.
    Thai text needs word segmentation; English passes through.
    """
    if not text:
        return ""

    # Cheap O(1) size guard BEFORE the Thai scan — keeps both the scan and
    # newmm off megabyte-scale code bodies.
    if (
        not _thai_available
        or len(text) > _THAI_TOKENIZE_MAX_CHARS
        or not _has_thai(text)
    ):
        return text

    # Tokenize Thai parts, keep English as-is
    tokens = _thai_tokenize(text, engine="newmm")
    return " ".join(t.strip() for t in tokens if t.strip())


# ═══════════════════════════════════════════════════════
# Memory Store v2
# ═══════════════════════════════════════════════════════

class MemoryStoreV2:
    """
    SQLite-only memory store for Cosmos Tier 0.
    Features: CRUD, FTS5 BM25 search, folder support, tags, auto-summary.
    """

    def __init__(self, db_path: str = "data/brain_v2"):
        # Path resolution defers to core.runtime_config so the priority
        # chain (COSMOS_BRAIN_DB > COSMOS_DATA_DIR > legacy default) is
        # observed everywhere, including inside the bundled .dmg where
        # sidecar_entry exports COSMOS_DATA_DIR to a user-writable path.
        # Without this, callers passing the default `data/brain_v2` arg
        # would get a brain DB inside the read-only .app bundle.
        from core.runtime_config import brain_db_path
        import os as _os
        explicit_brain_db = _os.environ.get("COSMOS_BRAIN_DB", "").strip()
        explicit_data_dir = _os.environ.get("COSMOS_DATA_DIR", "").strip()
        if explicit_brain_db or explicit_data_dir:
            resolved = brain_db_path()
            self.db_path = str(resolved)
            os.makedirs(resolved.parent, exist_ok=True)
        else:
            # Legacy code path — honor the constructor arg verbatim.
            os.makedirs(db_path, exist_ok=True)
            self.db_path = os.path.join(db_path, "brain.db")
        self.lock = threading.Lock()
        # All connection-tuning pragmas (WAL, busy_timeout=5000,
        # synchronous=NORMAL, temp_store=MEMORY, wal_autocheckpoint,
        # journal_size_limit, cache_size=-64 MB) live in
        # core.memory.db_conn.open_sqlite — drift away from this helper
        # silently re-introduces SQLITE_BUSY pop-ups under load. See
        # [[feedback-sqlite-locking-fix]] in auto-memory.
        from core.memory.db_conn import open_sqlite
        self.conn = open_sqlite(self.db_path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        """Create all v5 tables if they don't exist."""
        cursor = self.conn.cursor()

        # Main memories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories_v2 (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'note',
                typed_data TEXT,
                tags TEXT,
                folder_id TEXT,
                source TEXT,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                importance_score REAL DEFAULT 0.5,
                FOREIGN KEY (folder_id) REFERENCES folders(id)
            )
        """)

        # Folders hierarchy
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (parent_id) REFERENCES folders(id)
            )
        """)

        # Universal Index — disk-path metadata for /Universal/<label>
        # folders so the rescan / watcher flows know where to walk
        # without re-prompting the operator. One row per indexed root;
        # `watch_enabled` controls the watchdog observer (Phase 3
        # daemon mode). Idempotent migration — created if missing.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS universal_roots (
                folder_id      TEXT PRIMARY KEY,
                disk_path      TEXT NOT NULL,
                label          TEXT NOT NULL,
                last_scanned_at TIMESTAMP,
                watch_enabled  INTEGER DEFAULT 0,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
            )
        """)

        # Expression index on json_extract(typed_data, '$.path') so
        # universal-index dedup lookups stay O(log n) at 50K+ memories
        # instead of full-table scanning typed_data on every upsert.
        # Idempotent — sqlite skips if the index exists.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS memories_v2_universal_path
            ON memories_v2 (json_extract(typed_data, '$.path'))
            WHERE source = 'universal-index'
        """)

        # AI Control Center — Phase 0 foundation. One row per agent
        # (e.g. WikiGrapher, Coder, Researcher) so the MCP dispatcher
        # can verify a per-launch token, scope writes to the agent's
        # folder, and gate which tools the agent can call.
        #
        # token_hash:    SHA-256 of the plaintext token. Plaintext is
        #                returned ONCE on creation/regenerate; we
        #                never persist it. Constant-time compare on
        #                verify keeps timing-safe.
        # scope_path:    Folder prefix the agent may write under
        #                (e.g. "/Agents/WikiGrapher"). Reads outside
        #                the scope are still allowed; writes/creates
        #                outside are rejected by the dispatcher.
        # tools_whitelist: JSON array of MCP tool names the agent
        #                may invoke. Empty array = all-deny (safe
        #                default for newly-created agents until the
        #                operator picks tools in the Control Center).
        # template:      Strict | Standard | Trusted preset name —
        #                stored for UI display, NOT enforcement
        #                (enforcement reads tools_whitelist + scope).
        # revoked_at:    NULL = active; non-null = token rejected on
        #                verify even if hash matches.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL UNIQUE,
                scope_path    TEXT NOT NULL,
                token_hash    TEXT NOT NULL,
                tools_whitelist TEXT NOT NULL DEFAULT '[]',
                template      TEXT NOT NULL DEFAULT 'strict',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at  TIMESTAMP,
                revoked_at    TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS agents_token_hash
            ON agents (token_hash)
            WHERE revoked_at IS NULL
        """)

        # FTS5 virtual table for BM25 search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                id UNINDEXED,
                content,
                tags,
                folder_path,
                category UNINDEXED,
                tokenize = 'unicode61 remove_diacritics 2'
            )
        """)

        # Custom schemas storage
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS custom_schemas (
                name TEXT PRIMARY KEY,
                fields TEXT NOT NULL,
                aggregations TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Relationships (for graph view — carry over from v4)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT,
                target_id TEXT,
                relation_type TEXT,
                weight REAL,
                created_at TIMESTAMP,
                PRIMARY KEY (source_id, target_id)
            )
        """)

        # Folder-to-folder relationships ("galaxy bridges" for Universe view)
        # weight: 0..1 similarity score. relation_type: 'semantic' | 'manual' | 'co_access'
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folder_relationships (
                source_folder_id TEXT NOT NULL,
                target_folder_id TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'semantic',
                weight REAL NOT NULL DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_folder_id, target_folder_id, relation_type),
                FOREIGN KEY (source_folder_id) REFERENCES folders(id) ON DELETE CASCADE,
                FOREIGN KEY (target_folder_id) REFERENCES folders(id) ON DELETE CASCADE
            )
        """)
        # Code Index (Phase 5.2 Enhanced)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS code_index (
                id TEXT PRIMARY KEY,
                file_path TEXT,
                file_hash TEXT,
                symbol_name TEXT,
                symbol_type TEXT, -- function, class, variable, method
                scope TEXT,       -- parent class name or module
                content TEXT,     -- just signature/summary
                body TEXT,        -- full body
                docstring TEXT,
                language TEXT,
                start_line INTEGER,
                end_line INTEGER,
                metadata JSON,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS code_links (
                source_id TEXT,
                target_id TEXT,
                link_type TEXT, -- call, inherit, import
                PRIMARY KEY(source_id, target_id, link_type)
            )
        """)

        # Per-project error log — symptoms/fixes AI has seen so it can avoid
        # repeating them on future edits in the same project.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS code_errors (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                symptom TEXT NOT NULL,
                root_cause TEXT,
                fix TEXT,
                files_affected TEXT,
                tags TEXT,
                severity INTEGER DEFAULT 2,
                times_seen INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # FTS5 for code search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
                id UNINDEXED,
                symbol_name,
                file_path,
                content,
                docstring,
                tokenize = 'unicode61 remove_diacritics 2'
            )
        """)

        # Indexes for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_v2_category
            ON memories_v2(category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_v2_folder
            ON memories_v2(folder_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_v2_created
            ON memories_v2(created_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_folders_parent
            ON folders(parent_id)
        """)
        
        # Migration: code_errors.commit_hash (added 2026-05-03 for #5 staleness check)
        cursor.execute("PRAGMA table_info(code_errors)")
        _ce_cols = {row[1] for row in cursor.fetchall()}
        if "commit_hash" not in _ce_cols:
            cursor.execute("ALTER TABLE code_errors ADD COLUMN commit_hash TEXT")
        # Phase 5.13 — Lessons UI: pinned + disabled flags. Pinned lessons
        # surface first in find_relevant_code; disabled ones are excluded
        # from any AI-facing recall (the user keeps the row for posterity
        # but tells Cosmos not to feed it to the model). Soft-delete by
        # design — we never lose a captured lesson without an explicit user
        # delete.
        if "pinned" not in _ce_cols:
            cursor.execute("ALTER TABLE code_errors ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "disabled" not in _ce_cols:
            cursor.execute("ALTER TABLE code_errors ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        # Phase 5.14 — scope_globs: JSON array of glob patterns (e.g.
        # ["src/api/**", "tests/**"]) that say "always remind for paths
        # matching these". Smarter recall ranking in find_relevant_code
        # boosts lessons whose scope_globs match the path argument. Empty
        # array (the default) means standard ranking — no behavior change
        # for existing rows.
        if "scope_globs" not in _ce_cols:
            cursor.execute("ALTER TABLE code_errors ADD COLUMN scope_globs TEXT")

        # User-perspective linking: `note` (the human-readable "why" behind an
        # edge) + `origin` (who made it — 'user_directed' via brain_link,
        # 'manual' via the detail-panel + link, 'ai'/'auto' from the indexer).
        # Without `note` a relationship is an anonymous (A,B,type) row the user
        # can't read or tell apart from inferred noise — the exact gap that made
        # AI-created links feel like "the AI's view" instead of the user's. Both
        # additive + nullable: existing edges keep working, they just render
        # without a why-caption / provenance badge.
        cursor.execute("PRAGMA table_info(relationships)")
        _rel_cols = {row[1] for row in cursor.fetchall()}
        if "note" not in _rel_cols:
            cursor.execute("ALTER TABLE relationships ADD COLUMN note TEXT")
        if "origin" not in _rel_cols:
            cursor.execute("ALTER TABLE relationships ADD COLUMN origin TEXT")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_index_file ON code_index(file_path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_index_symbol ON code_index(symbol_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_index_type ON code_index(symbol_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_links_source ON code_links(source_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_links_target ON code_links(target_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_errors_project ON code_errors(project_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_errors_severity ON code_errors(severity)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_errors_last_seen ON code_errors(last_seen_at)")

        self.conn.commit()

    # ─── CRUD ─────────────────────────────────────────

    @retry_on_lock()
    def store(self, content: str, category: str = "note",
              typed_data: dict = None, tags: list = None,
              folder_id: str = None, source: str = "manual",
              importance: float = 0.5, memory_id: str = None,
              skip_summary: bool = False) -> str:
        """
        Store a new memory. Auto-generates ID, summary, and extracts tags.
        Returns the memory ID.

        skip_summary: pass True for bulk-import paths where TextRank
            summarization (~250 ms/event) is the dominant cost. Without
            this escape, ingesting 200 events from the benchmark harness
            blew 48 s on TextRank for almost no user value — short events
            don't need extractive summaries. Auto-skipped if content is
            under 300 chars or has fewer than 3 sentences regardless.
        """
        memory_id = memory_id or str(uuid.uuid4())

        # Auto-extract tags from content if none provided
        if tags is None:
            tags = extract_tags(content)

        # TextRank summarization is O(n²) over sentence count. Skip it for
        # short content (no signal to extract) and for explicit bulk paths.
        should_summarize = (not skip_summary
                            and len(content or "") >= 300
                            and content.count(".") + content.count("\n") >= 3)
        summary = summarize(content) if should_summarize else (content or "")[:200]

        now = datetime.now().isoformat()

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO memories_v2
                (id, content, category, typed_data, tags, folder_id, source,
                 summary, created_at, updated_at, importance_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                memory_id, content, category,
                json.dumps(typed_data or {}, ensure_ascii=False),
                tags_to_json(tags), folder_id, source,
                summary, now, now, importance
            ))

            # Sync FTS5
            folder_path = self._get_folder_path(folder_id) if folder_id else ""
            self._sync_fts_insert(
                cursor, memory_id, content, tags, folder_path, category
            )

            self.conn.commit()

        return memory_id

    def get(self, memory_id: str) -> dict | None:
        """Fetch a single memory with all data."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT m.id, m.content, m.category, m.typed_data, m.tags,
                       m.folder_id, m.source, m.summary, m.created_at,
                       m.updated_at, m.importance_score,
                       COALESCE(f.path, '') as folder_path
                FROM memories_v2 m
                LEFT JOIN folders f ON m.folder_id = f.id
                WHERE m.id = ?
            """, (memory_id,))
            row = cursor.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "content": row[1],
            "category": row[2],
            "typed_data": json.loads(row[3] or "{}"),
            "tags": tags_from_json(row[4]),
            "folder_id": row[5],
            "source": row[6],
            "summary": row[7],
            "created_at": row[8],
            "updated_at": row[9],
            "importance_score": row[10],
            "folder_path": row[11],
        }

    @retry_on_lock()
    def update(self, memory_id: str, **fields) -> bool:
        """
        Partial update of a memory.
        Supported fields: content, category, typed_data, tags, folder_id,
                          source, importance_score
        """
        allowed = {"content", "category", "typed_data", "tags", "folder_id",
                    "source", "importance_score"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        # Serialize complex types
        if "typed_data" in updates:
            updates["typed_data"] = json.dumps(updates["typed_data"], ensure_ascii=False)
        if "tags" in updates:
            updates["tags"] = tags_to_json(updates["tags"])

        updates["updated_at"] = datetime.now().isoformat()

        # Re-summarize if content changed
        if "content" in updates:
            updates["summary"] = summarize(updates["content"])

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [memory_id]

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"UPDATE memories_v2 SET {set_clause} WHERE id = ?",
                values
            )

            # Re-sync FTS5 if content or tags changed
            if "content" in fields or "tags" in fields:
                mem = None
                cursor.execute(
                    "SELECT content, tags, folder_id, category FROM memories_v2 WHERE id = ?",
                    (memory_id,)
                )
                mem = cursor.fetchone()
                if mem:
                    folder_path = self._get_folder_path(mem[2]) if mem[2] else ""
                    self._sync_fts_update(
                        cursor, memory_id, mem[0],
                        tags_from_json(mem[1]), folder_path, mem[3]
                    )

            self.conn.commit()

        return True

    @retry_on_lock()
    def delete(self, memory_id: str) -> bool:
        """Delete a memory and its FTS entry. Returns True if the memory existed."""
        with self.lock:
            cursor = self.conn.cursor()
            # Capture rowcount from the FIRST delete — relationships/FTS may legitimately
            # be empty for orphaned/short-lived memories.
            cursor.execute("DELETE FROM memories_v2 WHERE id = ?", (memory_id,))
            deleted = cursor.rowcount > 0
            cursor.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
            cursor.execute(
                "DELETE FROM relationships WHERE source_id = ? OR target_id = ?",
                (memory_id, memory_id)
            )
            self.conn.commit()
        return deleted

    def list_all(self, category: str = None, folder_id: str = None,
                 limit: int = 100, offset: int = 0,
                 recursive: bool = True) -> list:
        """List memories with optional category/folder filter.

        When `folder_id` is provided and `recursive` is True (the default),
        memories in any descendant folder are included too — clicking
        `/Code` should show everything under `/Code/Cosmos/Modules/...`,
        not just files pinned at the `/Code` node itself.
        """
        with self.lock:
            cursor = self.conn.cursor()
            where_parts = []
            params = []
            cte = ""

            if category:
                where_parts.append("m.category = ?")
                params.append(category)
            if folder_id:
                if recursive:
                    cte = """
                        WITH RECURSIVE folder_subtree(id) AS (
                            SELECT id FROM folders WHERE id = ?
                            UNION ALL
                            SELECT f.id FROM folders f
                            JOIN folder_subtree s ON f.parent_id = s.id
                        )
                    """
                    where_parts.append("m.folder_id IN (SELECT id FROM folder_subtree)")
                    params.insert(0, folder_id)
                else:
                    where_parts.append("m.folder_id = ?")
                    params.append(folder_id)

            where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

            cursor.execute(f"""
                {cte}
                SELECT m.id, m.content, m.category, m.typed_data, m.tags,
                       m.folder_id, m.source, m.summary, m.created_at,
                       m.updated_at, m.importance_score,
                       COALESCE(f.path, '') as folder_path
                FROM memories_v2 m
                LEFT JOIN folders f ON m.folder_id = f.id
                {where}
                ORDER BY m.created_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])

            return [
                {
                    "id": r[0], "content": r[1], "category": r[2],
                    "typed_data": json.loads(r[3] or "{}"),
                    "tags": tags_from_json(r[4]),
                    "folder_id": r[5],
                    "source": r[6] or "",
                    "summary": r[7],
                    "created_at": r[8],
                    "updated_at": r[9],
                    "importance_score": r[10],
                    "folder_path": r[11],
                }
                for r in cursor.fetchall()
            ]

    def count(self, category: str = None) -> int:
        """Count total memories, optionally filtered by category."""
        with self.lock:
            cursor = self.conn.cursor()
            if category:
                cursor.execute(
                    "SELECT COUNT(*) FROM memories_v2 WHERE category = ?",
                    (category,)
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM memories_v2")
            return cursor.fetchone()[0]

    # ─── FTS5 Sync ────────────────────────────────────

    def _sync_fts_insert(self, cursor, memory_id, content, tags, folder_path, category):
        """Insert a new entry into the FTS5 index."""
        tokenized_content = pre_tokenize(content)
        fts_tags = tags_to_fts(tags)
        tokenized_path = pre_tokenize(folder_path)

        cursor.execute("""
            INSERT INTO memories_fts (id, content, tags, folder_path, category)
            VALUES (?, ?, ?, ?, ?)
        """, (memory_id, tokenized_content, fts_tags, tokenized_path, category))

    def _sync_fts_update(self, cursor, memory_id, content, tags, folder_path, category):
        """Update an existing FTS5 entry (delete + re-insert)."""
        cursor.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
        self._sync_fts_insert(cursor, memory_id, content, tags, folder_path, category)

    def _get_folder_path(self, folder_id: str) -> str:
        """Get folder path from ID."""
        if not folder_id:
            return ""
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM folders WHERE id = ?", (folder_id,))
        row = cursor.fetchone()
        return row[0] if row else ""

    # ─── Relationships ────────────────────────────────

    @retry_on_lock()
    def add_relationship(self, source_id: str, target_id: str,
                         relation_type: str = "related", weight: float = 0.5,
                         note: str | None = None, origin: str | None = None):
        """Add a relationship between two memories.

        note   — optional human-readable "why" (the user's reason, or the AI's
                 articulation of the user's intent). Surfaced in the node detail
                 panel so a link reads as a sentence, not an anonymous
                 (A, B, type) row — the thing that made AI links feel like the
                 AI's view instead of the user's.
        origin — provenance: 'user_directed' (brain_link on user command),
                 'manual' (detail-panel + link), or None/'auto' (indexer). Lets
                 the graph + panel mark the user's own links apart from noise.

        Conflict policy — holds PER (source_id, target_id) ORDER. The PK is the
        ORDERED pair, so a reverse-direction write of the same conceptual pair
        does NOT conflict and lands as a SEPARATE row. (Known narrow edge-case,
        deferred: two opposite-direction user-type links for one pair would
        render as two lines + double a node's degree in the graph. Not data
        corruption — both rows are valid.) For a same-direction rewrite: a
        user-owned edge ('user_directed'/'manual') is STICKY — a later
        auto/indexer write (origin=None) won't clobber its type/note/origin. A
        user write always wins (newest intent). Auto-over-auto replaces freely.
        """
        with self.lock:
            self.conn.execute("""
                INSERT INTO relationships
                    (source_id, target_id, relation_type, weight, created_at, note, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    relation_type = excluded.relation_type,
                    weight        = excluded.weight,
                    created_at    = excluded.created_at,
                    note          = COALESCE(excluded.note, relationships.note),
                    origin        = COALESCE(excluded.origin, relationships.origin)
                WHERE excluded.origin IS NOT NULL
                   OR relationships.origin IS NULL
                   OR relationships.origin NOT IN ('user_directed', 'manual')
            """, (source_id, target_id, relation_type, weight,
                  datetime.now().isoformat(), note, origin))
            self.conn.commit()

    @retry_on_lock()
    def remove_relationship(self, source_id: str, target_id: str,
                            relation_type: str | None = None) -> int:
        """Remove a memory↔memory edge. If `relation_type` is given, only
        that type is deleted; otherwise every edge between the pair (in
        either direction) is removed. Returns rows deleted."""
        with self.lock:
            cursor = self.conn.cursor()
            if relation_type:
                cursor.execute("""
                    DELETE FROM relationships
                    WHERE relation_type = ?
                      AND ((source_id = ? AND target_id = ?)
                        OR (source_id = ? AND target_id = ?))
                """, (relation_type, source_id, target_id, target_id, source_id))
            else:
                cursor.execute("""
                    DELETE FROM relationships
                    WHERE (source_id = ? AND target_id = ?)
                       OR (source_id = ? AND target_id = ?)
                """, (source_id, target_id, target_id, source_id))
            self.conn.commit()
            return cursor.rowcount

    def get_relationships(self, memory_id: str) -> list:
        """Get all relationships for a memory."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT source_id, target_id, relation_type, weight
                FROM relationships
                WHERE source_id = ? OR target_id = ?
            """, (memory_id, memory_id))
            return [
                {"source_id": r[0], "target_id": r[1],
                 "relation_type": r[2], "weight": r[3]}
                for r in cursor.fetchall()
            ]

    # ─── Stats ────────────────────────────────────────

    def stats(self) -> dict:
        """Get overall store statistics.

        Counts EVERY memory (incl. source='universal-index' indexed files
        under /Universal) so this (/status, /app/info), the dashboard, and
        the sidebar all agree with what the Memory Browser actually shows
        — and so deleting a file/folder there visibly drops the number.
        """
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories_v2")
            total = cursor.fetchone()[0]

            cursor.execute(
                "SELECT category, COUNT(*) FROM memories_v2 GROUP BY category"
            )
            by_category = {r[0]: r[1] for r in cursor.fetchall()}

            cursor.execute("SELECT COUNT(*) FROM folders")
            folder_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM relationships")
            rel_count = cursor.fetchone()[0]

        return {
            "total_memories": total,
            "by_category": by_category,
            "total_folders": folder_count,
            "total_relationships": rel_count,
        }

    def close(self):
        """Close the database connection."""
        self.conn.close()


# ═══════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════

_store_v2 = None

def get_store_v2(db_path: str = "data/brain_v2") -> MemoryStoreV2:
    global _store_v2
    if _store_v2 is None:
        _store_v2 = MemoryStoreV2(db_path)
    return _store_v2
