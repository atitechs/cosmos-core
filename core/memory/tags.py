"""
Cosmos v5 — Tag System
━━━━━━━━━━━━━━━━━━━━━━━
Extract, store, and query #hashtags.
No AI required.
"""
import re
import json
import sqlite3

# Regex: match #hashtags (supports Thai, English, numbers, underscores)
TAG_PATTERN = re.compile(r'#([\w\u0E00-\u0E7F]+)', re.UNICODE)


def extract_tags(text: str) -> list:
    """Extract #hashtags from text using regex."""
    if not text:
        return []
    return list(set(TAG_PATTERN.findall(text)))


def tags_to_json(tags: list) -> str:
    """Convert tags list to JSON string for storage."""
    return json.dumps(tags, ensure_ascii=False) if tags else "[]"


def tags_from_json(tags_json: str) -> list:
    """Parse tags from JSON string."""
    if not tags_json:
        return []
    try:
        return json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        return []


def tags_to_fts(tags: list) -> str:
    """Convert tags list to space-separated string for FTS5 indexing."""
    return " ".join(f"#{t}" for t in tags) if tags else ""


class TagManager:
    """Manages tag operations on the v2 store."""

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def add_tags(self, memory_id: str, tags: list):
        """Add tags to a memory (merges with existing)."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT tags FROM memories_v2 WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        if not row:
            return

        existing = tags_from_json(row[0])
        merged = list(set(existing + tags))
        cursor.execute(
            "UPDATE memories_v2 SET tags = ? WHERE id = ?",
            (tags_to_json(merged), memory_id)
        )
        self.conn.commit()

    def remove_tag(self, memory_id: str, tag: str):
        """Remove a specific tag from a memory."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT tags FROM memories_v2 WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        if not row:
            return

        existing = tags_from_json(row[0])
        if tag in existing:
            existing.remove(tag)
            cursor.execute(
                "UPDATE memories_v2 SET tags = ? WHERE id = ?",
                (tags_to_json(existing), memory_id)
            )
            self.conn.commit()

    def get_popular_tags(self, category: str = None, limit: int = 20) -> list:
        """
        Get most popular tags across all memories.
        Returns: [(tag, count), ...]
        """
        cursor = self.conn.cursor()
        if category:
            cursor.execute(
                "SELECT tags FROM memories_v2 WHERE category = ? AND tags IS NOT NULL",
                (category,)
            )
        else:
            cursor.execute("SELECT tags FROM memories_v2 WHERE tags IS NOT NULL")

        # Count tag occurrences
        tag_counts = {}
        for (tags_json,) in cursor.fetchall():
            for tag in tags_from_json(tags_json):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        # Sort by count, return top N
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_tags[:limit]

    def search_by_tags(self, tags: list, mode: str = "AND") -> list:
        """
        Find memories matching given tags.
        mode='AND': must have ALL tags
        mode='OR':  must have ANY tag
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id, content, category, tags, folder_id FROM memories_v2 WHERE tags IS NOT NULL"
        )

        results = []
        tag_set = set(tags)

        for row in cursor.fetchall():
            mem_tags = set(tags_from_json(row[3]))
            if mode == "AND" and tag_set.issubset(mem_tags):
                results.append({
                    "id": row[0], "content": row[1],
                    "category": row[2], "tags": list(mem_tags),
                    "folder_id": row[4],
                })
            elif mode == "OR" and tag_set.intersection(mem_tags):
                results.append({
                    "id": row[0], "content": row[1],
                    "category": row[2], "tags": list(mem_tags),
                    "folder_id": row[4],
                })

        return results
