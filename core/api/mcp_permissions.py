"""
MCP Permission Engine
━━━━━━━━━━━━━━━━━━━━━
Per-tool and per-folder access control for external AI clients.

Design:
- Tool-level:    allow | deny | confirm  (confirm = require user approval each call)
- Folder-level:  read  | write | deny    (default: read)
- Defaults are permissive but writes/deletes need explicit allow.

Persistence: data/brain_v2/mcp_permissions.json
"""
from __future__ import annotations
import json
import os
import threading
from typing import Literal


ToolPermission = Literal["allow", "deny", "confirm"]
FolderPermission = Literal["read", "write", "deny"]


DEFAULT_CONFIG = {
    "version": 1,
    "tools": {
        "brain_search":    "allow",
        "brain_get":       "allow",
        "brain_aggregate": "allow",
        "brain_remember":  "allow",   # change to "confirm" for stricter mode
        "brain_status":    "allow",
    },
    "folders": {
        # path → permission. Path "/" is the root catch-all.
        "/": "write",
    },
    "deny_categories": [],   # e.g. ["expense"] to hide a category from MCP
}


class PermissionEngine:
    def __init__(self, config_path: str = "data/brain_v2/mcp_permissions.json"):
        # Honor COSMOS_DATA_DIR so the bundled .dmg writes permissions
        # into the user's writable data dir, not relative-to-CWD
        # (which may be inside the read-only .app bundle).
        if os.environ.get("COSMOS_DATA_DIR", "").strip():
            from core.runtime_config import data_dir
            self.config_path = str(data_dir() / "mcp_permissions.json")
        else:
            self.config_path = config_path
        self._lock = threading.Lock()
        self.config = self._load()

    # ---- IO ----
    def _load(self) -> dict:
        if not os.path.exists(self.config_path):
            self._save(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Ensure all keys present (migration-safe)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            return dict(DEFAULT_CONFIG)

    def _save(self, cfg: dict):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)

    def reload(self):
        with self._lock:
            self.config = self._load()

    # ---- Queries ----
    def tool_permission(self, tool_name: str) -> ToolPermission:
        """Returns the configured permission for `tool_name`. Scans every
        equivalent alias (legacy `brain_*`/`code_*` and canonical
        `cosmos_*`) and applies the most-restrictive rule found —
        DENY > CONFIRM > ALLOW. This means an explicit deny stored under
        any form blocks calls under every form: the user cannot be played
        against themselves by choosing a different name for the same
        handler. If no form has an explicit rule, default is `allow`."""
        from core.api.tool_aliases import all_forms  # local import to avoid cycle

        rules = self.config["tools"]
        precedence = {"deny": 3, "confirm": 2, "allow": 1}
        best: ToolPermission | None = None
        for form in all_forms(tool_name):
            rule = rules.get(form)
            if rule is None:
                continue
            if best is None or precedence.get(rule, 0) > precedence.get(best, 0):
                best = rule
        return best if best is not None else "allow"

    def folder_permission(self, path: str) -> FolderPermission:
        """Walk up the path tree to find the closest matching rule."""
        if not path:
            return self.config["folders"].get("/", "read")
        # Direct hit
        if path in self.config["folders"]:
            return self.config["folders"][path]
        # Walk up
        parts = path.strip("/").split("/")
        while parts:
            parts.pop()
            candidate = "/" + "/".join(parts) if parts else "/"
            if candidate in self.config["folders"]:
                return self.config["folders"][candidate]
        return self.config["folders"].get("/", "read")

    def category_allowed(self, category: str) -> bool:
        return category not in self.config.get("deny_categories", [])

    # ---- Mutations ----
    def set_tool(self, tool_name: str, permission: ToolPermission):
        with self._lock:
            self.config["tools"][tool_name] = permission
            self._save(self.config)

    def set_folder(self, path: str, permission: FolderPermission):
        with self._lock:
            self.config["folders"][path] = permission
            self._save(self.config)

    def set_deny_categories(self, categories: list[str]):
        with self._lock:
            self.config["deny_categories"] = categories
            self._save(self.config)

    def to_dict(self) -> dict:
        return dict(self.config)

    # ---- Authorization helpers ----
    def can_call_tool(self, tool_name: str) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        perm = self.tool_permission(tool_name)
        if perm == "allow":
            return True, "allowed"
        if perm == "deny":
            return False, f"Tool '{tool_name}' is denied by user policy."
        if perm == "confirm":
            # In Phase 3 v1 we treat confirm == allow but tag it; UI decides later.
            return True, "confirm-required"
        return False, f"Unknown permission: {perm}"

    def can_read_folder(self, path: str) -> tuple[bool, str]:
        perm = self.folder_permission(path)
        if perm in ("read", "write"):
            return True, perm
        return False, f"Folder '{path}' read access denied."

    def can_write_folder(self, path: str) -> tuple[bool, str]:
        perm = self.folder_permission(path)
        if perm == "write":
            return True, "write"
        return False, f"Folder '{path}' write access denied (current: {perm})."


_engine: PermissionEngine | None = None
def get_permission_engine() -> PermissionEngine:
    global _engine
    if _engine is None:
        _engine = PermissionEngine()
    return _engine
