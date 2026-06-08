"""
Brain Manifest — Single source of truth for how the brain is organized.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The manifest tells external AI (Claude / Cursor / etc.) the layout of this
brain: which folders exist, what they're for, where to save new things,
and what is off-limits.

Stored at: data/brain_v2/manifest.json

Design principles:
  - Convention over configuration — sensible defaults work for 90% of users.
  - Single source of truth — folder permissions in MCP layer derive from here.
  - Hot-reloadable — changing manifest takes effect on next MCP call.
  - Safe defaults — `/Private` always denied even if manifest is corrupted.
"""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime
from typing import Optional


MANIFEST_PATH = "data/brain_v2/manifest.json"

# Always-deny folders — hardcoded safety net even if manifest is missing
HARDCODED_PRIVATE_PATHS = {"/Private", "/Secret"}


DEFAULT_MANIFEST: dict = {
    "version": 1,
    "owner": "User",
    "language_pref": "th",
    "created_at": None,    # filled on first save
    "updated_at": None,

    # Folder rules — `ai_writable` and `ai_readable` drive MCP permissions.
    # `auto_subfolder` controls auto-nesting (e.g. "YYYY-MM" → /Journal/2026-04/)
    "folders": {
        "/Inbox": {
            "purpose": "AI default fallback when uncertain where to save",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "note",
        },
        "/Notes": {
            "purpose": "General notes and quick captures",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "note",
        },
        "/Journal": {
            "purpose": "Daily journal entries",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "journal",
            "auto_subfolder": "YYYY-MM",
        },
        "/Trading": {
            "purpose": "Forex / trade journal",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "trade",
        },
        "/Tasks": {
            "purpose": "Tasks and todos",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "task",
        },
        "/Reading": {
            "purpose": "Books, articles, highlights",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "reading",
        },
        "/Code": {
            "purpose": "Code snippets and decisions",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "code",
        },
        "/Calendar": {
            "purpose": "Appointments and events",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "calendar",
        },
        "/Finance/Expenses": {
            "purpose": "Daily expenses",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "expense",
            "auto_subfolder": "YYYY-MM",
        },
        "/Finance/Income": {
            "purpose": "Income records",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "income",
        },
        "/Reports/Claude-Code": {
            "purpose": "Claude Code dogfooding telemetry — task outcomes, token usage, error tracking",
            "ai_writable": True,
            "ai_readable": True,
            "category_hint": "claude_session",
            "auto_subfolder": "YYYY-MM",
        },
        # Multi-agent workspace. The parent ships empty — users
        # provision their own agents through the AI Control Center
        # ("+ New Agent" → name + scope + template + token) or via
        # the FolderTree "+ Agent" shortcut. Empty default keeps the
        # Control Center's empty state coherent ("Provision your first
        # agent") and avoids forcing every new install to inherit two
        # example agents most users won't actually use.
        # Historical: /Agents/_Template, /Agents/Researcher, and
        # /Agents/Coder were seeded here pre-2026-05-10. Removed
        # because (a) _Template was functionally inert (template
        # content lives in `_AGENT_TEMPLATE_FILES` constant in
        # bootstrap_folders.py, not in the folder), and (b) the two
        # examples cluttered the Control Center for users who never
        # touched them.
        "/Agents": {
            "purpose": "Multi-agent workspaces — provision per-agent scopes via AI Control Center",
            "ai_writable": False,
            "ai_readable": True,
            "category_hint": "note",
        },
        "/Archive": {
            "purpose": "Old or inactive content — read-only for AI",
            "ai_writable": False,
            "ai_readable": True,
        },
        "/Private": {
            "purpose": "Personal — AI hands off entirely",
            "ai_writable": False,
            "ai_readable": False,
        },
    },

    # When AI receives a new memory of category X, where to save it.
    # "_unknown" is the fallback for unclassified content.
    "category_routing": {
        "note":     "/Notes",
        "task":     "/Tasks",
        "journal":  "/Journal",
        "trade":    "/Trading",
        "expense":  "/Finance/Expenses",
        "income":   "/Finance/Income",
        "reading":  "/Reading",
        "code":     "/Code",
        "calendar": "/Calendar",
        "research": "/Reading",
        "recipe":   "/Notes",
        "claude_session": "/Reports/Claude-Code",
        "_unknown": "/Inbox",
    },

    # Optional naming hints — AI can use these when generating filenames.
    # Tokens: {YYYY-MM-DD}, {YYYY-MM}, {YYYY}, {HHMM}, plus any typed_data field.
    "naming_conventions": {
        "expense": "{YYYY-MM-DD}_{vendor}_{amount}.md",
        "trade":   "{YYYY-MM-DD}_{HHMM}_{result}_{net_pnl}.md",
        "journal": "{YYYY-MM-DD}_journal.md",
        "calendar":"{YYYY-MM-DD}_{HHMM}_{agenda}.md",
    },

    # Free-form hints AI can read — keep small.
    "ai_hints": [
        "When user asks 'what did I do today', search across all folders for created_at = today.",
        "Default language is Thai unless content is clearly English.",
        "Never write to /Archive or /Private.",
    ],

    # Phase 7A — Code projects watched for live re-indexing.
    # Each entry: {id, name, path, auto_watch, excludes, last_indexed_at, stats}
    "indexed_projects": [],

    # Global watcher settings
    "watcher_settings": {
        "debounce_ms": 2000,
        # NOTE: the watcher auto-skips all dot-folders, so entries like
        # `.git`, `.venv`, `.idea`, `.vscode`, `.zed`, `.cursor`, `.pnpm`,
        # `.cargo`, `.svn`, `.hg`, `.eggs`, `.ruff_cache` are already
        # excluded without listing here. The list below covers
        # non-dot-folder names only.
        "default_excludes": [
            # VCS + Python (non-dot)
            ".git", ".venv", "venv", "__pycache__",
            # JS / TS
            "node_modules", "dist", "build", "out",
            ".next", ".nuxt",
            # Rust
            "target",
            # Go
            "vendor",
            # Test / cache (non-dot variants)
            ".cache", ".pytest_cache", ".mypy_cache", ".tox",
            "coverage", "htmlcov",
            # Static-site generators
            "_site",
        ],
        "pause_when_minimized": False,
        "pause_on_battery": False,
    },
}


# ───────────────────────────────────────────────────────────────────────
# Manager
# ───────────────────────────────────────────────────────────────────────

class BrainManifest:
    def __init__(self, path: str = MANIFEST_PATH):
        # Honor COSMOS_DATA_DIR so the bundled .dmg seeds the manifest
        # in the user's writable data dir, not relative-to-CWD.
        if os.environ.get("COSMOS_DATA_DIR", "").strip():
            from core.runtime_config import data_dir
            self.path = str(data_dir() / "manifest.json")
        else:
            self.path = path
        # RLock so methods that already hold the lock can call save() safely
        self._lock = threading.RLock()
        self.data = self._load_or_seed()

    # ── IO ──
    def _load_or_seed(self) -> dict:
        if not os.path.exists(self.path):
            return self._seed_default()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️  Manifest corrupt at {self.path} — re-seeding default.")
            return self._seed_default()

        # Migration-safe: ensure new keys appear on old manifests.
        # Top-level setdefault is fine for scalars and lists, but
        # `folders` and `category_routing` are dicts where new entries
        # must merge in (otherwise an old manifest never picks up new
        # default folders like /Agents that ship in later releases).
        for k, v in DEFAULT_MANIFEST.items():
            if k in ("folders", "category_routing") and isinstance(v, dict):
                data.setdefault(k, {})
                if isinstance(data[k], dict):
                    for nested_key, nested_val in v.items():
                        data[k].setdefault(nested_key, nested_val)
            else:
                data.setdefault(k, v)
        return data

    def _seed_default(self) -> dict:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        seed = json.loads(json.dumps(DEFAULT_MANIFEST))  # deep copy
        seed["created_at"] = datetime.utcnow().isoformat() + "Z"
        seed["updated_at"] = seed["created_at"]
        self._atomic_write(seed)
        return seed

    def _atomic_write(self, data: dict):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def reload(self) -> dict:
        with self._lock:
            self.data = self._load_or_seed()
        return self.data

    def save(self):
        with self._lock:
            self.data["updated_at"] = datetime.utcnow().isoformat() + "Z"
            self._atomic_write(self.data)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.data))

    # ── Queries ──
    def folder_rule(self, path: str) -> Optional[dict]:
        return self.data.get("folders", {}).get(path)

    def is_writable(self, path: str) -> bool:
        if path in HARDCODED_PRIVATE_PATHS:
            return False
        rule = self.folder_rule(path)
        return rule.get("ai_writable", True) if rule else True

    def is_readable(self, path: str) -> bool:
        if path in HARDCODED_PRIVATE_PATHS:
            return False
        rule = self.folder_rule(path)
        return rule.get("ai_readable", True) if rule else True

    def route_category(self, category: str) -> str:
        routing = self.data.get("category_routing", {})
        return routing.get(category) or routing.get("_unknown", "/Inbox")

    def expand_path(self, base_path: str, when: Optional[datetime] = None) -> str:
        """Expand auto_subfolder placeholders. e.g. /Journal + YYYY-MM → /Journal/2026-04."""
        rule = self.folder_rule(base_path)
        if not rule or not rule.get("auto_subfolder"):
            return base_path
        when = when or datetime.utcnow()
        sub = rule["auto_subfolder"].replace("YYYY", f"{when.year:04d}") \
                                     .replace("MM",   f"{when.month:02d}") \
                                     .replace("DD",   f"{when.day:02d}")
        return f"{base_path.rstrip('/')}/{sub}"

    def naming_template(self, category: str) -> Optional[str]:
        return self.data.get("naming_conventions", {}).get(category)

    # ── Mutations ──
    def patch_folder(self, path: str, **updates):
        with self._lock:
            self.data.setdefault("folders", {})
            existing = self.data["folders"].get(path, {})
            existing.update(updates)
            self.data["folders"][path] = existing
            self.save()

    # ── Indexed Projects (Phase 7A) ──
    def list_projects(self) -> list:
        return list(self.data.get("indexed_projects", []))

    def get_project(self, project_id: str) -> Optional[dict]:
        for p in self.data.get("indexed_projects", []):
            if p.get("id") == project_id:
                return p
        return None

    def add_project(self, project: dict) -> dict:
        with self._lock:
            self.data.setdefault("indexed_projects", [])
            # Reject duplicate paths (normalize)
            path = os.path.abspath(os.path.expanduser(project["path"]))
            for p in self.data["indexed_projects"]:
                if os.path.abspath(p["path"]) == path:
                    return p  # already registered
            project["path"] = path
            self.data["indexed_projects"].append(project)
            self.save()
            return project

    def update_project(self, project_id: str, **updates) -> Optional[dict]:
        with self._lock:
            for p in self.data.get("indexed_projects", []):
                if p.get("id") == project_id:
                    p.update(updates)
                    self.save()
                    return p
            return None

    def remove_project(self, project_id: str) -> bool:
        with self._lock:
            projects = self.data.get("indexed_projects", [])
            before = len(projects)
            self.data["indexed_projects"] = [
                p for p in projects if p.get("id") != project_id
            ]
            if len(self.data["indexed_projects"]) < before:
                self.save()
                return True
            return False

    def watcher_settings(self) -> dict:
        return dict(self.data.get("watcher_settings", {}))

    def patch_category_routing(self, mapping: dict):
        with self._lock:
            self.data.setdefault("category_routing", {})
            self.data["category_routing"].update(mapping)
            self.save()

    def replace(self, new_data: dict):
        # keep created_at if present
        with self._lock:
            new_data.setdefault("created_at", self.data.get("created_at"))
            self.data = new_data
            self.save()

    # ── Sync to MCP permission engine ──
    def sync_to_mcp_permissions(self):
        """
        Mirror folder rules into MCP permission engine.
        - ai_writable=True   → write
        - ai_readable=True   → read
        - else               → deny
        """
        try:
            from core.api.mcp_permissions import get_permission_engine
        except ImportError:
            return  # MCP not available — fine
        perms = get_permission_engine()
        for path, rule in self.data.get("folders", {}).items():
            if rule.get("ai_writable"):
                perms.set_folder(path, "write")
            elif rule.get("ai_readable"):
                perms.set_folder(path, "read")
            else:
                perms.set_folder(path, "deny")


_manifest: Optional[BrainManifest] = None
def get_manifest() -> BrainManifest:
    global _manifest
    if _manifest is None:
        _manifest = BrainManifest()
    return _manifest
