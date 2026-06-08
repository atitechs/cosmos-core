"""
Import Resolver
━━━━━━━━━━━━━━━
Parses imports extracted via AST and attempts to map them to actual file paths.
Used for building dependency graphs between files.
"""

import os
from typing import Optional, List, Dict

class ImportResolver:
    def __init__(self, workspace_root: str):
        self.root = workspace_root
        
    def resolve(self, current_file_path: str, imports: List[Dict], language: str) -> List[Dict]:
        """
        Takes a list of raw AST imports and resolves them to file paths.
        Returns the updated list with 'resolved_path' added.
        """
        resolved_imports = []
        
        for imp in imports:
            resolved_path = None
            raw_path = imp.get("path") or imp.get("name")
            
            if not raw_path:
                continue
                
            if language == "python":
                resolved_path = self._resolve_python(current_file_path, imp)
            elif language in ("typescript", "tsx", "javascript", "jsx"):
                resolved_path = self._resolve_ts_js(current_file_path, imp)
            elif language == "rust":
                resolved_path = self._resolve_rust(current_file_path, imp)
                
            imp_copy = dict(imp)
            imp_copy["resolved_path"] = resolved_path
            resolved_imports.append(imp_copy)
            
        return resolved_imports
        
    def _resolve_python(self, current_file: str, imp: Dict) -> Optional[str]:
        """
        Resolve Python import.
        from core.memory.store import get_store -> core/memory/store.py
        import os -> None (built-in)
        """
        module_path = imp.get("path") # e.g., 'core.memory.store'
        if not module_path:
            return None
            
        # Try relative to workspace root
        parts = module_path.split('.')
        base_path = os.path.join(self.root, *parts)
        
        # Could be a file (module.py)
        if os.path.isfile(base_path + ".py"):
            return os.path.relpath(base_path + ".py", self.root)
            
        # Could be a package (module/__init__.py)
        if os.path.isdir(base_path) and os.path.isfile(os.path.join(base_path, "__init__.py")):
            return os.path.relpath(os.path.join(base_path, "__init__.py"), self.root)
            
        return None
        
    def _resolve_ts_js(self, current_file: str, imp: Dict) -> Optional[str]:
        """
        Resolve TS/JS import.
        import { Button } from '../components/Button' -> src/components/Button.tsx
        import React from 'react' -> None (external)
        """
        source = imp.get("path") # The string literal in import, e.g. '../components/Button'
        if not source:
            return None
            
        # Strip quotes
        source = source.strip("'\"")
        
        # Ignore node_modules
        if not source.startswith('.') and not source.startswith('/'):
            # Might be aliases like @/components/Button, but we'll keep it simple for now
            return None
            
        current_dir = os.path.dirname(os.path.join(self.root, current_file))
        target_base = os.path.normpath(os.path.join(current_dir, source))
        
        # Check extensions
        for ext in ['.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.js']:
            candidate = target_base + ext if not target_base.endswith(ext) else target_base
            if os.path.isfile(candidate):
                return os.path.relpath(candidate, self.root)
                
        return None
        
    def _resolve_rust(self, current_file: str, imp: Dict) -> Optional[str]:
        """
        Resolve Rust use statement.
        use crate::core::memory -> src/core/memory.rs or src/core/memory/mod.rs
        use std::fs -> None (built-in)
        """
        path_str = imp.get("path")
        if not path_str:
            return None
            
        parts = path_str.split('::')
        
        if parts[0] == "std" or parts[0] == "core":
            return None # built-in
            
        # Assuming typical Cargo structure where crate root is src/
        if parts[0] == "crate":
            parts = parts[1:]
            
        base_path = os.path.join(self.root, "src", *parts)
        
        if os.path.isfile(base_path + ".rs"):
            return os.path.relpath(base_path + ".rs", self.root)
            
        if os.path.isdir(base_path) and os.path.isfile(os.path.join(base_path, "mod.rs")):
            return os.path.relpath(os.path.join(base_path, "mod.rs"), self.root)
            
        return None
