"""
Cosmos v5 — Phase 1 Integration Test
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests all Tier 0 modules end-to-end.
Run: python -m core.memory.tests.test_phase1
"""
import os
import sys
import time
import json
import tempfile
import shutil

sys.path.insert(0, os.getcwd())


def test_all():
    print("=" * 60)
    print("🧪 Cosmos v5 Phase 1 — Integration Test Suite")
    print("=" * 60)

    # Use temp directory for test DB
    test_dir = os.path.join(os.getcwd(), "data", "test_v2")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    passed = 0
    failed = 0
    total_time = 0

    # ── Test 1: Schema Registry ────────────────────────
    print("\n📋 Test 1: Schema Registry")
    t0 = time.time()
    try:
        from core.memory.schema_registry_v2 import SchemaRegistryV2

        reg = SchemaRegistryV2()

        # Check 10 built-in schemas
        categories = reg.list_all()
        assert len(categories) >= 10, f"Expected 10+ schemas, got {len(categories)}"
        assert "expense" in categories
        assert "trade" in categories
        assert "journal" in categories
        assert "note" in categories

        # Validate expense
        valid, errors = reg.validate("expense", {"amount": 65.0, "vendor": "Starbucks"})
        assert valid, f"Valid expense rejected: {errors}"

        # Validate missing required field
        valid, errors = reg.validate("expense", {"vendor": "Starbucks"})
        assert not valid, "Missing required field should fail"
        assert any("amount" in e for e in errors)

        # Get numeric fields
        nums = reg.get_numeric_fields("trade")
        assert "net_pnl" in nums
        assert "entry_price" in nums

        # Get categorical fields
        cats = reg.get_categorical_fields("trade")
        assert "session" in cats
        assert "direction" in cats

        # Apply defaults
        result = reg.apply_defaults("task", {})
        assert result.get("done") == False

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — {len(categories)} schemas, validation works")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 2: Store v2 + FTS5 ────────────────────────
    print("\n📦 Test 2: Store v2 (SQLite + FTS5)")
    t0 = time.time()
    try:
        from core.memory.store_v2 import MemoryStoreV2

        store = MemoryStoreV2(db_path=test_dir)

        # Store memories
        id1 = store.store("ค่ากาแฟ 65 บาท สตาร์บัค #expense #food",
                          category="expense",
                          typed_data={"amount": 65, "vendor": "Starbucks", "expense_category": "food"})
        assert id1

        id2 = store.store("EURUSD long @ 1.0850 TP +2R london session #trade",
                          category="trade",
                          typed_data={"pair": "EURUSD", "direction": "long", "session": "london", "net_pnl": 25.50})
        assert id2

        id3 = store.store("วันนี้รู้สึกเหนื่อย แต่ภูมิใจที่จบงานได้ #journal #proud",
                          category="journal",
                          typed_data={"mood": "good", "energy": 6})
        assert id3

        # Get
        mem = store.get(id1)
        assert mem is not None
        assert mem["category"] == "expense"
        assert mem["typed_data"]["amount"] == 65

        # Update
        store.update(id1, typed_data={"amount": 70, "vendor": "Starbucks Updated"})
        mem = store.get(id1)
        assert mem["typed_data"]["amount"] == 70

        # Count
        assert store.count() == 3
        assert store.count(category="trade") == 1

        # Stats
        stats = store.stats()
        assert stats["total_memories"] == 3

        # Delete
        store.delete(id3)
        assert store.count() == 2

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — CRUD + FTS5 sync works")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 3: Folder Hierarchy ───────────────────────
    print("\n📁 Test 3: Folder Hierarchy")
    t0 = time.time()
    try:
        from core.memory.folder import FolderTree

        ft = FolderTree(store.conn)
        ft.ensure_defaults()

        # Check defaults created
        notes = ft.get_by_path("/Notes")
        trading = ft.get_by_path("/Trading")
        assert notes is not None, "Default /Notes not created"
        assert trading is not None, "Default /Trading not created"

        # Create nested folder
        forex = ft.create("Forex", parent_id=trading["id"])
        assert forex["path"] == "/Trading/Forex"

        scalp = ft.create("Scalping", parent_id=forex["id"])
        assert scalp["path"] == "/Trading/Forex/Scalping"

        # List children
        children = ft.list_children(trading["id"])
        assert len(children) >= 1
        assert any(c["name"] == "Forex" for c in children)

        # Get tree
        tree = ft.get_tree()
        assert len(tree) >= 2  # Notes + Trading

        # Rename
        ft.rename(forex["id"], "FX")
        fx = ft.get(forex["id"])
        assert fx["path"] == "/Trading/FX"
        # Check descendant path updated
        sc = ft.get(scalp["id"])
        assert sc["path"] == "/Trading/FX/Scalping"

        # Move
        ft.move(scalp["id"], trading["id"])
        sc = ft.get(scalp["id"])
        assert sc["path"] == "/Trading/Scalping"

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — CRUD + rename + move + path cascade")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 4: BM25 Search ───────────────────────────
    print("\n🔍 Test 4: BM25 Search (FTS5)")
    t0 = time.time()
    try:
        from core.memory.search import BM25Search

        bm25 = BM25Search(store.conn)

        # Re-add test data with id3 deleted, add fresh one
        store.store("วันนี้รู้สึกเหนื่อย แต่ภูมิใจที่จบงานได้ #journal",
                    category="journal",
                    typed_data={"mood": "good"})

        # Search English
        results = bm25.search("EURUSD london")
        assert len(results) > 0, "Should find EURUSD trade"
        assert results[0]["category"] == "trade"

        # Search Thai
        results = bm25.search("กาแฟ")
        assert len(results) > 0, "Should find coffee expense"

        # Search with category filter
        results = bm25.search("london", filters={"category": "trade"})
        assert len(results) > 0
        assert all(r["category"] == "trade" for r in results)

        # Speed test
        t_start = time.time()
        for _ in range(100):
            bm25.search("กาแฟ EURUSD")
        t_search = (time.time() - t_start) / 100 * 1000  # ms per search

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — Thai+EN search, {t_search:.1f}ms/query avg")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 5: Tags ──────────────────────────────────
    print("\n🏷️  Test 5: Tag System")
    t0 = time.time()
    try:
        from core.memory.tags import extract_tags, TagManager

        # Extract tags
        tags = extract_tags("ค่ากาแฟ 65 บาท #expense #food #สตาร์บัค")
        assert "expense" in tags
        assert "food" in tags
        assert "สตาร์บัค" in tags

        # Tag manager
        tm = TagManager(store.conn)
        popular = tm.get_popular_tags(limit=10)
        assert len(popular) > 0, "Should have some tags"

        # Search by tags
        by_tags = tm.search_by_tags(["trade"], mode="OR")
        assert len(by_tags) > 0

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — extract + popular + search")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 6: Universal Aggregator ──────────────────
    print("\n📊 Test 6: Universal Aggregator")
    t0 = time.time()
    try:
        from core.memory.aggregator_v2 import UniversalAggregator

        agg = UniversalAggregator(store.conn)

        # Count expenses
        result = agg.compute("expense", "count")
        assert result["type"] == "count"
        assert result["count"] >= 1

        # Sum expense amount
        result = agg.compute("expense", "sum", field="amount")
        assert result["type"] == "sum"
        assert result["value"] >= 65  # At least our Starbucks

        # Trade overview
        result = agg.compute("trade", "overview")
        assert result["type"] == "overview"
        assert result["total_records"] >= 1

        # Convenience shortcuts
        assert agg.count("expense") >= 1
        assert agg.sum("expense", "amount") >= 65

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — count/sum/overview work")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Test 7: Summarizer ────────────────────────────
    print("\n📝 Test 7: TextRank Summarizer")
    t0 = time.time()
    try:
        from core.memory.summarizer import summarize

        # Short text — should return as-is
        short = "Hello world"
        assert summarize(short) == short

        # Long text
        long_text = """
        The quick brown fox jumps over the lazy dog. This is a test of the TextRank
        summarization algorithm. The algorithm works by building a graph of sentences
        and computing their importance based on connectivity. More connected sentences
        are considered more important. The output is an extractive summary that
        preserves the most important sentences from the original text. This method
        does not require any AI model or GPU. It runs purely on CPU using graph
        algorithms. TextRank was inspired by PageRank, the algorithm used by Google
        to rank web pages. In our case, we rank sentences instead of web pages.
        """ * 3  # Make it long enough

        result = summarize(long_text)
        assert len(result) > 0
        assert len(result) < len(long_text)  # Should be shorter

        dt = time.time() - t0
        total_time += dt
        print(f"   ✅ PASSED ({dt:.3f}s) — short passthrough + long summarization")
        passed += 1
    except Exception as e:
        dt = time.time() - t0
        total_time += dt
        print(f"   ❌ FAILED ({dt:.3f}s): {e}")
        failed += 1
        import traceback; traceback.print_exc()

    # ── Summary ───────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"📊 Results: {passed}/{passed+failed} passed, {failed} failed")
    print(f"⏱️  Total time: {total_time:.3f}s")
    print("=" * 60)

    # Cleanup — close SQLite connections BEFORE rmtree, otherwise
    # macOS / Windows raise PermissionError on the journal/WAL files
    # that are still mmap'd by an open connection. (Linux happily
    # unlinks open files; the test was silently passing there but
    # cascading on Mac CI.)
    try:
        if "store" in dir():
            try:
                store.conn.close()  # type: ignore[name-defined]
            except Exception:
                pass
    except Exception:
        pass
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except PermissionError as e:
            print(f"⚠️  cleanup partial — {e}")

    return failed == 0


if __name__ == "__main__":
    success = test_all()
    sys.exit(0 if success else 1)
