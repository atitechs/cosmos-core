import lancedb
import os
from datetime import datetime
import json
import threading

class MemoryStore:
    def __init__(self, db_path="data/brain_memory"):
        os.makedirs(db_path, exist_ok=True)
        
        self.lock = threading.Lock()
        
        # 1. Vector DB (LanceDB)
        self.vector_db = lancedb.connect(os.path.join(db_path, "lancedb"))
        
        # 2. Metadata DB (SQLite) — share the WAL/busy_timeout/synchronous
        # tuning via the centralized helper.
        from core.memory.db_conn import open_sqlite
        self.sqlite_conn = open_sqlite(os.path.join(db_path, "metadata.db"))
        self._init_sqlite()

    def _init_sqlite(self):
        cursor = self.sqlite_conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                category TEXT,
                typed_data TEXT,
                source TEXT,
                created_at TIMESTAMP,
                importance_score REAL
            )
        """)
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
        self.sqlite_conn.commit()

    def store(self, memory_id, content, vector, category, typed_data, source, importance=0.5):
        # บันทึกใน Vector DB
        table_name = "memories"
        data = [{
            "id": memory_id,
            "vector": vector,
            "text": content,
            "category": category
        }]
        
        if table_name in self.vector_db.table_names():
            table = self.vector_db.open_table(table_name)
            table.add(data)
        else:
            self.vector_db.create_table(table_name, data=data)
            
        # บันทึกใน SQLite
        with self.lock:
            cursor = self.sqlite_conn.cursor()
            cursor.execute("""
                INSERT INTO memories (id, content, category, typed_data, source, created_at, importance_score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                memory_id, 
                content, 
                category, 
                json.dumps(typed_data), 
                source, 
                datetime.now().isoformat(), 
                importance
            ))
            self.sqlite_conn.commit()
        return True

    def add_relationship(self, source_id, target_id, relation_type="related", weight=0.5):
        with self.lock:
            cursor = self.sqlite_conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO relationships (source_id, target_id, relation_type, weight, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (source_id, target_id, relation_type, weight, datetime.now().isoformat()))
            self.sqlite_conn.commit()
        return True

    def get_relationships(self, memory_id):
        with self.lock:
            cursor = self.sqlite_conn.cursor()
            cursor.execute("""
                SELECT target_id, relation_type, weight FROM relationships 
                WHERE source_id = ? OR target_id = ?
            """, (memory_id, memory_id))
            return cursor.fetchall()

_store = None

def get_store():
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
