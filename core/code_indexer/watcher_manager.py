"""
Watcher Manager — Phase 7A
━━━━━━━━━━━━━━━━━━━━━━━━━━
Central orchestrator for per-project file watchers.

Responsibilities:
    1. Sync watcher state with the project registry — exactly one running
       FileWatcher per project where `auto_watch=True`.
    2. Wire change events to incremental re-indexing.
    3. Track recent activity per project (for the UI).
    4. Provide a graceful `shutdown()` for app exit.

Threading model:
    - watchdog Observers run in their own daemon threads.
    - The debounce timer in `FileWatcher` fires on a Timer thread.
    - Re-index work is scheduled onto an internal worker thread (one
      per project) so a slow scan can't block detection of new events.
"""
from __future__ import annotations
import contextlib
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Deque

from core.code_indexer.project_registry import get_project_registry
from core.code_indexer.watcher import FileWatcher
from core.setup.brain_manifest import get_manifest


def _log(message: str):
    """Log away from MCP stdio stdout, which carries JSON-RPC frames."""
    print(message, file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _ProjectWorker:
    """Serializes re-index calls for a single project so concurrent file
    bursts don't trigger overlapping CodeIndexer scans."""

    def __init__(self, project: dict, manager: "WatcherManager"):
        self.project = project
        self.manager = manager
        self._cond = threading.Condition()
        self._pending: Optional[List[str]] = None
        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, name=f"reindex-{project['id'][:8]}", daemon=True
        )
        self._thread.start()

    def submit(self, paths: List[str]):
        with self._cond:
            if self._pending is None:
                self._pending = list(paths)
            else:
                self._pending.extend(paths)
            self._cond.notify()

    def stop(self, timeout: float = 2.0):
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=timeout)

    def _loop(self):
        while True:
            with self._cond:
                while self._pending is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                paths = self._pending or []
                self._pending = None
            try:
                self.manager._reindex(self.project, paths)
            except Exception as e:
                _log(f"[watcher_manager] reindex failed for "
                     f"{self.project.get('name')}: {e}")


class WatcherManager:
    def __init__(self):
        self.registry = get_project_registry()
        self.manifest = get_manifest()
        self._watchers: Dict[str, FileWatcher] = {}
        self._workers: Dict[str, _ProjectWorker] = {}
        self._activity: Dict[str, Deque[dict]] = {}  # project_id → recent events
        # project_id → live indexing progress dict {stage,current,total,
        # current_file,percent}. Updated from the worker thread during a
        # scan so the UI can poll a progress popup for big repos.
        self._progress: Dict[str, dict] = {}
        self._lock = threading.RLock()

    # ── Lifecycle ──
    def sync(self):
        """Reconcile running watchers with the current registry state.

        Call after add/update/remove to make the running set match what
        the manifest says should be running.
        """
        with self._lock:
            wanted = {
                p["id"]: p for p in self.registry.list()
                if p.get("auto_watch")
            }
            running = set(self._watchers.keys())

            # Stop watchers no longer wanted
            for pid in running - set(wanted.keys()):
                self._stop_one(pid)

            # Start / restart watchers that should be running
            for pid, project in wanted.items():
                existing = self._watchers.get(pid)
                if existing and existing.project.get("path") != project["path"]:
                    self._stop_one(pid)
                    existing = None
                if existing is None:
                    self._start_one(project)

    def shutdown(self):
        """Stop all watchers and workers gracefully."""
        with self._lock:
            for pid in list(self._watchers.keys()):
                self._stop_one(pid)

    # ── Status ──
    def status(self) -> List[dict]:
        with self._lock:
            out = []
            for project in self.registry.list():
                pid = project["id"]
                w = self._watchers.get(pid)
                out.append({
                    "id": pid,
                    "name": project.get("name"),
                    "path": project.get("path"),
                    "auto_watch": project.get("auto_watch", False),
                    "is_watching": bool(w and w.is_alive()),
                    "last_indexed_at": project.get("last_indexed_at"),
                    "stats": project.get("stats"),
                    "recent_activity": list(self._activity.get(pid, [])),
                })
            return out

    def recent_activity(self, project_id: str) -> List[dict]:
        with self._lock:
            return list(self._activity.get(project_id, []))

    def progress(self, project_id: str) -> dict:
        """Live indexing progress for one project. is_running True while a
        scan is in flight. Polled by the UI to drive the progress popup."""
        p = self._progress.get(project_id)
        if not p:
            return {"is_running": False, "stage": "idle", "current": 0,
                    "total": 0, "current_file": None, "percent": 0.0}
        return p

    def trigger_reindex(self, project_id: str) -> bool:
        """Manual full re-index (UI 'Force Re-index' button or MCP tool)."""
        project = self.registry.get(project_id)
        if not project:
            return False
        worker = self._workers.get(project_id)
        if worker is None:
            with self._lock:
                worker = self._workers.get(project_id) or self._make_worker(project)
        worker.submit([])  # empty list = full scan
        return True

    # ── Internals ──
    def _start_one(self, project: dict):
        excludes = self.registry.effective_excludes(project)
        debounce_ms = self.manifest.watcher_settings().get("debounce_ms", 2000)

        worker = self._make_worker(project)
        watcher = FileWatcher(
            project=project,
            on_changes=lambda pid, paths: worker.submit(paths),
            excludes=excludes,
            debounce_ms=debounce_ms,
        )
        if watcher.start():
            self._watchers[project["id"]] = watcher

    def _make_worker(self, project: dict) -> _ProjectWorker:
        pid = project["id"]
        worker = self._workers.get(pid)
        if worker is None:
            worker = _ProjectWorker(project, self)
            self._workers[pid] = worker
        return worker

    def _stop_one(self, project_id: str):
        watcher = self._watchers.pop(project_id, None)
        if watcher:
            watcher.stop()
        worker = self._workers.pop(project_id, None)
        if worker:
            worker.stop()

    def _reindex(self, project: dict, changed_paths: List[str]):
        """Run an incremental re-index. The CodeIndexer already detects
        changes via file hashes, so we just call scan_all() and let it
        do the diffing itself. `changed_paths` is recorded for activity."""
        from core.code_indexer.indexer import CodeIndexer

        pid = project["id"]
        started = time.time()
        self._record_activity(pid, "reindex_start", {"files": len(changed_paths)})
        self._progress[pid] = {"is_running": True, "stage": "scanning",
                               "current": 0, "total": 0, "current_file": None, "percent": 0.0}

        def _progress_cb(p: dict):
            # p is {stage,current,total,current_file,percent} from scan_all.
            self._progress[pid] = {"is_running": True, **p}

        try:
            # Reindex can run inside the MCP stdio process. Any legacy
            # stdout writes from deep indexing code would corrupt JSON-RPC,
            # so redirect them while the background worker is active.
            with contextlib.redirect_stdout(sys.stderr):
                indexer = CodeIndexer(project["path"])
                indexer.scan_all(progress_callback=_progress_cb)
            stats = self._collect_stats(project["path"])
            self.registry.mark_indexed(pid, stats=stats)
            self._progress[pid] = {"is_running": False, "stage": "done",
                                   "current": 0, "total": 0, "current_file": None, "percent": 100.0}
            self._record_activity(pid, "reindex_done", {
                "files": len(changed_paths),
                "duration_ms": int((time.time() - started) * 1000),
                "stats": stats,
            })
            # Auto-regenerate the Obsidian-style Project Map after every
            # reindex so it stays in sync with the call graph. Failure
            # here must NOT fail the reindex itself — it's a side-effect.
            try:
                from core.code_indexer import project_map
                from core.memory.store_v2 import get_store_v2
                project_map.write_moc(project["path"], pid, get_store_v2().conn)
            except Exception as moc_err:
                self._record_activity(pid, "moc_write_skipped", {"error": str(moc_err)})
        except Exception as e:
            self._progress[pid] = {"is_running": False, "stage": "error",
                                   "error": str(e)[:300], "current": 0, "total": 0,
                                   "current_file": None, "percent": 0.0}
            self._record_activity(pid, "reindex_error", {"error": str(e)})
            raise

    def _collect_stats(self, root: str) -> dict:
        try:
            from core.memory.store_v2 import get_store_v2
            cur = get_store_v2().conn.cursor()
            cur.execute(
                "SELECT COUNT(DISTINCT file_path), COUNT(*) "
                "FROM code_index WHERE file_path NOT LIKE ?",
                (f"!{root}%",),  # crude but cheap; real filter is implicit
            )
            files, symbols = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM code_links")
            links = cur.fetchone()[0]
            return {"files": files or 0, "symbols": symbols or 0, "links": links or 0}
        except Exception:
            return {}

    def _record_activity(self, project_id: str, kind: str, payload: dict):
        with self._lock:
            buf = self._activity.setdefault(project_id, deque(maxlen=20))
            buf.appendleft({
                "kind": kind,
                "at": _now_iso(),
                **payload,
            })


_manager: Optional[WatcherManager] = None


def get_watcher_manager() -> WatcherManager:
    global _manager
    if _manager is None:
        _manager = WatcherManager()
    return _manager
