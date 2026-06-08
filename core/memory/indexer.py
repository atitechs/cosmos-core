import uuid
import os
import json
import glob
import yaml

from core.ai.classifier import get_classifier
from core.ai.extractor import get_extractor
from core.memory.embedder import get_embedder
from core.memory.store import get_store
from core.ai.bridge import get_bridge

class BrainIndexer:
    def __init__(self):
        self.classifier = get_classifier()
        self.extractor = get_extractor()
        self.embedder = get_embedder()
        self.store = get_store()
        self.bridge = get_bridge()
        self.progress = {"status": "idle", "current": 0, "total": 0, "percent": 0}

    def process_and_store(self, text, source="manual"):
        """
        กระบวนการเปลี่ยนข้อความดิบให้เป็นความจำ (Full Pipeline)
        """
        print(f"🧠 Brain Indexer: Processing new memory from {source}...")
        
        # 1. Generate Unique ID
        memory_id = str(uuid.uuid4())
        
        # 2. AI Steps (ข้ามถ้าอยู่ใน Mode 'none')
        if self.bridge.mode != "none":
            # 2.1 Classify Category
            category = self.classifier.classify(text)
            print(f"  - Category: {category}")
            
            # 2.2 Extract Structured Data
            typed_data = self.extractor.extract(text, category)
            print(f"  - Data Extracted: {list(typed_data.keys())}")
        else:
            # Mode C: No AI fallback
            category = "note"
            typed_data = {}
            print(f"  - AI Mode is OFF: Defaulting to 'note'")

        # 3. Generate Embedding Vector (ใช้ BGE-M3 เสมอเพื่อการค้นหาข้ามภาษา)
        print(f"  - Generating Vector...")
        vector = self.embedder.encode(text)
        
        # 4. Save to Database (Vector DB + SQLite)
        print(f"  - Saving to Brain Store...")
        success = self.store.store(
            memory_id=memory_id,
            content=text,
            vector=vector,
            category=category,
            typed_data=typed_data,
            source=source,
            importance=0.5 # Default score สำหรับตอนนี้
        )
        
        if success:
            print(f"✅ Memory stored successfully! ID: {memory_id}")
            
            # 5. Proactive Relationship Building (Librarian's Linker)
            self._build_proactive_links(memory_id, vector, category)
            
            return memory_id
        else:
            print(f"❌ Failed to store memory.")
            return None

    def full_sync(self, notes_dir="data/notes"):
        """
        Full Sync: ลบข้อมูลเก่าทั้งหมด แล้ว Import ใหม่จาก data/notes
        ใช้เมื่อผู้ใช้เปลี่ยนชุดข้อมูล
        """
        try:
            print("🔄 Full Sync: Starting...")
            
            # 1. นับไฟล์ที่ต้อง Import
            note_files = glob.glob(os.path.join(notes_dir, "*.md"))
            total_files = len(note_files)
            
            if total_files == 0:
                print("❌ No .md files found in data/notes")
                self.progress = {"status": "error", "message": "No files found", "percent": 0}
                return 0
            
            self.progress = {"status": "syncing", "current": 0, "total": total_files, "percent": 0}
            print(f"   Found {total_files} files to import")
            
            # 2. ลบข้อมูลเก่าทั้งหมด
            print("   🗑️ Clearing old data...")
            with self.store.lock:
                self.store.sqlite_conn.execute("DELETE FROM memories")
                self.store.sqlite_conn.execute("DELETE FROM relationships")
                self.store.sqlite_conn.commit()
            
            # ลบ LanceDB table
            table_name = "memories"
            if table_name in self.store.vector_db.table_names():
                self.store.vector_db.drop_table(table_name)
            
            print("   ✅ Old data cleared")
            
            # 3. Import ไฟล์ทั้งหมด
            imported = 0
            for i, file_path in enumerate(note_files):
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
                            except:
                                pass
                    
                    # กำหนด Category จาก metadata หรือชื่อไฟล์
                    category = "trade"  # default สำหรับ trade notes
                    if 'lesson' in filename.lower():
                        category = "note"
                    
                    # สร้าง typed_data จาก YAML metadata
                    typed_data = {
                        "filename": filename,
                        "trade_date": str(metadata.get('date', '')),
                        "time": str(metadata.get('time', '')),
                        "action": str(metadata.get('action', '')),
                        "result": str(metadata.get('result', '')),
                        "net_pnl": float(metadata.get('net_pnl', 0)),
                        "entry_price": float(metadata.get('entry_price', 0)),
                        "stop_loss": float(metadata.get('stop_loss', 0)),
                        "take_profit": float(metadata.get('take_profit', 0)),
                        "session": str(metadata.get('session', '')),
                        "confidence": float(metadata.get('confidence', 0)),
                        "lot_size": float(metadata.get('lot_size', 0)),
                        "close_reason": str(metadata.get('close_reason', '')),
                        "trend_h4": str(metadata.get('trend_h4', '')),
                        "trend_h1": str(metadata.get('trend_h1', '')),
                        "trend_m15": str(metadata.get('trend_m15', '')),
                    }
                    
                    # คำนวณ importance จากผลลัพธ์
                    importance = 0.5
                    pnl = typed_data['net_pnl']
                    if pnl > 30: importance = 0.9
                    elif pnl > 15: importance = 0.7
                    elif pnl > 0: importance = 0.6
                    elif pnl < -20: importance = 0.8  # ขาดทุนมากก็สำคัญ
                    elif pnl < 0: importance = 0.5
                    
                    # Generate embedding
                    vector = self.embedder.encode(content)
                    
                    # Store
                    memory_id = str(uuid.uuid4())
                    self.store.store(
                        memory_id=memory_id,
                        content=content,
                        vector=vector,
                        category=category,
                        typed_data=typed_data,
                        source=f"file:{filename}",
                        importance=importance
                    )
                    
                    imported += 1
                    
                except Exception as e:
                    print(f"   ❌ Error importing {filename}: {e}")
                
                # Update progress
                self.progress["current"] = i + 1
                self.progress["percent"] = int(((i + 1) / total_files) * 100)
                
                if (i + 1) % 10 == 0:
                    print(f"   📦 Imported {i + 1}/{total_files} files...")
            
            print(f"   📦 Imported {imported}/{total_files} files. Building relationships...")

            # Build vector-similarity relationships (no AI needed, fast)
            table_name = "memories"
            if table_name in self.store.vector_db.table_names():
                table = self.store.vector_db.open_table(table_name)
                all_memories = table.search().limit(None).to_list()
                links_created = 0
                for mem in all_memories:
                    mem_id = mem['id']
                    vector = mem['vector']
                    category = mem['category']
                    results = table.search(vector).limit(7).to_list()
                    for res in results:
                        target_id = res['id']
                        if target_id == mem_id:
                            continue
                        distance = res.get('_distance', 1.0)
                        score = 1.0 - min(distance, 1.0)
                        if score >= 0.75:
                            relation_type = "category_cluster" if res['category'] == category else "semantic_match"
                            self.store.add_relationship(mem_id, target_id, relation_type, weight=score)
                            links_created += 1
                print(f"   🔗 Built {links_created} relationships")

            print(f"✅ Full Sync Complete! Imported {imported}/{total_files} files.")
            self.progress = {"status": "idle", "current": total_files, "total": total_files, "percent": 100}
            return imported
            
        except Exception as e:
            print(f"❌ Full Sync Error: {e}")
            import traceback
            traceback.print_exc()
            self.progress = {"status": "error", "message": str(e), "percent": 0}
            return 0

    def maintenance_relink(self):
        """
        Deep Sync: AI Quality Pass แบบเต็ม
        Phase 1 — AI Enrichment: re-classify, re-extract, re-encode vector จาก clean_summary
        Phase 2 — Relationship Rebuild: ล้าง relationships เก่า แล้ว rebuild ใหม่ทั้งหมด
        """
        try:
            print("🧠 Deep Sync: Starting AI Quality Pass...")

            table_name = "memories"
            if table_name not in self.store.vector_db.table_names():
                print("   - No memories found.")
                self.progress = {"status": "idle", "current": 0, "total": 0, "percent": 100}
                return 0

            with self.store.lock:
                cursor = self.store.sqlite_conn.cursor()
                cursor.execute("SELECT id, content, category FROM memories")
                all_sqlite_records = cursor.fetchall()

            total = len(all_sqlite_records)
            if total == 0:
                self.progress = {"status": "idle", "current": 0, "total": 0, "percent": 100}
                return 0

            self.progress = {"status": "syncing", "current": 0, "total": total * 2, "percent": 0}

            # --- Phase 1: AI Enrichment ---
            # Re-classify category, re-extract typed_data, re-encode vector จาก clean_summary
            print(f"   - Phase 1: AI Enrichment for {total} records...")
            table = self.store.vector_db.open_table(table_name)
            ai_active = True

            for i, (mem_id, content, old_category) in enumerate(all_sqlite_records):
                if ai_active:
                    try:
                        new_typed_data = self.extractor.extract(content, old_category)

                        if isinstance(new_typed_data, dict) and new_typed_data.get("error") == "AI_UNAVAILABLE":
                            print(f"     ⚠️ AI Extractor unavailable — skipping Phase 1.")
                            ai_active = False
                        else:
                            new_category = new_typed_data.get('category', old_category)
                            clean_summary = new_typed_data.get('clean_summary', content)

                            # Re-encode vector จาก clean_summary แทน raw content
                            new_vector = self.embedder.encode(clean_summary)

                            # อัปเดต SQLite: typed_data + category
                            with self.store.lock:
                                self.store.sqlite_conn.execute(
                                    "UPDATE memories SET typed_data = ?, category = ? WHERE id = ?",
                                    (json.dumps(new_typed_data), new_category, mem_id)
                                )

                            # อัปเดต LanceDB: ลบแล้ว insert ใหม่ด้วย vector และ category ที่ถูกต้อง
                            table.delete(f"id = '{mem_id}'")
                            table.add([{
                                "id": mem_id,
                                "vector": new_vector,
                                "text": content,
                                "category": new_category
                            }])

                    except Exception as ai_err:
                        print(f"     ⚠️ AI Error on {mem_id}: {ai_err}. Switching to Safe Mode...")
                        ai_active = False

                current_step = i + 1
                self.progress["current"] = current_step
                self.progress["percent"] = int((current_step / (total * 2)) * 100)

                if current_step % 20 == 0:
                    self.store.sqlite_conn.commit()
                    print(f"     [Phase 1] {current_step}/{total} enriched...")

            self.store.sqlite_conn.commit()

            # --- Phase 2: Clear + Rebuild Relationships ---
            print(f"   - Phase 2: Clearing old relationships and rebuilding from scratch...")

            with self.store.lock:
                self.store.sqlite_conn.execute("DELETE FROM relationships")
                self.store.sqlite_conn.commit()

            all_memories = table.search().limit(None).to_list()

            links_created = 0
            for i, mem in enumerate(all_memories):
                current_idx = total + i + 1
                self.progress["current"] = current_idx
                self.progress["percent"] = int((current_idx / (total * 2)) * 100)

                mem_id = mem['id']
                vector = mem['vector']
                category = mem['category']

                results = table.search(vector).limit(7).to_list()

                for res in results:
                    target_id = res['id']
                    if target_id == mem_id:
                        continue

                    distance = res.get('_distance', 1.0)
                    score = 1.0 - min(distance, 1.0)

                    if score >= 0.75:
                        relation_type = "category_cluster" if res['category'] == category else "semantic_match"
                        self.store.add_relationship(mem_id, target_id, relation_type, weight=score)
                        links_created += 1

                if (i + 1) % 20 == 0:
                    print(f"     [Phase 2] {i + 1}/{len(all_memories)} nodes scanned...")

            print(f"✅ Deep Sync Complete! Enriched {total} records, built {links_created} relationships.")
            self.progress = {"status": "idle", "current": total * 2, "total": total * 2, "percent": 100}
            return links_created

        except Exception as e:
            print(f"❌ Critical Error during Deep Sync: {e}")
            import traceback
            traceback.print_exc()
            self.progress = {"status": "error", "message": str(e), "percent": 0}
            return 0

    def _build_proactive_links(self, memory_id, vector, category, threshold=0.75):
        """
        บรรณารักษ์ทำการเชื่อมโยงข้อมูลใหม่กับข้อมูลเก่าทันทีที่บันทึก
        """
        print(f"🔗 Librarian: Building proactive links for {memory_id}...")
        
        table_name = "memories"
        if table_name not in self.store.vector_db.table_names():
            return

        table = self.store.vector_db.open_table(table_name)
        
        results = table.search(vector).limit(6).to_list()
        
        links_count = 0
        for res in results:
            target_id = res['id']
            if target_id == memory_id:
                continue
                
            distance = res.get('_distance', 1.0)
            score = 1.0 - min(distance, 1.0)
            
            if score >= threshold:
                relation_type = "semantic_match"
                if res['category'] == category:
                    relation_type = "category_cluster"
                
                self.store.add_relationship(memory_id, target_id, relation_type, weight=score)
                links_count += 1
        
        if links_count > 0:
            print(f"   ✅ Linked with {links_count} existing memories.")

_indexer = None

def get_indexer():
    global _indexer
    if _indexer is None:
        _indexer = BrainIndexer()
    return _indexer
