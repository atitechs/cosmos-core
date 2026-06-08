"""
Cosmos v5 — Migration (v4 → v5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Migrates existing 62 trade notes from v4 metadata.db
to v5 brain.db with folders, tags, and FTS5 index.
Non-destructive: v4 DB is never modified.
"""
import os
import sys
import json
import sqlite3
import glob
import yaml

sys.path.append(os.getcwd())

from core.memory.store_v2 import get_store_v2
from core.memory.folder import FolderTree
from core.memory.tags import extract_tags


def migrate_v4_to_v5(
    v4_db_path: str = "data/brain_memory/metadata.db",
    notes_dir: str = "data/notes",
    v5_db_path: str = "data/brain_v2",
):
    """
    Migrate v4 data to v5 schema.

    Strategy:
    1. Create default folders (/Notes, /Trading)
    2. Read existing records from v4 metadata.db
    3. If notes_dir exists, re-import from .md files (richer data)
    4. Insert into v5 with folder=/Trading, category=trade
    5. Build FTS5 index automatically (handled by store_v2)

    Non-destructive: v4 DB is never modified.
    """
    print("🔄 Cosmos v4 → v5 Migration")
    print(f"   v4 DB: {v4_db_path}")
    print(f"   Notes dir: {notes_dir}")
    print(f"   v5 DB: {v5_db_path}")
    print()

    # 1. Initialize v5 store
    store = get_store_v2(v5_db_path)
    folders = FolderTree(store.conn)

    # Check if already migrated
    existing_count = store.count()
    if existing_count > 0:
        print(f"⚠️  v5 DB already has {existing_count} records.")
        print(f"   Skipping migration to prevent duplicates.")
        print(f"   Delete {v5_db_path}/brain.db to re-run migration.")
        return existing_count

    # 2. Create default folders
    folders.ensure_defaults()
    trading_folder = folders.get_by_path("/Trading")
    notes_folder = folders.get_by_path("/Notes")
    print(f"   📁 Created default folders: /Trading, /Notes")

    imported = 0

    # 3. Try importing from .md files first (richer data with YAML frontmatter)
    if os.path.exists(notes_dir):
        note_files = glob.glob(os.path.join(notes_dir, "*.md"))
        if note_files:
            print(f"   📝 Found {len(note_files)} .md files in {notes_dir}")
            imported = _import_from_files(store, trading_folder, note_files)

    # 4. If no files, try importing from v4 SQLite
    if imported == 0 and os.path.exists(v4_db_path):
        print(f"   📦 Importing from v4 metadata.db...")
        imported = _import_from_v4_db(store, trading_folder, v4_db_path)

    if imported == 0:
        print("   ℹ️  No data to migrate. Starting fresh.")

    print(f"\n✅ Migration complete! {imported} records imported.")
    print(f"   v5 DB: {v5_db_path}/brain.db")

    # Show summary
    stats = store.stats()
    print(f"   Total memories: {stats['total_memories']}")
    print(f"   By category: {stats['by_category']}")
    print(f"   Folders: {stats['total_folders']}")

    return imported


def _import_from_files(store, trading_folder, note_files):
    """Import from .md files with YAML frontmatter."""
    imported = 0

    for file_path in note_files:
        try:
            filename = os.path.basename(file_path)
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Parse YAML front matter
            metadata = {}
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    try:
                        metadata = yaml.safe_load(parts[1]) or {}
                    except Exception:
                        pass

            # Determine category
            category = "trade"
            if 'lesson' in filename.lower():
                category = "note"

            # Build typed_data from YAML metadata
            typed_data = {}
            field_map = {
                'date': 'trade_date', 'time': 'time', 'action': 'action',
                'result': 'result', 'net_pnl': 'net_pnl',
                'entry_price': 'entry_price', 'stop_loss': 'sl_price',
                'take_profit': 'tp_price', 'session': 'session',
                'confidence': 'confidence', 'lot_size': 'lot_size',
                'close_reason': 'close_reason',
                'trend_h4': 'trend_h4', 'trend_h1': 'trend_h1',
                'trend_m15': 'trend_m15',
            }
            for yaml_key, td_key in field_map.items():
                val = metadata.get(yaml_key)
                if val is not None:
                    typed_data[td_key] = val

            typed_data["filename"] = filename

            # Extract tags from content
            tags = extract_tags(content)
            if category == "trade":
                tags.append("trade")
                if typed_data.get("session"):
                    tags.append(str(typed_data["session"]))

            # Calculate importance
            importance = 0.5
            pnl = float(typed_data.get('net_pnl', 0) or 0)
            if pnl > 30: importance = 0.9
            elif pnl > 15: importance = 0.7
            elif pnl > 0: importance = 0.6
            elif pnl < -20: importance = 0.8
            elif pnl < 0: importance = 0.5

            # Store
            folder_id = trading_folder["id"] if category == "trade" else None
            store.store(
                content=content,
                category=category,
                typed_data=typed_data,
                tags=list(set(tags)),
                folder_id=folder_id,
                source=f"migration:file:{filename}",
                importance=importance,
            )
            imported += 1

        except Exception as e:
            print(f"   ❌ Error importing {os.path.basename(file_path)}: {e}")

    print(f"   ✅ Imported {imported}/{len(note_files)} files")
    return imported


def _import_from_v4_db(store, trading_folder, v4_db_path):
    """Import from v4 metadata.db SQLite."""
    imported = 0
    try:
        v4_conn = sqlite3.connect(v4_db_path)
        cursor = v4_conn.cursor()
        cursor.execute("SELECT id, content, category, typed_data, source, created_at, importance_score FROM memories")
        rows = cursor.fetchall()
        v4_conn.close()

        for row in rows:
            v4_id, content, category, typed_data_json, source, created_at, importance = row
            typed_data = json.loads(typed_data_json or "{}")
            tags = extract_tags(content or "")
            if category == "trade":
                tags.append("trade")

            folder_id = trading_folder["id"] if category == "trade" else None

            store.store(
                content=content or "",
                category=category or "note",
                typed_data=typed_data,
                tags=list(set(tags)),
                folder_id=folder_id,
                source=f"migration:v4:{source}",
                importance=importance or 0.5,
            )
            imported += 1

        print(f"   ✅ Imported {imported}/{len(rows)} records from v4 DB")

    except Exception as e:
        print(f"   ❌ v4 DB import error: {e}")

    return imported


# ═══════════════════════════════════════════════════════
# CLI Runner
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    migrate_v4_to_v5()
