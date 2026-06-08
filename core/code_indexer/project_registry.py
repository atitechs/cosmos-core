"""
Project Registry — Phase 7A
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Manages the list of code projects that Cosmos watches.

The registry is a thin layer on top of `BrainManifest.indexed_projects`:
  - Normalizes input (path expansion, defaults).
  - Computes derived fields (name from path, default excludes).
  - Returns full project dicts to API consumers.

Schema (per project):
    {
      "id": "uuid",
      "name": "Cosmos",                  # display name (defaults to basename)
      "path": "/absolute/path",
      "auto_watch": true,                 # toggle on/off
      "excludes": [".venv", ...],         # extra excludes on top of global defaults
      "added_at": "ISO-8601",
      "last_indexed_at": "ISO-8601" | null,
      "stats": { "files": 0, "symbols": 0, "links": 0 } | null,
    }
"""
from __future__ import annotations
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict


# Free-tier project limit was removed 2026-05-14 to honor the website's
# "every feature available in the free app today stays free, for everyone,
# indefinitely" promise. Pro positioning is now exclusively cloud-side
# (sync, hosted AI bridge, backup) — local watched-project count is
# uncapped on every plan. The `ProjectLimitExceeded` exception remains
# importable for backwards compatibility but is no longer raised.
class ProjectLimitExceeded(Exception):
    """Deprecated 2026-05-14. Was raised on free-tier project cap; no
    longer thrown since the local-feature cap was removed. Kept as a
    type so any caller catching it still type-checks."""


# Blocked root paths — too broad to index. User can index a SUBFOLDER
# under any of these, but not the path itself. Catches the "user dragged
# their entire home / external drive root" footgun.
def _blocked_root_paths() -> set[str]:
    home = str(Path.home())
    raw = {
        "/",
        "/System",
        "/Library",
        "/Applications",
        "/Volumes",
        "/Network",
        "/private",
        "/tmp",
        "/var",
        "/var/folders",
        "/var/log",
        "/usr",
        "/etc",
        "/Users",
        home,
        os.path.join(home, "Library"),
    }
    # On macOS several of these are symlinks (`/etc → /private/etc`,
    # `/tmp → /private/tmp`, `/var → /private/var`). User input is
    # normalized via realpath, so include both the symlink and its
    # target in the set so either form triggers the block.
    resolved = set()
    for p in raw:
        try:
            resolved.add(os.path.realpath(p))
        except OSError:
            pass
    return raw | resolved


class BlockedRootPath(ValueError):
    """Raised when the user tries to register a path that is too broad
    to index (filesystem root, /Volumes, raw $HOME, etc.). The fix is
    to choose a specific subfolder containing the project."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_path(path: str) -> str:
    # Use realpath so macOS symlink shadows like `/etc → /private/etc` and
    # `/tmp → /private/tmp` resolve to the same canonical form. Without
    # this, the BLOCK_AS_ROOT check let `/private/etc` through while
    # rejecting `/etc` — confusing and exploitable to skip safeguards.
    expanded = os.path.expanduser(path)
    try:
        return os.path.realpath(expanded)
    except OSError:
        return os.path.abspath(expanded)


def _normalize_path_safe(path: str) -> str:
    # Same as _normalize_path but returns "" for empty input instead of
    # silently resolving to cwd via realpath("").
    if not path:
        return ""
    return _normalize_path(path)


class ProjectRegistry:
    def __init__(self, manifest=None):
        if manifest is None:
            from core.setup.brain_manifest import get_manifest
            manifest = get_manifest()
        self.manifest = manifest
        self._warned_blocked: set[str] = set()

    # ── Read ──
    def list(self) -> List[Dict]:
        # Historical orphans: a project registered before the block list
        # included /private/etc (the realpath form of /etc) sits in the
        # manifest forever — list() would surface it to the preamble and
        # the AI would treat /private/etc as a real project. The block
        # check already rejects new attempts; this filter prevents the
        # legacy rows from polluting the UX. Data is preserved in
        # manifest.json — user can remove via the Indexed Projects panel.
        blocked = _blocked_root_paths()
        out: List[Dict] = []
        for p in self.manifest.list_projects():
            norm = _normalize_path_safe(p.get("path", ""))
            if norm and norm in blocked:
                if norm not in self._warned_blocked:
                    print(f"⚠️ [ProjectRegistry] hiding registered project at "
                          f"blocked path {norm!r} (system/root dir); "
                          f"remove it via the Indexed Projects panel.",
                          file=__import__('sys').stderr)
                    self._warned_blocked.add(norm)
                continue
            out.append(p)
        return out

    def get(self, project_id: str) -> Optional[Dict]:
        return self.manifest.get_project(project_id)

    def find_by_path(self, path: str) -> Optional[Dict]:
        target = _normalize_path(path)
        for p in self.list():
            if _normalize_path(p["path"]) == target:
                return p
        return None

    # ── Write ──
    def add(self, path: str, name: Optional[str] = None,
            auto_watch: bool = True, excludes: Optional[List[str]] = None) -> Dict:
        norm = _normalize_path(path)
        if not os.path.isdir(norm):
            raise ValueError(f"Path does not exist or is not a directory: {norm}")

        if norm in _blocked_root_paths():
            raise BlockedRootPath(
                f"Path '{norm}' is too broad to index as a project root. "
                "Choose a specific subfolder (e.g. `~/Documents/code/myproject` "
                "or `/Volumes/MyDrive/work/repo`) so Cosmos doesn't scan the "
                "entire system, network, or external-drive root."
            )

        # Read-permission probe — surfaces a clear error at registration
        # time instead of letting the watcher silently fail later. Without
        # this, dropping a chmod-000 folder into the wizard registers
        # successfully but never indexes a single file. `os.access` returns
        # False for both "no read perm" and "missing"; the isdir check above
        # already rules out "missing", so a False here means permissions.
        if not os.access(norm, os.R_OK | os.X_OK):
            raise PermissionError(
                f"Cosmos doesn't have read+execute permission on '{norm}'. "
                "Open System Settings → Privacy & Security → Files and "
                "Folders, grant Cosmos.app access to this folder, or pick "
                "a folder your user account can read."
            )

        existing = self.find_by_path(norm)
        if existing:
            return existing

        project = {
            "id": str(uuid.uuid4()),
            "name": name or os.path.basename(norm) or norm,
            "path": norm,
            "auto_watch": bool(auto_watch),
            "excludes": list(excludes or []),
            "added_at": _now_iso(),
            "last_indexed_at": None,
            "stats": None,
        }
        return self.manifest.add_project(project)

    def update(self, project_id: str, **updates) -> Optional[Dict]:
        # Allow only known fields to be patched
        allowed = {"name", "auto_watch", "excludes",
                   "last_indexed_at", "stats"}
        clean = {k: v for k, v in updates.items() if k in allowed}
        return self.manifest.update_project(project_id, **clean)

    def remove(self, project_id: str) -> bool:
        return self.manifest.remove_project(project_id)

    def mark_indexed(self, project_id: str, stats: Optional[Dict] = None) -> Optional[Dict]:
        return self.update(project_id,
                           last_indexed_at=_now_iso(),
                           stats=stats)

    # ── Helpers ──
    def effective_excludes(self, project: Dict) -> List[str]:
        """Per-project + global defaults — used by file watcher to filter events."""
        global_defaults = self.manifest.watcher_settings().get("default_excludes", [])
        return list(set(global_defaults) | set(project.get("excludes") or []))


_registry: Optional[ProjectRegistry] = None


def get_project_registry() -> ProjectRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectRegistry()
    return _registry
