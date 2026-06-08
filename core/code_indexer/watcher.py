"""
File Watcher — Phase 7A
━━━━━━━━━━━━━━━━━━━━━━━
Watches a project directory and triggers re-indexing when source files
change. Uses watchdog for the OS-level events and a debouncer thread to
batch rapid bursts (e.g., `git checkout` touching 50 files).

Public surface:
    FileWatcher(project, on_changes, indexer_factory=None,
                debounce_ms=2000, excludes=None)
        .start()   → begin observing in a daemon thread
        .stop()    → gracefully shut down
        .is_alive() → bool

`on_changes(project_id, changed_paths)` is invoked after the debounce
window closes. The watcher itself does not call the indexer — that's the
WatcherManager's job — keeping this class single-responsibility.
"""
from __future__ import annotations
import os
import sys
import threading
from typing import Callable, Iterable, Optional, Set, List

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent


# Source file extensions we care about — must match CodeIndexer.supported_ext
SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs"}


def _log(message: str):
    """Log away from stdout so MCP stdio JSON-RPC stays clean."""
    print(message, file=sys.stderr, flush=True)


class _DebouncedHandler(FileSystemEventHandler):
    """Collect events, fire `flush_callback(paths)` after `debounce` seconds of quiet."""

    def __init__(self, root: str, excludes: Iterable[str],
                 flush_callback: Callable[[List[str]], None],
                 debounce: float = 2.0):
        super().__init__()
        # Resolve symlinks — macOS FSEvents reports realpath
        # (e.g. /var/folders/... → /private/var/folders/...) which would
        # otherwise fall outside `relpath(path, root)`.
        self.root = os.path.realpath(os.path.abspath(root))
        self.excludes = set(excludes)
        self.flush_callback = flush_callback
        self.debounce = debounce

        self._pending: Set[str] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._stopped = False

    # ── watchdog callbacks ──
    def on_modified(self, event: FileSystemEvent):
        self._track(event)

    def on_created(self, event: FileSystemEvent):
        self._track(event)

    def on_deleted(self, event: FileSystemEvent):
        self._track(event)

    def on_moved(self, event):
        self._track_path(getattr(event, "src_path", None))
        self._track_path(getattr(event, "dest_path", None))

    # ── internals ──
    def _track(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._track_path(event.src_path)

    def _track_path(self, path: Optional[str]):
        if not path:
            return
        if not self._is_relevant(path):
            return
        with self._lock:
            if self._stopped:
                return
            self._pending.add(path)
            self._reschedule_locked()

    def _is_relevant(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext not in SOURCE_EXTS:
            return False

        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return False
        if rel.startswith(".."):
            return False

        # Reject if any path segment matches an excluded folder name
        parts = rel.split(os.sep)
        for seg in parts[:-1]:
            if seg in self.excludes:
                return False
            if seg.startswith(".") and seg not in (".", ".."):
                return False  # always skip dot-folders
        return True

    def _reschedule_locked(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        with self._lock:
            paths = sorted(self._pending)
            self._pending.clear()
            self._timer = None
        if paths and not self._stopped:
            try:
                self.flush_callback(paths)
            except Exception as e:
                _log(f"[watcher] flush_callback failed: {e}")

    def stop(self):
        with self._lock:
            self._stopped = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()


class FileWatcher:
    """High-level watcher tied to a single project."""

    def __init__(self, project: dict,
                 on_changes: Callable[[str, List[str]], None],
                 excludes: Optional[Iterable[str]] = None,
                 debounce_ms: int = 2000):
        self.project = project
        self.project_id = project["id"]
        self.root = project["path"]
        self.on_changes = on_changes
        self.excludes = set(excludes or [])
        self.debounce = max(0.05, debounce_ms / 1000.0)

        self._observer: Optional[Observer] = None
        self._handler: Optional[_DebouncedHandler] = None

    def start(self) -> bool:
        if self._observer is not None:
            return True  # already running
        if not os.path.isdir(self.root):
            _log(f"[watcher] {self.project_id}: path missing — {self.root}")
            return False

        self._handler = _DebouncedHandler(
            root=self.root,
            excludes=self.excludes,
            flush_callback=lambda paths: self.on_changes(self.project_id, paths),
            debounce=self.debounce,
        )
        self._observer = Observer()
        self._observer.schedule(self._handler, self.root, recursive=True)
        self._observer.daemon = True
        self._observer.start()
        _log(f"[watcher] started for '{self.project.get('name')}' @ {self.root}")
        return True

    def stop(self, timeout: float = 2.0):
        if self._handler:
            self._handler.stop()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=timeout)
            except Exception as e:
                _log(f"[watcher] stop error: {e}")
            self._observer = None
        self._handler = None

    def is_alive(self) -> bool:
        return self._observer is not None and self._observer.is_alive()
