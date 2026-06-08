"""
Project Summarizer — "AST Juicer"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

After raw indexing populates `code_index` + `code_links`, this module
distills the project into a small set of human-readable Markdown notes
saved as memories under `/Code/{project_name}/`.

Hierarchy produced (depending on project type):

  /Code/{project}/
    ├── Architecture.md      — overview, languages, frameworks, stats
    ├── Entry Points.md      — main files / runners
    ├── Key Symbols.md       — top-referenced functions/classes
    └── Modules/
        ├── {Module}.md      — auto-clustered by top-level src/lib folder
        └── ...

Tier behaviour:
  - Tier 0  : template-based bullet summaries (this file)
  - Tier 2+ : optional LLM upgrade (see render_with_llm helper)
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import List, Optional


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────


def _log(message: str):
    """Log away from stdout so MCP stdio JSON-RPC stays clean."""
    print(message, file=sys.stderr, flush=True)


def _slugify_module_name(raw: str) -> str:
    """Turn a folder segment into a clean module title — Auth, Payment, etc."""
    cleaned = raw.replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in cleaned.split() if word)


def _detect_module_for_path(rel_path: str) -> str:
    """
    Decide which logical module a file belongs to. Strategy:
      1. Strip common roots (src/, lib/, app/, packages/...)
      2. Take the next folder segment as module — that's the user's intent
      3. If file is at workspace root → 'Root'
    """
    parts = rel_path.replace("\\", "/").split("/")
    common_roots = {"src", "lib", "app", "packages", "core", "components", "modules", "internal"}

    # Skip leading common roots
    while parts and parts[0] in common_roots:
        parts = parts[1:]

    if len(parts) == 0:
        return "Root"
    if len(parts) == 1:
        # Just a top-level file
        return "Root"
    return _slugify_module_name(parts[0])


# ──────────────────────────────────────────────────────────
# ProjectSummarizer
# ──────────────────────────────────────────────────────────

class ProjectSummarizer:
    """
    Produces a curated set of markdown memories that "summarize" a code index.
    Idempotent — re-running replaces the previous summary set.
    """

    SECTION_FILES = ["Architecture.md", "Entry Points.md", "Key Symbols.md"]

    def __init__(self, workspace_root: str, conn: sqlite3.Connection):
        self.root = os.path.abspath(workspace_root)
        self.conn = conn
        self.project_name = os.path.basename(self.root) or "Project"

    # ── Public entry point ──
    def summarize(self) -> dict:
        """
        Run the full summarization pipeline. Returns stats:
          { folder_path, notes_created, modules_detected, project_name }
        """
        from core.memory.folder import FolderTree
        tree = FolderTree(self.conn)

        # 1. Ensure folder hierarchy
        code_root = self._ensure_folder(tree, "Code", parent_id=None, parent_path="")
        project_folder = self._ensure_folder(
            tree, self.project_name,
            parent_id=code_root["id"], parent_path="/Code"
        )

        # 2. Read indexed data
        symbols = self._load_symbols()
        if not symbols:
            return {
                "folder_path": project_folder["path"],
                "notes_created": 0,
                "modules_detected": 0,
                "project_name": self.project_name,
                "warning": "No symbols indexed for this project — run code index first.",
            }

        # 3. Cluster by module
        modules: dict[str, list[dict]] = defaultdict(list)
        for sym in symbols:
            modules[_detect_module_for_path(sym["file_path"])].append(sym)

        # Drop trivially small modules (single root file) into 'Root'
        merged: dict[str, list[dict]] = defaultdict(list)
        for module, syms in modules.items():
            if module != "Root" and len(syms) < 2:
                merged["Root"].extend(syms)
            else:
                merged[module].extend(syms)

        # 4. Wipe previous summaries for this project (idempotent)
        self._delete_previous_summaries(project_folder["id"])

        # Compute the project overview ONCE and reuse — _render_architecture
        # and _render_entry_points both need it; calling analyze() twice (it
        # runs a batch of SQL over code_index/code_links) doubled the cost.
        from core.code_indexer.project_analyzer import ProjectAnalyzer
        try:
            overview = ProjectAnalyzer(self.root).analyze(self.conn) or {}
        except Exception:
            overview = {}

        # 5. Generate Level-1 summaries (always 3)
        notes_created = 0
        notes_created += self._save_note(
            project_folder["id"], "Architecture",
            self._render_architecture(symbols, merged, overview),
        )
        notes_created += self._save_note(
            project_folder["id"], "Entry Points",
            self._render_entry_points(symbols, overview),
        )
        notes_created += self._save_note(
            project_folder["id"], "Key Symbols",
            self._render_key_symbols(symbols),
        )

        # 6. Modules sub-folder + Level-2 module notes
        if len(merged) > 1 or (len(merged) == 1 and "Root" not in merged):
            modules_folder = self._ensure_folder(
                tree, "Modules",
                parent_id=project_folder["id"],
                parent_path=project_folder["path"],
            )
            for module_name, syms in sorted(merged.items()):
                notes_created += self._save_note(
                    modules_folder["id"], module_name,
                    self._render_module(module_name, syms),
                )

        return {
            "folder_path": project_folder["path"],
            "notes_created": notes_created,
            "modules_detected": len(merged),
            "project_name": self.project_name,
            "module_names": sorted(merged.keys()),
        }

    # ── Folder helpers ──
    def _ensure_folder(self, tree, name: str, parent_id: Optional[str], parent_path: str) -> dict:
        target_path = f"{parent_path}/{name}" if parent_path else f"/{name}"
        existing = tree.get_by_path(target_path)
        if existing:
            return existing
        return tree.create(name, parent_id=parent_id)

    # ── Data access ──
    def _load_symbols(self) -> List[dict]:
        # Do NOT pull `body`/`content` (full source) into memory — the
        # summary notes only render names/types/docstrings/paths. On a real
        # codebase (e.g. cpython: 86k symbols with large function bodies)
        # selecting full bodies ballooned RSS to ~2.7 GB and dominated the
        # "Distilling summary notes" phase. We only need the body LENGTH (as
        # a size proxy for the no-call-graph fallback), computed in SQL.
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, file_path, symbol_name, symbol_type, scope,
                   LENGTH(body) AS body_len, docstring, language, start_line, end_line
            FROM code_index
            WHERE symbol_type NOT IN ('overview', 'file')
        """)
        cols = ["id", "file_path", "symbol_name", "symbol_type", "scope",
                "body_len", "docstring", "language", "start_line", "end_line"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _load_link_counts(self) -> dict:
        """Return {symbol_id: in_degree} — how many things point at it."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT target_id, COUNT(*) FROM code_links
            GROUP BY target_id
        """)
        return {row[0]: row[1] for row in cur.fetchall()}

    def _delete_previous_summaries(self, folder_id: str):
        """Remove old summary memories under the project folder so this run replaces them."""
        cur = self.conn.cursor()
        # Clear notes directly under the project folder
        cur.execute("""
            DELETE FROM memories_v2
            WHERE folder_id = ? AND source = 'project_summarizer'
        """, (folder_id,))

        # Also clear the Modules sub-folder children if it exists
        cur.execute("""
            SELECT id FROM folders
            WHERE parent_id = ? AND name = 'Modules'
        """, (folder_id,))
        row = cur.fetchone()
        if row:
            cur.execute("""
                DELETE FROM memories_v2
                WHERE folder_id = ? AND source = 'project_summarizer'
            """, (row[0],))
        self.conn.commit()

    def _save_note(self, folder_id: str, title: str, content: str) -> int:
        """Save a markdown note to memories_v2. Returns 1 on success.

        Race-safe idempotency: deletes any previous note with the same title in
        this folder before inserting. Survives overlapping summarize() runs
        where the coarse-grained _delete_previous_summaries from one run
        commits after another run's INSERT.
        """
        from core.memory.store_v2 import get_store_v2
        store = get_store_v2()
        try:
            cur = store.conn.cursor()
            cur.execute("""
                DELETE FROM memories_v2
                WHERE folder_id = ?
                  AND source = 'project_summarizer'
                  AND json_extract(typed_data, '$.title') = ?
            """, (folder_id, title))
            cur.execute("""
                DELETE FROM memories_fts
                WHERE id NOT IN (SELECT id FROM memories_v2)
            """)
            store.conn.commit()

            store.store(
                content=content,
                category="code_summary",
                tags=["code_summary", self.project_name.lower()],
                folder_id=folder_id,
                source="project_summarizer",
                typed_data={"title": title, "project": self.project_name},
            )
            return 1
        except Exception as e:
            _log(f"[summarizer] failed to save '{title}': {e}")
            return 0

    # ──────────────────────────────────────────────
    # Renderers (Tier 0 — template-based)
    # ──────────────────────────────────────────────

    def _render_architecture(self, symbols: List[dict], modules: dict, overview: dict = None) -> str:
        # overview is computed once in summarize() and passed in. Fall back to
        # computing it here only if called standalone.
        if overview is None:
            from core.code_indexer.project_analyzer import ProjectAnalyzer
            try:
                overview = ProjectAnalyzer(self.root).analyze(self.conn) or {}
            except Exception:
                overview = {}

        # Stats
        by_lang = Counter(s["language"] for s in symbols if s.get("language"))
        by_type = Counter(s["symbol_type"] for s in symbols if s.get("symbol_type"))
        files = {s["file_path"] for s in symbols}

        lines = [
            f"# 🏛 Architecture — {self.project_name}\n",
            f"**Path:** `{self.root}`",
            f"**Indexed:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "## 📊 Stats",
            f"- Total files indexed: **{len(files)}**",
            f"- Total symbols: **{len(symbols)}**",
        ]

        if by_lang:
            top_langs = ", ".join(f"{lang} ({n})" for lang, n in by_lang.most_common())
            lines.append(f"- Languages: {top_langs}")
        if by_type:
            type_summary = ", ".join(f"{t} ({n})" for t, n in by_type.most_common())
            lines.append(f"- Symbol kinds: {type_summary}")

        if overview.get("frameworks"):
            lines.append(f"- Frameworks detected: **{', '.join(overview['frameworks'])}**")

        # Modules
        if modules:
            lines.append("\n## 🧩 Modules")
            sorted_mods = sorted(modules.items(), key=lambda x: -len(x[1]))
            for name, syms in sorted_mods[:12]:
                lines.append(f"- **{name}** — {len(syms)} symbol(s)")

        # Entry points teaser
        if overview.get("entry_points"):
            lines.append("\n## 🚪 Entry Points")
            for ep in overview["entry_points"][:5]:
                lines.append(f"- `{ep}`")

        # Design & styling integration (Phase 5.13)
        design_ctx = overview.get("design_context")
        if design_ctx:
            try:
                from core.code_indexer.preamble import _format_design_context_markdown
                design_md = _format_design_context_markdown(design_ctx)
                if design_md:
                    lines.append(design_md)
            except Exception:
                pass

        lines.append("")
        lines.append("> 🔗 ดูรายละเอียดของแต่ละ module ใน `/Code/{name}/Modules/`".format(name=self.project_name))
        lines.append("> 🤖 AI ภายนอก (Claude/Cursor) ใช้ MCP tools `code_search`, `code_get_symbol`, `code_callers`, `code_hierarchy`")
        return "\n".join(lines)

    def _render_entry_points(self, symbols: List[dict], overview: dict = None) -> str:
        if overview is None:
            from core.code_indexer.project_analyzer import ProjectAnalyzer
            try:
                overview = ProjectAnalyzer(self.root).analyze(self.conn) or {}
            except Exception:
                overview = {}

        lines = [f"# 🚪 Entry Points — {self.project_name}\n"]
        eps = overview.get("entry_points", [])
        if eps:
            lines.append("Likely entry points (auto-detected):")
            for ep in eps:
                lines.append(f"- `{ep}`")
        else:
            lines.append("_No conventional entry points detected._")

        # Files with `main` / `run` / `bootstrap` symbols
        runner_keywords = {"main", "run", "start", "bootstrap", "entry", "init"}
        runners = [
            s for s in symbols
            if s.get("symbol_name") and s["symbol_name"].lower() in runner_keywords
            and s.get("symbol_type") in ("function", "method")
        ]
        if runners:
            lines.append("\n## ⚡ Runner-style symbols")
            for s in runners[:15]:
                lines.append(f"- **{s['symbol_name']}** ({s['language']}) — `{s['file_path']}:{s.get('start_line', '?')}`")

        return "\n".join(lines)

    def _render_key_symbols(self, symbols: List[dict], top_n: int = 20) -> str:
        link_counts = self._load_link_counts()
        scored = []
        for s in symbols:
            score = link_counts.get(s["id"], 0)
            scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)

        lines = [
            f"# ⭐ Key Symbols — {self.project_name}\n",
            "Symbols ranked by how often they're referenced (in-degree of call graph):",
            "",
        ]
        if not any(score for score, _ in scored):
            lines.append("_No cross-references detected. Tier 0 indexer captured definitions but call graph is empty for this project type._")
            lines.append("")
            lines.append("## Most-prominent definitions (by file size proxy)")
            scored = sorted(symbols, key=lambda s: s.get("body_len") or 0, reverse=True)[:top_n]
            for s in scored:
                lines.append(f"- **{s['symbol_name']}** ({s['symbol_type']}, {s['language']}) — `{s['file_path']}`")
            return "\n".join(lines)

        for score, s in scored[:top_n]:
            scope = f" ({s['scope']})" if s.get("scope") else ""
            lines.append(f"- **{s['symbol_name']}**{scope} — referenced {score}× — `{s['file_path']}:{s.get('start_line', '?')}`")

        return "\n".join(lines)

    # Cap how many symbols a single module note lists. A note that
    # enumerates every symbol of a giant module (e.g. cpython's stdlib →
    # ~10k symbols) becomes an ~MB markdown blob; inserting + FTS-indexing
    # (incl. pythainlp tokenization) one of those took minutes. A 10k-line
    # note is also unreadable. We list the largest files / first symbols up
    # to the cap and summarise the remainder as a count — the AI can drill
    # in with code_get_symbol / code_search anyway.
    MAX_SYMBOLS_PER_MODULE_NOTE = 120
    MAX_FILES_PER_MODULE_NOTE = 60

    def _render_module(self, module_name: str, syms: List[dict]) -> str:
        # Group within module by file
        by_file = defaultdict(list)
        for s in syms:
            by_file[s["file_path"]].append(s)

        lines = [
            f"# 🧩 {module_name} Module — {self.project_name}\n",
            f"**Files:** {len(by_file)} · **Symbols:** {len(syms)}",
            "",
        ]

        # Distinct symbol types in this module
        types = Counter(s["symbol_type"] for s in syms)
        type_summary = ", ".join(f"{n} {t}" for t, n in types.most_common())
        lines.append(f"_Composition:_ {type_summary}")
        lines.append("")

        # Per file — bounded. Largest files first so the most significant
        # ones make the cut; both the file count and the running symbol
        # count are capped so the note stays small + fast to index.
        ordered_files = sorted(by_file.items(), key=lambda kv: len(kv[1]), reverse=True)
        shown_syms = 0
        shown_files = 0
        for fp, file_syms in ordered_files:
            if shown_files >= self.MAX_FILES_PER_MODULE_NOTE or shown_syms >= self.MAX_SYMBOLS_PER_MODULE_NOTE:
                break
            shown_files += 1
            lines.append(f"## `{fp}`")
            file_syms.sort(key=lambda s: s.get("start_line") or 0)
            for s in file_syms:
                if shown_syms >= self.MAX_SYMBOLS_PER_MODULE_NOTE:
                    lines.append(f"- … +{len(file_syms) - 0} more in this file")
                    break
                docstring = s.get("docstring") or ""
                hint = f" — {docstring.strip().splitlines()[0][:80]}" if docstring.strip() else ""
                scope = f" (in {s['scope']})" if s.get("scope") else ""
                lines.append(f"- L{s.get('start_line', '?')} **{s['symbol_name']}**{scope} `{s['symbol_type']}`{hint}")
                shown_syms += 1
            lines.append("")

        remaining_files = len(by_file) - shown_files
        if remaining_files > 0:
            lines.append(f"_… and {remaining_files} more file(s) / {len(syms) - shown_syms} more symbol(s) — "
                         f"use `code_search` / `code_get_symbol` to drill in._")

        return "\n".join(lines)


def summarize_project(workspace_root: str) -> dict:
    """Convenience function — uses default v5 store."""
    from core.memory.store_v2 import get_store_v2
    store = get_store_v2()
    summarizer = ProjectSummarizer(workspace_root, store.conn)
    return summarizer.summarize()
