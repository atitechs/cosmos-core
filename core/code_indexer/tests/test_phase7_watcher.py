"""Phase 7A — File watcher + project registry tests."""
from __future__ import annotations
import os
import sys
import time
import shutil
import tempfile
import threading
import contextlib
import io

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from core.setup.brain_manifest import BrainManifest
from core.code_indexer.project_registry import ProjectRegistry
from core.code_indexer.watcher import FileWatcher, _DebouncedHandler
from core.code_indexer.watcher_manager import WatcherManager


def _isolate_manifest() -> BrainManifest:
    """Build a manifest backed by a fresh temp file."""
    fd, path = tempfile.mkstemp(suffix="_manifest.json")
    os.close(fd)
    os.remove(path)
    return BrainManifest(path=path)


def test_project_registry_crud():
    print("📋 Test 1: ProjectRegistry CRUD")
    started = time.time()

    manifest = _isolate_manifest()
    reg = ProjectRegistry(manifest=manifest)

    proj_dir = tempfile.mkdtemp(prefix="proj_")
    try:
        # Add
        p = reg.add(path=proj_dir, name="My Project")
        assert p["id"], "project should have id"
        assert p["auto_watch"] is True, "default auto_watch=True"
        assert os.path.isabs(p["path"]), "path should be absolute"

        # Idempotent add (same path)
        p2 = reg.add(path=proj_dir)
        assert p2["id"] == p["id"], "duplicate path should return existing"

        # List + get
        assert len(reg.list()) == 1
        assert reg.get(p["id"])["name"] == "My Project"
        assert reg.find_by_path(proj_dir)["id"] == p["id"]

        # Update
        upd = reg.update(p["id"], auto_watch=False)
        assert upd["auto_watch"] is False
        assert reg.get(p["id"])["auto_watch"] is False

        # mark_indexed
        reg.mark_indexed(p["id"], stats={"files": 5, "symbols": 42, "links": 7})
        cur = reg.get(p["id"])
        assert cur["last_indexed_at"] is not None
        assert cur["stats"]["symbols"] == 42

        # Effective excludes merge global + per-project
        reg.update(p["id"], excludes=["custom_dir"])
        excludes = reg.effective_excludes(reg.get(p["id"]))
        assert "custom_dir" in excludes
        assert ".venv" in excludes  # global default

        # Remove
        assert reg.remove(p["id"]) is True
        assert reg.list() == []
        assert reg.remove(p["id"]) is False  # idempotent

        # Reject non-existent path
        try:
            reg.add(path="/this/does/not/exist/abc123")
        except ValueError:
            pass
        else:
            raise AssertionError("should reject missing path")
    finally:
        shutil.rmtree(proj_dir, ignore_errors=True)
        os.remove(manifest.path) if os.path.exists(manifest.path) else None

    print(f"   ✅ PASSED ({time.time() - started:.3f}s)")


def test_debounced_handler():
    print("📋 Test 2: Debouncing batches rapid events")
    started = time.time()

    work = os.path.realpath(tempfile.mkdtemp(prefix="watch_"))
    flushed: list = []
    flush_event = threading.Event()

    def on_flush(paths):
        flushed.append(list(paths))
        flush_event.set()

    handler = _DebouncedHandler(
        root=work, excludes={".venv"}, flush_callback=on_flush, debounce=0.3
    )

    # Simulate rapid event burst across 5 files
    class Evt:
        def __init__(self, path, is_dir=False):
            self.src_path = path
            self.is_directory = is_dir

    for i in range(5):
        path = os.path.join(work, f"a_{i}.py")
        with open(path, "w") as f:
            f.write(f"x = {i}\n")
        handler.on_modified(Evt(path))

    # Excluded extension — should not be tracked
    other = os.path.join(work, "image.png")
    with open(other, "w") as f:
        f.write("not code")
    handler.on_modified(Evt(other))

    # Excluded folder
    venv_dir = os.path.join(work, ".venv")
    os.makedirs(venv_dir, exist_ok=True)
    excluded = os.path.join(venv_dir, "x.py")
    with open(excluded, "w") as f:
        f.write("nope")
    handler.on_modified(Evt(excluded))

    flush_event.wait(timeout=2.0)
    handler.stop()
    shutil.rmtree(work, ignore_errors=True)

    assert len(flushed) == 1, f"should flush once, got {len(flushed)}"
    paths = flushed[0]
    assert len(paths) == 5, f"should have 5 .py paths, got {len(paths)}: {paths}"
    assert all(p.endswith(".py") and ".venv" not in p for p in paths)

    print(f"   ✅ PASSED ({time.time() - started:.3f}s) — 5 events → 1 flush")


def test_file_watcher_lifecycle():
    print("📋 Test 3: FileWatcher start/stop + real fs event")
    started = time.time()

    work = tempfile.mkdtemp(prefix="watch_lc_")
    project = {
        "id": "test-1",
        "name": "Test",
        "path": work,
    }
    received: list = []
    got_event = threading.Event()

    def on_changes(pid, paths):
        received.append((pid, paths))
        got_event.set()

    watcher = FileWatcher(project, on_changes=on_changes,
                          excludes={".venv"}, debounce_ms=200)
    assert watcher.start() is True
    assert watcher.is_alive()

    try:
        # Trigger a real fs event
        time.sleep(0.1)
        target = os.path.join(work, "main.py")
        with open(target, "w") as f:
            f.write("def hello(): pass\n")

        got_event.wait(timeout=3.0)
        assert received, "watcher should have detected file creation"
        pid, paths = received[0]
        assert pid == "test-1"
        assert any(p.endswith("main.py") for p in paths)
    finally:
        watcher.stop()
        assert not watcher.is_alive()
        shutil.rmtree(work, ignore_errors=True)

    print(f"   ✅ PASSED ({time.time() - started:.3f}s) — real fs event detected")


def test_watcher_manager_sync():
    print("📋 Test 4: WatcherManager sync respects auto_watch toggle")
    started = time.time()

    manifest = _isolate_manifest()
    proj_dir = tempfile.mkdtemp(prefix="mgr_")

    try:
        reg = ProjectRegistry(manifest=manifest)
        p = reg.add(path=proj_dir, name="Mgr Test", auto_watch=True)

        # Build manager that uses our isolated registry
        manager = WatcherManager()
        manager.registry = reg
        manager.manifest = manifest

        manager.sync()
        time.sleep(0.2)
        statuses = manager.status()
        assert len(statuses) == 1
        assert statuses[0]["is_watching"] is True

        # Toggle off → watcher should stop
        reg.update(p["id"], auto_watch=False)
        manager.sync()
        time.sleep(0.2)
        statuses = manager.status()
        assert statuses[0]["is_watching"] is False

        # Toggle back on
        reg.update(p["id"], auto_watch=True)
        manager.sync()
        time.sleep(0.2)
        assert manager.status()[0]["is_watching"] is True

        # Remove → manager.sync() should drop the watcher
        reg.remove(p["id"])
        manager.sync()
        time.sleep(0.2)
        assert manager.status() == []

        manager.shutdown()
    finally:
        shutil.rmtree(proj_dir, ignore_errors=True)
        if os.path.exists(manifest.path):
            os.remove(manifest.path)

    print(f"   ✅ PASSED ({time.time() - started:.3f}s)")


def test_watcher_manager_reindex_keeps_stdout_clean():
    print("📋 Test 5: Reindex worker does not write to stdout")
    started = time.time()

    manifest = _isolate_manifest()
    proj_dir = tempfile.mkdtemp(prefix="mgr_stdout_")

    try:
        reg = ProjectRegistry(manifest=manifest)
        p = reg.add(path=proj_dir, name="Stdout Guard", auto_watch=False)

        manager = WatcherManager()
        manager.registry = reg
        manager.manifest = manifest
        manager._collect_stats = lambda root: {}

        import core.code_indexer.indexer as indexer_module
        original_indexer = indexer_module.CodeIndexer

        class FakeCodeIndexer:
            def __init__(self, root):
                print("constructor stdout leak")

            def scan_all(self, progress_callback=None):
                print("scan stdout leak")
                if progress_callback:
                    progress_callback({"stage": "done", "current": 1,
                                       "total": 1, "current_file": None,
                                       "percent": 100.0})

        indexer_module.CodeIndexer = FakeCodeIndexer
        try:
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                manager._reindex(p, [])
            assert captured.getvalue() == "", (
                "reindex must keep stdout clean for MCP stdio; "
                f"captured: {captured.getvalue()!r}"
            )
        finally:
            indexer_module.CodeIndexer = original_indexer
    finally:
        shutil.rmtree(proj_dir, ignore_errors=True)
        if os.path.exists(manifest.path):
            os.remove(manifest.path)

    print(f"   ✅ PASSED ({time.time() - started:.3f}s)")


def main():
    print("=" * 60)
    print("🧪 Cosmos v5 Phase 7A — Watcher + Project Registry")
    print("=" * 60)
    tests = [
        test_project_registry_crud,
        test_debounced_handler,
        test_file_watcher_lifecycle,
        test_watcher_manager_sync,
        test_watcher_manager_reindex_keeps_stdout_clean,
    ]
    passed = failed = 0
    t0 = time.time()
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"   ❌ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"📊 Results: {passed}/{passed + failed} passed, {failed} failed")
    print(f"⏱️  Total: {time.time() - t0:.3f}s")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
