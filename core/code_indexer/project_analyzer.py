"""
Project Structure Analyzer
━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates a high-level overview of the project by analyzing config files
and aggregating stats from the code index.
"""

import os
import json
import sys
from typing import Dict, Any, List


def _log(message: str):
    """Log away from stdout so MCP stdio JSON-RPC stays clean."""
    print(message, file=sys.stderr, flush=True)


class ProjectAnalyzer:
    def __init__(self, workspace_root: str):
        self.root = workspace_root
        
    def analyze(self, store_conn) -> Dict[str, Any]:
        """Build a detailed project overview."""
        cursor = store_conn.cursor()
        
        # Design & styling extraction (Phase 5.13)
        from core.code_indexer.design_extractor import DesignExtractor
        try:
            design_context = DesignExtractor(self.root).extract()
        except Exception as e:
            _log(f"⚠️  [ProjectAnalyzer] Design extraction failed: {e}")
            design_context = {}
        
        return {
            "frameworks": self._detect_frameworks(),
            "entry_points": self._find_entry_points(),
            "stats": self._get_stats(store_conn),
            "modules": self._summarize_modules(store_conn),
            "core_components": self._get_core_components(cursor),
            "dependencies": self._get_module_dependencies(cursor),
            "design_context": design_context
        }

    def _get_core_components(self, cursor) -> List[Dict]:
        """Find symbols that are most heavily used across the project."""
        cursor.execute("""
            SELECT target_id, COUNT(*) as call_count
            FROM code_links
            WHERE link_type = 'call'
            GROUP BY target_id
            ORDER BY call_count DESC
            LIMIT 10
        """)
        results = []
        for row in cursor.fetchall():
            target_id, count = row
            # Fetch symbol name for the ID
            cursor.execute("SELECT symbol_name, file_path FROM code_index WHERE id = ?", (target_id,))
            sym = cursor.fetchone()
            if sym:
                results.append({"name": sym[0], "file": sym[1], "calls": count})
        return results

    def _get_module_dependencies(self, cursor) -> List[Dict]:
        """Identify which top-level modules depend on each other."""
        cursor.execute("""
            SELECT 
                SUBSTR(ci_src.file_path, 1, INSTR(ci_src.file_path, '/') - 1) as src_mod,
                SUBSTR(ci_dst.file_path, 1, INSTR(ci_dst.file_path, '/') - 1) as dst_mod,
                COUNT(*) as link_count
            FROM code_links cl
            JOIN code_index ci_src ON cl.source_id = ci_src.id
            JOIN code_index ci_dst ON cl.target_id = ci_dst.id
            WHERE src_mod != '' AND dst_mod != '' AND src_mod != dst_mod
            GROUP BY src_mod, dst_mod
            ORDER BY link_count DESC
            LIMIT 15
        """)
        return [{"source": r[0], "target": r[1], "count": r[2]} for r in cursor.fetchall()]
        
    def _detect_frameworks(self) -> List[str]:
        frameworks = []
        
        # Check Node.js
        pkg_json_path = os.path.join(self.root, "package.json")
        if os.path.isfile(pkg_json_path):
            try:
                with open(pkg_json_path, 'r') as f:
                    data = json.load(f)
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    if "react" in deps: frameworks.append("React")
                    if "next" in deps: frameworks.append("Next.js")
                    if "vite" in deps: frameworks.append("Vite")
                    if "@tauri-apps/api" in deps: frameworks.append("Tauri (Frontend)")
            except Exception:
                pass
                
        # Check Rust
        cargo_path = os.path.join(self.root, "Cargo.toml")
        if os.path.isfile(cargo_path):
            frameworks.append("Rust Cargo")
            try:
                with open(cargo_path, 'r') as f:
                    content = f.read()
                    if "tauri =" in content or "tauri=" in content:
                        frameworks.append("Tauri (Backend)")
            except Exception:
                pass
                
        # Check Python
        req_path = os.path.join(self.root, "requirements.txt")
        if os.path.isfile(req_path):
            try:
                with open(req_path, 'r') as f:
                    content = f.read()
                    if "fastapi" in content: frameworks.append("FastAPI")
                    if "django" in content: frameworks.append("Django")
                    if "flask" in content: frameworks.append("Flask")
            except Exception:
                pass
                
        return list(set(frameworks))
        
    def _find_entry_points(self) -> List[str]:
        candidates = [
            "src/main.rs", "src/main.ts", "src/main.tsx", "src/index.ts", "src/index.js",
            "main.py", "app.py", "server.py", "core/api/rest_server.py", "core/api/rest_server_v2.py"
        ]
        
        found = []
        for c in candidates:
            if os.path.isfile(os.path.join(self.root, c)):
                found.append(c)
                
        return found
        
    def _get_stats(self, store_conn) -> Dict[str, int]:
        cursor = store_conn.cursor()
        
        stats = {}
        
        # Total files
        cursor.execute("SELECT COUNT(DISTINCT file_path) FROM code_index")
        stats["total_files"] = cursor.fetchone()[0]
        
        # Functions/Classes
        cursor.execute("SELECT symbol_type, COUNT(*) FROM code_index GROUP BY symbol_type")
        for row in cursor.fetchall():
            stats[f"total_{row[0]}s"] = row[1]
            
        # Links
        cursor.execute("SELECT link_type, COUNT(*) FROM code_links GROUP BY link_type")
        for row in cursor.fetchall():
            stats[f"total_{row[0]}_links"] = row[1]
            
        return stats
        
    def _summarize_modules(self, store_conn) -> Dict[str, int]:
        """Count symbols per top-level directory."""
        cursor = store_conn.cursor()
        cursor.execute("SELECT file_path FROM code_index")
        
        modules = {}
        for row in cursor.fetchall():
            path = row[0]
            top_dir = path.split('/')[0] if '/' in path else "root"
            modules[top_dir] = modules.get(top_dir, 0) + 1
            
        return modules
