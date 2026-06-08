from __future__ import annotations
"""
Cosmos v5 — Folder Hierarchy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Obsidian-like folder tree for organizing memories.
Unlimited nesting depth, path caching.
"""
import uuid
import sqlite3
from datetime import datetime


class FolderTree:
    """
    Manages a folder hierarchy stored in SQLite.
    Each folder has: id, parent_id, name, path (computed).
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    # ─── CRUD ─────────────────────────────────────────

    def create(self, name: str, parent_id: str = None) -> dict:
        """
        Create a new folder. Returns the folder dict.
        """
        folder_id = str(uuid.uuid4())

        # Compute path
        if parent_id:
            parent_path = self._get_path_by_id(parent_id)
            if parent_path is None:
                raise ValueError(f"Parent folder '{parent_id}' not found")
            path = f"{parent_path}/{name}"
        else:
            path = f"/{name}"

        # Check duplicate name under same parent
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND name = ?",
            (parent_id, name)
        )
        if cursor.fetchone():
            raise ValueError(f"Folder '{name}' already exists under this parent")

        cursor.execute("""
            INSERT INTO folders (id, parent_id, name, path, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (folder_id, parent_id, name, path, datetime.now().isoformat()))
        self.conn.commit()

        return {"id": folder_id, "parent_id": parent_id, "name": name, "path": path}

    def rename(self, folder_id: str, new_name: str) -> bool:
        """Rename a folder and update all descendant paths."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT parent_id, path FROM folders WHERE id = ?", (folder_id,))
        row = cursor.fetchone()
        if not row:
            return False

        parent_id, old_path = row

        # Check duplicate name under same parent
        cursor.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND name = ? AND id != ?",
            (parent_id, new_name, folder_id)
        )
        if cursor.fetchone():
            raise ValueError(f"Folder '{new_name}' already exists under this parent")

        # Compute new path
        if parent_id:
            parent_path = self._get_path_by_id(parent_id)
            new_path = f"{parent_path}/{new_name}"
        else:
            new_path = f"/{new_name}"

        # Update this folder
        cursor.execute(
            "UPDATE folders SET name = ?, path = ? WHERE id = ?",
            (new_name, new_path, folder_id)
        )

        # Update all descendant paths
        self._update_descendant_paths(cursor, old_path, new_path)
        self.conn.commit()
        return True

    def move(self, folder_id: str, new_parent_id: str = None) -> bool:
        """Move a folder to a new parent."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT name, path FROM folders WHERE id = ?", (folder_id,))
        row = cursor.fetchone()
        if not row:
            return False

        name, old_path = row

        # Prevent moving to self or descendant
        if new_parent_id:
            if new_parent_id == folder_id:
                raise ValueError("Cannot move folder into itself")
            new_parent_path = self._get_path_by_id(new_parent_id)
            if new_parent_path is None:
                raise ValueError(f"Target parent '{new_parent_id}' not found")
            if new_parent_path.startswith(old_path):
                raise ValueError("Cannot move folder into its own descendant")
            new_path = f"{new_parent_path}/{name}"
        else:
            new_path = f"/{name}"

        cursor.execute(
            "UPDATE folders SET parent_id = ?, path = ? WHERE id = ?",
            (new_parent_id, new_path, folder_id)
        )
        self._update_descendant_paths(cursor, old_path, new_path)
        self.conn.commit()
        return True

    def delete(self, folder_id: str, recursive: bool = False) -> bool:
        """
        Delete a folder.
        If recursive=True, delete all children folders and their memories.
        If recursive=False, only delete if empty.
        """
        cursor = self.conn.cursor()

        if not recursive:
            # Check for child folders
            cursor.execute(
                "SELECT COUNT(*) FROM folders WHERE parent_id = ?", (folder_id,)
            )
            if cursor.fetchone()[0] > 0:
                raise ValueError("Folder has children — use recursive=True")

            # Check for memories
            cursor.execute(
                "SELECT COUNT(*) FROM memories_v2 WHERE folder_id = ?", (folder_id,)
            )
            if cursor.fetchone()[0] > 0:
                raise ValueError("Folder has memories — use recursive=True")

        if recursive:
            # Get all descendant folder IDs
            descendant_ids = self._get_all_descendant_ids(folder_id)
            all_ids = [folder_id] + descendant_ids
            placeholders = ",".join("?" * len(all_ids))

            # DELETE the memories in these folders (+ their FTS mirror).
            # NOTE: this used to `UPDATE ... SET folder_id = NULL` — i.e. it
            # ORPHANED the memories instead of deleting them. That left the
            # rows in memories_v2 forever, so deleting a folder-with-files
            # never dropped the count, and orphans accumulated (count grew
            # past what the Browser tree shows). The docstring always said
            # "delete ... their memories" — the code now matches it.
            mem_ids = [r[0] for r in cursor.execute(
                f"SELECT id FROM memories_v2 WHERE folder_id IN ({placeholders})",
                all_ids,
            ).fetchall()]
            if mem_ids:
                miq = ",".join("?" * len(mem_ids))
                cursor.execute(f"DELETE FROM memories_v2 WHERE id IN ({miq})", mem_ids)
                cursor.execute(f"DELETE FROM memories_fts WHERE id IN ({miq})", mem_ids)

            # Delete all descendant folders
            cursor.execute(
                f"DELETE FROM folders WHERE id IN ({placeholders})",
                all_ids
            )
        else:
            cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

        self.conn.commit()
        return True

    # ─── Queries ──────────────────────────────────────

    def get(self, folder_id: str) -> dict | None:
        """Get a single folder by ID."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id, parent_id, name, path, created_at FROM folders WHERE id = ?",
            (folder_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "parent_id": row[1], "name": row[2],
            "path": row[3], "created_at": row[4],
        }

    def get_by_path(self, path: str) -> dict | None:
        """Get a folder by its path (e.g., '/Trading/Forex')."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id, parent_id, name, path, created_at FROM folders WHERE path = ?",
            (path,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "parent_id": row[1], "name": row[2],
            "path": row[3], "created_at": row[4],
        }

    def list_children(self, parent_id: str = None) -> list:
        """List direct children of a folder (or root folders if parent_id is None)."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id, parent_id, name, path, created_at FROM folders WHERE parent_id IS ?",
            (parent_id,)
        )
        return [
            {"id": r[0], "parent_id": r[1], "name": r[2],
             "path": r[3], "created_at": r[4]}
            for r in cursor.fetchall()
        ]

    def get_path(self, folder_id: str) -> str | None:
        """Get the full path of a folder (e.g., '/Work/Projects')."""
        return self._get_path_by_id(folder_id)

    def get_tree(self, parent_id: str = None) -> list:
        """
        Get the full folder tree as nested dicts.
        Returns: [{ id, parent_id, name, path, created_at, children, memory_count }, ...]
        Frontend `Folder` interface expects parent_id and created_at — must include both.
        """
        children = self.list_children(parent_id)
        result = []
        for child in children:
            node = {
                "id": child["id"],
                "parent_id": child["parent_id"],
                "name": child["name"],
                "path": child["path"],
                "created_at": child["created_at"],
                "children": self.get_tree(child["id"]),
            }

            # Count memories in this folder
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM memories_v2 WHERE folder_id = ?",
                (child["id"],)
            )
            node["memory_count"] = cursor.fetchone()[0]

            result.append(node)
        return result

    def get_all_memories_in(self, folder_id: str, recursive: bool = True) -> list:
        """Get all memory IDs in a folder (optionally recursive)."""
        cursor = self.conn.cursor()

        if recursive:
            # Get this folder + all descendants
            all_ids = [folder_id] + self._get_all_descendant_ids(folder_id)
            placeholders = ",".join("?" * len(all_ids))
            cursor.execute(
                f"SELECT id FROM memories_v2 WHERE folder_id IN ({placeholders})",
                all_ids
            )
        else:
            cursor.execute(
                "SELECT id FROM memories_v2 WHERE folder_id = ?",
                (folder_id,)
            )

        return [r[0] for r in cursor.fetchall()]

    # ─── Helpers ──────────────────────────────────────

    def _get_path_by_id(self, folder_id: str) -> str | None:
        """Get folder path by ID."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM folders WHERE id = ?", (folder_id,))
        row = cursor.fetchone()
        return row[0] if row else None

    def _get_all_descendant_ids(self, folder_id: str) -> list:
        """Recursively get all descendant folder IDs."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM folders WHERE parent_id = ?", (folder_id,)
        )
        children = [r[0] for r in cursor.fetchall()]
        all_descendants = list(children)
        for child_id in children:
            all_descendants.extend(self._get_all_descendant_ids(child_id))
        return all_descendants

    def _update_descendant_paths(self, cursor, old_prefix: str, new_prefix: str):
        """Update all folder paths that start with old_prefix."""
        cursor.execute(
            "SELECT id, path FROM folders WHERE path LIKE ?",
            (old_prefix + "/%",)
        )
        for fid, fpath in cursor.fetchall():
            new_path = new_prefix + fpath[len(old_prefix):]
            cursor.execute(
                "UPDATE folders SET path = ? WHERE id = ?",
                (new_path, fid)
            )

    def ensure_defaults(self):
        """Seed the default folder layout from brain_manifest.

        Previously hardcoded to Notes + Trading only — that left fresh
        installs missing /Inbox, /Journal, /Tasks, /Reading, /Code,
        /Calendar, /Finance/Expenses, /Finance/Income, /Agents,
        /Archive, /Private, /Reports/Claude-Code — every folder the
        category-routing config depends on. Walks each manifest path
        segment-by-segment so nested folders (e.g. /Finance/Expenses)
        get parent_id wired correctly. Falls back to the legacy 2-folder
        seed if the manifest import ever fails, so a broken manifest
        can never block first-launch.
        """
        try:
            from core.setup.brain_manifest import DEFAULT_MANIFEST
            paths = list(DEFAULT_MANIFEST.get("folders", {}).keys())
        except Exception:
            paths = ["/Notes", "/Trading"]

        for path in paths:
            segments = [s for s in path.split("/") if s]
            parent_id = None
            current = ""
            for seg in segments:
                current = f"{current}/{seg}"
                existing = self.get_by_path(current)
                if existing:
                    parent_id = existing["id"]
                    continue
                created = self.create(seg, parent_id=parent_id)
                parent_id = created["id"] if isinstance(created, dict) else None
