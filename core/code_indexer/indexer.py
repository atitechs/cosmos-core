"""
Code Indexer Foundation — Phase 5.2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Crawls the workspace, parses files using tree-sitter ASTs,
extracts symbols, imports, and builds call graphs.
Supports Python, TypeScript, and Rust.
"""

import os
import hashlib
import json
import sys
from datetime import datetime
from typing import List, Dict, Tuple
from core.code_indexer.project_analyzer import ProjectAnalyzer

try:
    from tree_sitter import Language, Parser, Query
    # QueryCursor was briefly removed in tree_sitter 0.23.x and re-added in
    # 0.24+. The fallback uses Query.matches() directly. Local 0.25.2 has
    # both so the primary import wins; if we ever pin to 0.23 the fallback
    # picks up. Track which path we took so callers can branch on it.
    try:
        from tree_sitter import QueryCursor  # 0.22 / 0.24+
        _HAS_QUERY_CURSOR = True
    except ImportError:
        _HAS_QUERY_CURSOR = False
    TS_AVAILABLE = True
    _TS_IMPORT_ERR: str | None = None
except ImportError as exc:
    # Capture the error so the warn message can tell the user WHICH module
    # was missing (e.g. tree_sitter_python vs tree_sitter itself). The old
    # silent `TS_AVAILABLE = False` made indexer issues opaque — operator
    # would see "indexer disabled" but not why.
    _TS_IMPORT_ERR = str(exc)
    _HAS_QUERY_CURSOR = False
    TS_AVAILABLE = False

from core.memory.store_v2 import get_store_v2
from core.code_indexer.queries import QUERIES
from core.code_indexer.import_resolver import ImportResolver
from core.code_indexer.call_graph import CallGraphBuilder


def _log(message: str):
    """Log away from stdout so MCP stdio JSON-RPC stays clean."""
    print(message, file=sys.stderr, flush=True)


class CodeIndexer:
    def __init__(self, workspace_root: str, conn=None):
        self.root = workspace_root
        self.store = get_store_v2()
        if conn is not None:
            self._dedicated_conn = conn
            self._shared_conn = True
        else:
            from core.runtime_config import brain_db_path
            from core.memory.db_conn import open_sqlite
            self._dedicated_conn = open_sqlite(str(brain_db_path()))
            self._shared_conn = False

        self.supported_ext = {
            '.py': 'python',
            '.ts': 'typescript',
            '.tsx': 'tsx',
            '.js': 'javascript',
            '.jsx': 'jsx',
            '.rs': 'rust',
            '.kt': 'kotlin',
            '.kts': 'kotlin',
            '.dart': 'dart',
        }
        
        self.parsers = {}
        self.languages = {}
        
        if TS_AVAILABLE:
            self._init_parsers()
            
        self.import_resolver = ImportResolver(self.root)
        self.call_graph = CallGraphBuilder()

    @property
    def conn(self):
        if hasattr(self, "_dedicated_conn") and self._dedicated_conn is not None:
            from core.memory.store_v2 import get_store_v2
            try:
                global_store = get_store_v2()
            except Exception:
                global_store = None
            if self.store is not global_store and self.store is not None:
                return self.store.conn
            return self._dedicated_conn
        return self.store.conn

    def _init_parsers(self):
        """Initialize tree-sitter parsers for supported languages."""
        
        try:
            import tree_sitter_python as tspython
            self.languages['python'] = Language(tspython.language())
            self.parsers['python'] = Parser(self.languages['python'])
        except ImportError:
            _log("Failed to load tree-sitter-python")
            
        try:
            import tree_sitter_javascript as tsjs
            self.languages['javascript'] = Language(tsjs.language())
            self.parsers['javascript'] = Parser(self.languages['javascript'])
            self.languages['jsx'] = self.languages['javascript']
            self.parsers['jsx'] = self.parsers['javascript']
        except ImportError:
            _log("Failed to load tree-sitter-javascript")
            
        try:
            import tree_sitter_typescript as tsts
            # tree-sitter-typescript provides both ts and tsx
            self.languages['typescript'] = Language(tsts.language_typescript())
            self.parsers['typescript'] = Parser(self.languages['typescript'])
            self.languages['tsx'] = Language(tsts.language_tsx())
            self.parsers['tsx'] = Parser(self.languages['tsx'])
        except ImportError:
            _log("Failed to load tree-sitter-typescript")
            
        try:
            import tree_sitter_rust as tsrust
            self.languages['rust'] = Language(tsrust.language())
            self.parsers['rust'] = Parser(self.languages['rust'])
        except ImportError:
            _log("Failed to load tree-sitter-rust")

        # Kotlin (KMP) — standalone tree-sitter-kotlin package.
        try:
            import tree_sitter_kotlin as tskotlin
            self.languages['kotlin'] = Language(tskotlin.language())
            self.parsers['kotlin'] = Parser(self.languages['kotlin'])
        except Exception:
            _log("Failed to load tree-sitter-kotlin")

        # Dart (Flutter) — no standalone wheel on PyPI; pull the grammar from
        # tree-sitter-language-pack but drive it with OUR Parser (its bundled
        # parser wrapper expects str, ours takes bytes like every other lang).
        try:
            from tree_sitter_language_pack import get_language as _tslp_get
            self.languages['dart'] = _tslp_get('dart')
            self.parsers['dart'] = Parser(self.languages['dart'])
        except Exception:
            _log("Failed to load tree-sitter dart (language-pack)")

    def scan_all(self, progress_callback=None):
        """
        Full workspace scan with incremental updates.

        progress_callback (optional): function called with a dict like
            { "stage": "scanning"|"parsing"|"linking"|"analyzing"|"done",
              "current": int, "total": int,
              "current_file": str | None,
              "percent": float (0..100) }
        Used by the REST API to expose live progress.
        """
        def _emit(stage, current=0, total=0, current_file=None):
            if progress_callback is None:
                return
            try:
                pct = (current / total * 100) if total else 0.0
                progress_callback({
                    "stage": stage,
                    "current": current,
                    "total": total,
                    "current_file": current_file,
                    "percent": round(pct, 1),
                })
            except Exception:
                pass

        try:

            if not TS_AVAILABLE:
                err_detail = f" (root cause: {_TS_IMPORT_ERR})" if _TS_IMPORT_ERR else ""
                _log(
                    f"❌ [CodeIndexer] tree-sitter not available{err_detail}. "
                    f"Run: pip install -r requirements-phase5.txt"
                )
                _emit("error")
                return

            _log(f"🔍 [CodeIndexer] Scanning workspace: {self.root}")
            _emit("scanning")

            # 1. Get existing file hashes
            cursor = self.conn.cursor()
            cursor.execute("SELECT DISTINCT file_path, file_hash FROM code_index")
            existing_hashes = {row[0]: row[1] for row in cursor.fetchall()}

            current_files = set()
            to_index = []

            # 2. Walk workspace
            #
            # Skip dotfiles + build/dep artifact dirs. `sidecar-bundle` is the
            # frozen PyInstaller copy of our own backend + every vendored dep
            # (fastmcp, the MCP SDK, ...); indexing it duplicated our source and
            # buried real hits under ~11k vendored symbols (~41% of the index),
            # so `find_relevant_code` surfaced SDK internals before our code.
            # `.pyinstaller-work`, `.tier2-stage`, `.venv` etc. are already
            # caught by the dotfile check; only the non-dotted artifact dirs
            # need listing here.
            for root, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {
                    'node_modules', 'dist', 'dist-ssr', 'dist-tier2', 'target',
                    'sidecar-bundle', '__pycache__', 'build',
                }]

                for file in files:
                    ext = os.path.splitext(file)[1]
                    if ext in self.supported_ext:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, self.root)
                        current_files.add(rel_path)

                        try:
                            with open(full_path, 'rb') as f:
                                content_bytes = f.read()

                            file_hash = hashlib.md5(content_bytes).hexdigest()

                            if rel_path not in existing_hashes or existing_hashes[rel_path] != file_hash:
                                to_index.append((rel_path, content_bytes, file_hash, ext))
                        except Exception as e:
                            _log(f"Failed to read {rel_path}: {e}")

            # 3. Remove files that have actually been deleted from THIS project.
            # Pre-Phase 5.12.4 we treated any row in code_index whose path
            # wasn't in `current_files` as deleted — which silently wiped a
            # second registered project's rows whenever the user reindexed
            # the first one (the rel_paths from project B simply weren't in
            # the walk of project A). Now we scope deletion to files whose
            # relative path resolves to an actual file inside `self.root`.
            # Rows whose rel_path does not exist under self.root almost
            # certainly belong to a different registered project and must
            # be left alone.
            managed_files = {
                p for p in existing_hashes
                if os.path.exists(os.path.join(self.root, p))
            }
            deleted_files = managed_files - current_files
            if deleted_files:
                _log(f"🗑️ [CodeIndexer] Removing {len(deleted_files)} deleted files...")
                self._remove_files(list(deleted_files))

            # 4. Parse changed files
            if not to_index:
                _log("✅ [CodeIndexer] Up to date. No changes detected.")
                _emit("done", 0, 0)
                return

            total = len(to_index)
            _log(f"⚙️ [CodeIndexer] Parsing {total} modified/new files...")

            all_symbols = []
            all_imports = []

            for i, (rel_path, content_bytes, file_hash, ext) in enumerate(to_index, start=1):
                lang_name = self.supported_ext[ext]
                _emit("parsing", i, total, rel_path)

                # Remove old entries for this file before parsing new ones
                if rel_path in existing_hashes:
                    self._remove_files([rel_path])

                symbols, imports = self._parse_file(rel_path, content_bytes, file_hash, lang_name)
                all_symbols.extend(symbols)

                # Resolve imports
                resolved = self.import_resolver.resolve(rel_path, imports, lang_name)
                all_imports.extend([{"source_file": rel_path, **imp} for imp in resolved if imp.get("resolved_path")])

            import time as _bt
            def _blap(label, t0):
                _log(f"[linking-timing] {label}: {_bt.perf_counter() - t0:.2f}s")

            # 5. Save symbols
            _emit("linking", total, total, "saving symbols")
            _t = _bt.perf_counter(); self._save_symbols(all_symbols); _blap("save_symbols", _t)

            # 6. Save imports as links
            _t = _bt.perf_counter(); self._save_import_links(all_imports); _blap("save_import_links", _t)

            # 7. Rebuild call graph
            _emit("linking", total, total, "rebuilding call graph")
            _log("🔗 [CodeIndexer] Rebuilding call graph...")
            _t = _bt.perf_counter(); self._rebuild_call_graph(); _blap("rebuild_call_graph", _t)

            # 7a. Synthesize cross-language HTTP edges (Phase 5.12.4).
            # AST-only call graphs miss `fetch("/functions/v1/notify")`-style
            # invocations of Edge Functions because the link is a string,
            # not a function call. Pattern-match those URLs and add edges
            # from the calling symbol to the Edge Function endpoint.
            try:
                _t = _bt.perf_counter(); self._synthesize_http_edges(); _blap("synthesize_http_edges", _t)
            except Exception as e:
                _log(f"⚠️  [CodeIndexer] HTTP edge synthesis failed: {e}")

            # 7b. Generate human-readable summary notes ("AST juicer")
            try:
                _emit("analyzing", total, total, "summarizing project")
                _log("📝 [CodeIndexer] Distilling summary notes...")
                from core.code_indexer.project_summarizer import ProjectSummarizer
                summarizer = ProjectSummarizer(self.root, self.conn)
                summary_stats = summarizer.summarize()
                _log(f"   ✓ Created {summary_stats.get('notes_created', 0)} summary notes "
                     f"in {summary_stats.get('folder_path')} "
                     f"({summary_stats.get('modules_detected', 0)} modules)")
            except Exception as e:
                _log(f"   ⚠ Summarizer failed (non-fatal): {e}")
        
            # 8. Generate Project Overview and Save to Memory (Phase 5.2 update)
            _log("📊 [CodeIndexer] Generating project overview...")
            analyzer = ProjectAnalyzer(self.root)
            overview = analyzer.analyze(self.conn)
        
            # Save to technical index
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO code_index (id, file_path, symbol_name, symbol_type, content, body, language, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"project_overview:{self.root}", 
                self.root, 
                "Project Overview", 
                "overview", 
                json.dumps(overview), 
                self._format_overview_markdown(overview),
                "markdown",
                datetime.now().isoformat()
            ))
        
            # ALSO save as a human-readable "Memory" so user can see it in the Brain list
            self._save_overview_to_memories(overview)

            # 9. Sync code_links → relationships so graph view sees module deps.
            # Without this, every re-index leaves stale orphan code_dep rows
            # (the new code_summary memories have new UUIDs, old edges break).
            try:
                from core.code_indexer.relationship_sync import sync_module_relationships
                n = sync_module_relationships(self.conn)
                _log(f"🔗 [CodeIndexer] Synced {n} module dep edges to graph")
            except Exception as e:
                _log(f"   ⚠ Relationship sync failed (non-fatal): {e}")

            self.conn.commit()
            _log(f"✅ [CodeIndexer] Indexed {len(to_index)} files successfully.")

            # Terminal stage marker. Without this, the only `done` emit lived in
            # the no-changes early-return above — so a real indexing run never
            # closed the "analyzing" phase, and bench_indexer_scale folded the
            # summarizer + ProjectAnalyzer cost invisibly into "linking". Emitting
            # it here gives the analyzing phase its own measured delta.
            _emit("done", total, total)

            # Reclaim the WAL after the heavy write burst. wal_autocheckpoint is
            # PASSIVE — it only reclaims frames behind the oldest active reader, so
            # under Cosmos's multi-process model (app sidecar + N MCP servers all
            # on one brain.db) a long-lived reader can starve it and let the -wal
            # file balloon to hundreds of MB. A bloated WAL is exactly what makes
            # later writers (re-index, MCP writes) surface "database is locked".
            # A TRUNCATE checkpoint here forces reclaim at the natural quiet point.
            # Best-effort: if a reader holds a snapshot we just get busy=1 and move
            # on — it must never fail the index it just completed.
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                _log(f"   ⚠ WAL checkpoint after index skipped (non-fatal): {e}")

        finally:
            if not getattr(self, "_shared_conn", True) and hasattr(self, "_dedicated_conn") and self._dedicated_conn is not None:
                try:
                    self._dedicated_conn.close()
                except Exception:
                    pass

    def _format_overview_markdown(self, overview: Dict) -> str:
        lines = [f"# 🧠 Project Intelligence Report: {os.path.basename(self.root)}\n"]
        
        lines.append("## 🏗️ Architecture & Stack")
        lines.append(f"- **Primary Frameworks**: {', '.join(overview.get('frameworks', []))}")
        lines.append(f"- **Project Scale**: {overview.get('stats', {}).get('total_files', 0)} files | {overview.get('stats', {}).get('total_functions', 0)} functions | {overview.get('stats', {}).get('total_classs', 0)} classes")
        
        lines.append("\n## 🎯 Core Components (High Centrality)")
        lines.append("These symbols are most heavily used throughout the project:")
        for comp in overview.get('core_components', []):
            lines.append(f"- **{comp['name']}** ({comp['calls']} callers) in `{comp['file']}`")
            
        lines.append("\n## 🔗 Module Dependencies")
        lines.append("Major cross-module relationship clusters detected:")
        for dep in overview.get('dependencies', []):
            lines.append(f"- `{dep['source']}` → depends on → `{dep['target']}` ({dep['count']} links)")

        lines.append("\n## 📂 Module Breakdown")
        modules = overview.get('modules', {})
        sorted_mods = sorted(modules.items(), key=lambda x: x[1], reverse=True)
        for mod, count in sorted_mods:
            if mod != 'root':
                lines.append(f"- **{mod}/**: {count} symbols")

        lines.append("\n## 🚀 Entry Points")
        for ep in overview.get('entry_points', []):
            lines.append(f"- `{ep}`")
            
        lines.append(f"\n*Last analyzed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        return "\n".join(lines)

    def _save_overview_to_memories(self, overview: Dict):
        """Save a summary of the project to the main memories_v2 table."""
        content = self._format_overview_markdown(overview)
        category = "note"
        folder_path = "/Code"
        
        # Create folder if not exists
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM folders WHERE path = ?", (folder_path,))
        row = cursor.fetchone()
        if not row:
            import uuid
            folder_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO folders (id, name, path, parent_id) VALUES (?, ?, ?, ?)", (folder_id, "Code", folder_path, None))
        else:
            folder_id = row[0]
            
        # Insert memory
        mid = f"code_overview_{hashlib.md5(self.root.encode()).hexdigest()[:8]}"
        now = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT OR REPLACE INTO memories_v2 (id, content, category, folder_id, importance_score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (mid, content, category, folder_id, 0.8, now, now))

        # Mirror into the FTS index. This raw path bypasses StoreV2, which is the
        # only place that hand-syncs memories_fts — without this the overview note
        # is silently invisible to brain_search (the FTS-drift bug). Delete-then-
        # insert mirrors StoreV2._sync_fts_update so re-indexing stays idempotent.
        from core.memory.search import pre_tokenize
        cursor.execute("DELETE FROM memories_fts WHERE id = ?", (mid,))
        cursor.execute(
            "INSERT INTO memories_fts (id, content, tags, folder_path, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, pre_tokenize(content), "", pre_tokenize(folder_path), category),
        )

    def _parse_file(self, rel_path: str, content_bytes: bytes, file_hash: str, lang_name: str) -> Tuple[List[Dict], List[Dict]]:
        """Parse a single file using tree-sitter."""
        parser = self.parsers.get(lang_name)
        lang = self.languages.get(lang_name)

        if not parser or not lang:
            return [], []

        tree = parser.parse(content_bytes)
        root_node = tree.root_node

        queries = QUERIES.get(lang_name, {})

        # 1. Extract definitions (Always use manual traversal for robustness)
        symbols = self._extract_symbols_manual(root_node, content_bytes, rel_path, file_hash, lang_name)

        # 1b. File-level fallback symbol — guarantees ALL file content is searchable
        # via FTS even when the AST query misses (e.g., typed React.FC<Props> arrow
        # functions, JSX text strings, comments, config files). Without this, files
        # that yield 0 symbols disappear from search entirely.
        try:
            file_content = content_bytes.decode('utf8', errors='replace')
            now = datetime.now().isoformat()
            file_id = hashlib.md5(f"file:{rel_path}".encode()).hexdigest()
            # Lightweight signature: first non-empty line (often a doc comment or
            # the primary export name) — keeps search results readable.
            sig_line = next(
                (ln.strip() for ln in file_content.splitlines() if ln.strip()),
                rel_path,
            )[:120]
            symbols.append({
                "id": file_id,
                "file_path": rel_path,
                "file_hash": file_hash,
                "symbol_name": os.path.basename(rel_path),
                "symbol_type": "file",
                "scope": "",
                "content": sig_line,
                "body": file_content,
                "start_line": 1,
                "end_line": file_content.count('\n') + 1,
                "language": lang_name,
                "metadata": {"is_file_level": True},
                "updated_at": now,
            })
        except Exception as e:
            _log(f"File-level index failed for {rel_path}: {e}")

        # 2. Extract imports
        imports = []
        if "imports" in queries:
            try:
                q = Query(lang, queries["imports"])
                qc = QueryCursor(q)
                captures = qc.captures(root_node)

                for name, nodes in captures.items():
                    if name in ("import.path", "import.source", "import.module"):
                        for node in nodes:
                            imports.append({"path": content_bytes[node.start_byte:node.end_byte].decode('utf8')})
            except Exception as e:
                _log(f"Import extraction error in {rel_path}: {e}")

        return symbols, imports
        
    def _extract_symbols_manual(self, root_node, content_bytes, rel_path, file_hash, lang_name):
        """Fallback simpler extraction iterating through top-level/class-level nodes."""
        symbols = []
        now = datetime.now().isoformat()
        
        def process_node(node, scope=""):
            if node.type in ("function_definition", "function_declaration", "function_item", "method_definition"):
                # Find name
                name_node = None
                for child in node.children:
                    if child.type == "identifier" or child.type == "property_identifier":
                        name_node = child
                        break

                if name_node:
                    name = content_bytes[name_node.start_byte:name_node.end_byte].decode('utf8')
                    body = content_bytes[node.start_byte:node.end_byte].decode('utf8')

                    sym_type = "method" if scope else "function"
                    sym_id = hashlib.md5(f"{rel_path}:{scope}:{name}".encode()).hexdigest()

                    # Extract calls within this function
                    calls = self._extract_calls(node, content_bytes, lang_name)

                    symbols.append({
                        "id": sym_id,
                        "file_path": rel_path,
                        "file_hash": file_hash,
                        "symbol_name": name,
                        "symbol_type": sym_type,
                        "scope": scope,
                        "content": body.split('\\n')[0], # Just signature for overview
                        "body": body,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "language": lang_name,
                        "metadata": {"calls": calls},
                        "updated_at": now
                    })

                    # Recurse into the function body — captures nested defs
                    # (FastAPI route handlers inside create_v2_app, closures,
                    # lazy loaders). Without this, files like rest_server_v2.py
                    # showed only 11 top-level symbols and hid 50+ route
                    # handlers from skeleton/search.
                    inner_scope = f"{scope}.{name}" if scope else name
                    for child in node.children:
                        if child.type in ("block", "statement_block", "function_body"):
                            for inner in child.children:
                                process_node(inner, scope=inner_scope)


            elif node.type in ("class_definition", "class_declaration", "struct_item"):
                name_node = None
                for child in node.children:
                    if child.type == "identifier" or child.type == "type_identifier":
                        name_node = child
                        break
                        
                if name_node:
                    name = content_bytes[name_node.start_byte:name_node.end_byte].decode('utf8')
                    body = content_bytes[node.start_byte:node.end_byte].decode('utf8')
                    
                    sym_id = hashlib.md5(f"{rel_path}::{name}".encode()).hexdigest()
                    
                    symbols.append({
                        "id": sym_id,
                        "file_path": rel_path,
                        "file_hash": file_hash,
                        "symbol_name": name,
                        "symbol_type": "class",
                        "scope": "",
                        "content": body.split('\\n')[0],
                        "body": body,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "language": lang_name,
                        "metadata": {},
                        "updated_at": now
                    })
                    
                    # Process methods inside class. Each language tree-sitter
                    # grammar names the class body differently:
                    #   - Python  → `block`
                    #   - Rust    → `declaration_list`
                    #   - TS/JS   → `class_body`   ← was missing pre-Phase 5.12.2,
                    #     which is why TypeScript classes (e.g. cline's
                    #     SqliteLockManager) showed up with zero methods in
                    #     code_skeleton output.
                    for child in node.children:
                        if child.type in ("block", "declaration_list", "class_body"):
                            for method_node in child.children:
                                process_node(method_node, scope=name)
                                
            else:
                # Traverse deeper for TS/JS exports, Rust impl blocks, and
                # Python `decorated_definition` (e.g. @app.post(...) handlers).
                # Without `decorated_definition`, every FastAPI route handler
                # was invisible to the indexer.
                if node.type in ("export_statement", "lexical_declaration",
                                 "impl_item", "decorated_definition"):
                    for child in node.children:
                        process_node(child, scope)

        # Start traversal
        for child in root_node.children:
            process_node(child)
            
        return symbols

    def _extract_calls(self, node, content_bytes, lang_name) -> List[str]:
        """Extract function names called within a node.

        Returns a list where each entry is either:
          - "name"               for a free-function call: foo()
          - "receiver.name"      for a member call: obj.foo() / obj.x.foo()
          - "scope::name"        for Rust scoped calls: Module::foo()

        The dotted form is what `CallGraphBuilder` uses to disambiguate
        between several globally-named functions (e.g. `log` defined in
        both `Logger` and a test fixture). Pre-Phase 5.12.3 we stored
        only the leaf, so the resolver had no way to tell them apart.
        """
        lang = self.languages.get(lang_name)
        queries = QUERIES.get(lang_name, {})

        if not lang or "calls" not in queries:
            return []

        calls: set[str] = set()
        try:
            q = Query(lang, queries["calls"])
            qc = QueryCursor(q)
            captures = qc.captures(node)

            # Track which member-call nodes we already captured as a chain
            # so we don't also emit the bare leaf name from
            # `call.method_name` (which would double-count).
            chain_ranges: set[tuple[int, int]] = set()

            # 1. Pull the full chains first.
            for cap_node in captures.get("call.member_chain", []):
                text = content_bytes[
                    cap_node.start_byte:cap_node.end_byte
                ].decode("utf8").strip()
                # Strip arg lists if any leaked in (defensive — query targets
                # member_expression / attribute, not the call_expression).
                text = text.split("(", 1)[0].strip()
                if text:
                    calls.add(text)
                    chain_ranges.add((cap_node.start_byte, cap_node.end_byte))

            # 2. Then bare names — but skip any whose member-chain wrapper
            #    we already recorded (avoid double-emit).
            for name in ("call.func_name", "call.method_name", "call.scoped"):
                for cap_node in captures.get(name, []):
                    leaf = content_bytes[
                        cap_node.start_byte:cap_node.end_byte
                    ].decode("utf8")
                    # Is this leaf inside a member chain we already kept?
                    swallowed = any(
                        cs <= cap_node.start_byte and cap_node.end_byte <= ce
                        for cs, ce in chain_ranges
                    )
                    if not swallowed:
                        calls.add(leaf)
        except Exception:
            pass

        return list(calls)

    def _remove_files(self, paths: List[str]):
        """Remove symbols and links for specific files."""
        if not paths: return
        cursor = self.conn.cursor()
        
        for path in paths:
            # Get IDs to delete from links
            cursor.execute("SELECT id FROM code_index WHERE file_path = ?", (path,))
            ids = [r[0] for r in cursor.fetchall()]
            
            for sym_id in ids:
                cursor.execute("DELETE FROM code_links WHERE source_id = ? OR target_id = ?", (sym_id, sym_id))
                
            cursor.execute("DELETE FROM code_index WHERE file_path = ?", (path,))
            cursor.execute("DELETE FROM code_fts WHERE file_path = ?", (path,))
            
        self.conn.commit()

    def _save_symbols(self, symbols: List[Dict]):
        """Save symbols to DB and sync FTS — batched via executemany.

        Two round-trips into SQLite instead of 3N: one INSERT OR REPLACE
        across code_index, one INSERT across code_fts. On a 10K-symbol scan
        this drops total insert wall time by ~10-20× and — importantly —
        collapses the lock-acquire count, slashing contention with concurrent
        readers (UI search, MCP brain queries) during indexing.

        No per-id `DELETE FROM code_fts` is needed first: scan_all calls
        _remove_files (which deletes code_fts by file_path) for every
        previously-seen file BEFORE this runs, and brand-new files have no
        FTS rows. The old per-id delete was therefore 100% redundant — a
        no-op FTS5 lookup per symbol (108k at 50k-file scale) that bought
        nothing.
        """
        if not symbols: return
        cursor = self.conn.cursor()

        # FTS Sync — pre-tokenize for Thai content. Without this, FTS5's
        # default unicode61 tokenizer treats whole Thai phrases as one
        # token and searches like "ตัวอักษรไทย" return 0 hits even when
        # the file contains the exact phrase.
        from core.memory.store_v2 import pre_tokenize as _pretok

        index_rows = [
            (
                sym["id"], sym["file_path"], sym["file_hash"], sym["symbol_name"],
                sym["symbol_type"], sym.get("scope", ""), sym["content"], sym.get("body", ""),
                sym.get("language", ""), sym["start_line"], sym["end_line"],
                json.dumps(sym["metadata"]), sym["updated_at"],
            )
            for sym in symbols
        ]
        cursor.executemany("""
            INSERT OR REPLACE INTO code_index
            (id, file_path, file_hash, symbol_name, symbol_type, scope, content, body, language, start_line, end_line, metadata, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, index_rows)

        fts_rows = [
            (
                sym["id"], sym["symbol_name"], sym["file_path"],
                _pretok(sym.get("body", sym["content"])),
                _pretok(sym.get("docstring", "")),
            )
            for sym in symbols
        ]
        cursor.executemany("""
            INSERT INTO code_fts (id, symbol_name, file_path, content, docstring)
            VALUES (?, ?, ?, ?, ?)
        """, fts_rows)

        self.conn.commit()

    def _save_import_links(self, imports: List[Dict]):
        """Save resolved imports as file-level links — batched."""
        if not imports: return
        cursor = self.conn.cursor()

        # Use path hashes as IDs for file-level links.
        rows = [
            (
                hashlib.md5(f"file:{imp['source_file']}".encode()).hexdigest(),
                hashlib.md5(f"file:{imp['resolved_path']}".encode()).hexdigest(),
            )
            for imp in imports
        ]
        cursor.executemany("""
            INSERT OR IGNORE INTO code_links (source_id, target_id, link_type)
            VALUES (?, ?, 'import')
        """, rows)

        self.conn.commit()

    def _synthesize_http_edges(self):
        """Cross-language HTTP edge synthesis.

        AST call graphs miss code that invokes one service from another
        through an HTTP URL (e.g. stripe-webhook calling notify via
        `fetch("/functions/v1/notify")`). Pre-Phase 5.12.4, code_callers
        on the notify Edge Function returned only its in-language callers
        and the cline benchmark logged this as the headline gap.

        For each Supabase Edge Function file we find on disk
        (path matches `**/functions/<name>/index.ts`), insert an
        `endpoint` symbol named after its directory. Then scan every
        symbol body for the URL pattern `/functions/v1/<name>` and add a
        synthetic `call` link from each containing symbol to the matching
        endpoint. The result: code_find_callers("notify") now lists
        stripe-webhook + cron-trial-reminders alongside any in-process
        callers.
        """
        import re
        cursor = self.conn.cursor()

        # 1. Discover Edge Function files. Match supabase/functions/<x>/index.ts
        #    plus the no-supabase-prefix shape that some users keep.
        cursor.execute(
            "SELECT DISTINCT file_path FROM code_index "
            "WHERE file_path LIKE '%/functions/%/index.ts' "
            "   OR file_path LIKE 'functions/%/index.ts'"
        )
        edge_files = [row[0] for row in cursor.fetchall()]
        if not edge_files:
            return

        endpoint_ids: dict[str, str] = {}
        path_re = re.compile(r"functions/([^/]+)/index\.ts$")
        for fp in edge_files:
            m = path_re.search(fp)
            if not m:
                continue
            name = m.group(1)
            sym_id = hashlib.md5(f"{fp}::endpoint::{name}".encode()).hexdigest()
            cursor.execute("""
                INSERT OR REPLACE INTO code_index
                  (id, file_path, file_hash, symbol_name, symbol_type,
                   scope, content, body, start_line, end_line, language,
                   metadata, updated_at)
                VALUES (?, ?, '', ?, 'endpoint', '',
                        ?, '', 1, 1, 'http', '{}', CURRENT_TIMESTAMP)
            """, (sym_id, fp, name, f"POST /functions/v1/{name}"))
            endpoint_ids[name] = sym_id

        if not endpoint_ids:
            return

        # 2. Drop existing synthetic links so a re-run doesn't pile up
        #    duplicates with stale source ids (the source symbols may have
        #    been re-hashed if the caller file changed).
        cursor.execute(
            "DELETE FROM code_links "
            "WHERE link_type = 'call' AND target_id IN ({})".format(
                ",".join("?" * len(endpoint_ids))
            ),
            list(endpoint_ids.values()),
        )

        # 3. For each endpoint, find symbols whose body mentions its URL.
        url_pattern_re = re.compile(r"/functions/v1/([\w-]+)")
        cursor.execute(
            "SELECT id, body FROM code_index "
            "WHERE symbol_type IN ('function', 'method', 'route') "
            "  AND body IS NOT NULL"
        )
        added = 0
        for caller_id, body in cursor.fetchall():
            if not body:
                continue
            for match in url_pattern_re.finditer(body):
                target_name = match.group(1)
                target_id = endpoint_ids.get(target_name)
                if not target_id or target_id == caller_id:
                    continue
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO code_links "
                        "(source_id, target_id, link_type) VALUES (?, ?, 'call')",
                        (caller_id, target_id),
                    )
                    if cursor.rowcount > 0:
                        added += 1
                except Exception:
                    pass

        self.conn.commit()
        if added:
            _log(
                f"🌐 [CodeIndexer] Synthesized {added} HTTP edge(s) "
                f"across {len(endpoint_ids)} Edge Function endpoint(s)."
            )

    def _rebuild_call_graph(self):
        """Rebuild all call links based on latest symbols."""
        cursor = self.conn.cursor()
        
        # Clear existing calls
        cursor.execute("DELETE FROM code_links WHERE link_type = 'call'")
        
        # Single full-table pass builds BOTH structures the call graph needs:
        # the name→definitions index (every symbol) and the caller list
        # (function/method/route rows that recorded calls in metadata).
        # Previously this was two separate full scans of code_index — at
        # 100k+ symbols the second scan was a wasted table read.
        cursor.execute("SELECT id, symbol_name, file_path, symbol_type, metadata FROM code_index")
        defs_by_name = {}
        callers = []
        for sym_id, name, file_path, symbol_type, metadata in cursor.fetchall():
            defs_by_name.setdefault(name, []).append({
                "id": sym_id, "file_path": file_path, "type": symbol_type
            })

            if symbol_type in ("function", "method", "route") and metadata:
                try:
                    meta = json.loads(metadata)
                except Exception:
                    continue
                if meta.get("calls"):
                    callers.append({
                        "id": sym_id,
                        "type": "function",  # simplified
                        "file_path": file_path,  # lets build_links narrow ambiguous calls to the caller's own file
                        "meta": meta,
                    })

        # Build links
        links = self.call_graph.build_links(callers, defs_by_name)
        
        # Save links — batched.
        cursor.executemany("""
            INSERT OR IGNORE INTO code_links (source_id, target_id, link_type)
            VALUES (?, ?, 'call')
        """, [(link["source_id"], link["target_id"]) for link in links])

        self.conn.commit()
