"""
Cosmos v5 — BM25 Search (SQLite FTS5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fast full-text search with BM25 ranking.
Supports Thai + English, folder/tag/category filters.
Target: <50ms for 10K notes.
"""
import sqlite3
from core.memory.store_v2 import pre_tokenize
from core.memory.tags import tags_from_json


class BM25Search:
    """
    BM25 search engine using SQLite FTS5.
    Pre-tokenizes Thai text for accurate segmentation.
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def search(self, query: str, filters: dict = None,
               limit: int = 20) -> list:
        """
        Search memories using BM25 ranking.

        Args:
            query: Search query text
            filters: Optional dict with keys:
                - folder_id: str — filter by folder
                - folder_path: str — filter by folder path prefix
                - tags: list[str] — filter by tags (AND mode)
                - category: str — filter by category
                - date_from: str — ISO date (created_at >=)
                - date_to: str — ISO date (created_at <=)
            limit: Max results (default 20)

        Returns:
            List of dicts with id, content, category, tags, folder_path,
            snippet, rank_score, created_at
        """
        filters = filters or {}

        # Tokenize query for Thai support
        tokenized_query = pre_tokenize(query)
        if not tokenized_query.strip():
            return []

        # Build FTS5 MATCH query
        # Escape special FTS5 characters
        safe_query = self._escape_fts_query(tokenized_query)

        cursor = self.conn.cursor()

        # Main FTS5 search
        sql = """
            SELECT
                fts.id,
                fts.content,
                fts.category,
                fts.tags,
                fts.folder_path,
                rank,
                snippet(memories_fts, 1, '**', '**', '...', 32) as snippet
            FROM memories_fts fts
            WHERE memories_fts MATCH ?
        """
        params = [safe_query]

        # Apply folder filter
        if filters.get("folder_path"):
            sql += " AND fts.folder_path LIKE ?"
            params.append(f"{filters['folder_path']}%")

        # Apply category filter
        if filters.get("category"):
            sql += " AND fts.category = ?"
            params.append(filters["category"])

        # Apply tag filter (using FTS MATCH on tags column)
        if filters.get("tags"):
            for tag in filters["tags"]:
                sql += " AND fts.tags LIKE ?"
                params.append(f"%#{tag}%")

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(sql, params)
            fts_results = cursor.fetchall()
        except Exception:
            # If FTS query fails (bad syntax), try simpler match
            try:
                cursor.execute("""
                    SELECT id, content, category, tags, folder_path, rank,
                           snippet(memories_fts, 1, '**', '**', '...', 32)
                    FROM memories_fts
                    WHERE memories_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (f'"{tokenized_query}"', limit))
                fts_results = cursor.fetchall()
            except Exception:
                return []

        # Space-separated FTS5 terms match as implicit-AND (every term must
        # co-occur), which is precise but collapses to ~0 recall the moment a
        # single stray/typo'd term is added. When the strict AND finds nothing
        # and we have >1 term, widen to OR and let BM25 rank surface the best
        # partial match. Gated on an empty AND result so precise queries keep
        # their precise (high-precision) results untouched.
        match_tokens = safe_query.split()
        if not fts_results and len(match_tokens) > 1:
            try:
                cursor.execute(sql, [" OR ".join(match_tokens)] + params[1:])
                fts_results = cursor.fetchall()
            except Exception:
                pass

        if not fts_results:
            return []

        # Enrich with metadata from main table
        result_ids = [r[0] for r in fts_results]
        metadata = self._fetch_metadata(cursor, result_ids)

        results = []
        for row in fts_results:
            mem_id = row[0]
            meta = metadata.get(mem_id, {})

            # Apply date filters (post-filter since FTS5 can't do this)
            created = meta.get("created_at", "")
            if filters.get("date_from") and created < filters["date_from"]:
                continue
            if filters.get("date_to") and created > filters["date_to"]:
                continue

            # Apply folder_id filter
            if filters.get("folder_id") and meta.get("folder_id") != filters["folder_id"]:
                continue

            results.append({
                "id": mem_id,
                "content": row[1][:500],  # Truncate for listing
                "category": row[2],
                "tags": tags_from_json(row[3]) if row[3] else [],
                "folder_path": row[4] or "",
                "rank_score": abs(row[5]) if row[5] else 0,
                "snippet": row[6] or "",
                "created_at": meta.get("created_at", ""),
                "typed_data": meta.get("typed_data", {}),
                "summary": meta.get("summary", ""),
                "folder_id": meta.get("folder_id"),
            })

        return results

    def search_simple(self, query: str, limit: int = 10) -> list:
        """
        Simplified search — just returns matching IDs and scores.
        Faster for internal use (e.g., aggregation pre-filter).
        """
        tokenized = pre_tokenize(query)
        if not tokenized.strip():
            return []

        safe_query = self._escape_fts_query(tokenized)
        cursor = self.conn.cursor()

        try:
            cursor.execute("""
                SELECT id, rank FROM memories_fts
                WHERE memories_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (safe_query, limit))
            return [{"id": r[0], "rank_score": abs(r[1])} for r in cursor.fetchall()]
        except Exception:
            return []

    def _fetch_metadata(self, cursor, ids: list) -> dict:
        """Fetch metadata from memories_v2 for given IDs."""
        if not ids:
            return {}
        import json
        placeholders = ",".join("?" * len(ids))
        cursor.execute(f"""
            SELECT id, created_at, folder_id, typed_data, summary
            FROM memories_v2
            WHERE id IN ({placeholders})
        """, ids)
        result = {}
        for r in cursor.fetchall():
            try:
                td = json.loads(r[3] or "{}")
            except Exception:
                td = {}
            result[r[0]] = {
                "created_at": r[1],
                "folder_id": r[2],
                "typed_data": td,
                "summary": r[4],
            }
        return result

    def _escape_fts_query(self, query: str) -> str:
        """
        Escape special FTS5 characters to prevent syntax errors.
        Wraps tokens in double quotes if they contain special chars.
        """
        # FTS5 special chars: AND OR NOT ( ) * : ^
        special = set('(){}[]"*:^')
        tokens = query.split()
        safe_tokens = []
        for token in tokens:
            if any(c in special for c in token):
                # Wrap in quotes, escaping internal quotes
                token = '"' + token.replace('"', '""') + '"'
            safe_tokens.append(token)
        return " ".join(safe_tokens)
