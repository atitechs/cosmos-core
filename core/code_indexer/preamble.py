"""
D-layer: project preamble pipeline.

Generates a 3-tier project summary that the cosmos-connector skill
fetches at session start (Trampoline pattern from A-layer). Tiers:

  Hot   : top 100 symbols, last 10 lessons, boundary count, stats
          (<1 KB, always-loaded)
  Warm  : module map + hotspots + full lesson index (~5-15 KB,
          on-request via MCP resource)
  Cold  : full skeleton tree, all-files index (50+ KB, paginated,
          on-demand only)

Also:
  - D.5 merge into <project>/CLAUDE.md with section markers
  - D.7 within-session content-addressed cache
  - D.8 refresh trigger (called from watcher_manager when index updates)
  - D.9 pyright LSP integration — DEFERRED. Stub returns (None, "pyright
        not yet wired; falls back to regex-based boundary detection").
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional


_SESSION_CACHE: dict[str, tuple[str, str, float, str]] = {}
"""D.7 in-process cache.
Key = "<project_id>:<tier>:<index_fingerprint>"
Value = (content_hash, body, ts, index_fingerprint)

0.2.18 Issue #10 — fingerprint the current code_index snapshot in the
cache key so stale entries auto-invalidate when the indexer re-runs.
Previously the cache used just (project_id, tier) and a 5-min TTL,
which caused other tools (trace/refactor) to see fresh index data
while preamble still served the pre-reindex snapshot — visible as a
mismatched indexed_at + content_hash between preamble and trace
within the same session."""

_CACHE_TTL_SEC = 300


def _normalize_path_safe(p: str) -> str:
    try:
        return os.path.abspath(os.path.expanduser(p or ""))
    except Exception:
        return p or ""


def _index_fingerprint(conn) -> str:
    """Cheap snapshot fingerprint — total row count + max updated_at.
    Same shape as the staleness marker so cache + reported indexed_at
    stay aligned."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MAX(updated_at) FROM code_index")
        total, last_idx = cur.fetchone()
        return hashlib.sha256(f"{total}-{last_idx or 'never'}".encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _cache_get(project_id: str, tier: str, fingerprint: str) -> Optional[tuple[str, str]]:
    key = f"{project_id}:{tier}:{fingerprint}"
    entry = _SESSION_CACHE.get(key)
    if not entry:
        return None
    h, body, ts, fp = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        return None
    if fp != fingerprint:
        return None
    return h, body


def _cache_set(project_id: str, tier: str, fingerprint: str, body: str) -> str:
    h = hashlib.sha256(body.encode()).hexdigest()[:12]
    _SESSION_CACHE[f"{project_id}:{tier}:{fingerprint}"] = (h, body, time.time(), fingerprint)
    return h


def invalidate(project_id: str) -> None:
    """D.8 — Refresh trigger: called by watcher_manager when index updates."""
    for k in list(_SESSION_CACHE.keys()):
        if k.startswith(f"{project_id}:"):
            del _SESSION_CACHE[k]


# ── Tier generators ────────────────────────────────────────────────────

def _topic_scoped_section(conn, intent: str, k: int = 5) -> str:
    """Track 3.5 — augment preamble with topic-relevant symbols + lessons
    when the caller provides an `intent` string (typically the user's
    first chat turn or session-start prompt).

    Closes the niche_intent gap in active-surfacing (Track 3 result:
    cosmos preamble hit-rate was 0.0 on niche topics because the fixed
    hot-tier output didn't know what the user was about to work on).

    Uses _fts_safe_query so natural-language intents like "I'm debugging
    websocket reconnection" don't get killed by FTS5's AND-default on
    stopwords (Issue #13 lesson — same fix pattern reused).
    """
    if not (intent or "").strip():
        return ""
    try:
        from core.api.mcp_server import _fts_safe_query
        safe_q = _fts_safe_query(intent)
    except Exception:
        safe_q = intent

    out = [f"\n## Topic-relevant items for: _{intent[:80]}_"]
    cur = conn.cursor()

    # Code symbols matching intent
    try:
        cur.execute("""
            SELECT ci.symbol_name, ci.file_path, ci.symbol_type, ci.start_line
            FROM code_fts JOIN code_index ci ON ci.id = code_fts.id
            WHERE code_fts MATCH ? AND ci.symbol_type != 'file'
            ORDER BY rank LIMIT ?
        """, (safe_q, k))
        rows = cur.fetchall()
        if rows:
            out.append("### Code symbols")
            for name, fp, stype, line in rows:
                out.append(f"- `{name}` ({stype}) in {fp}:{line or '?'}")
    except Exception:
        pass

    # Brain lessons matching intent
    try:
        cur.execute("""
            SELECT mf.id, substr(mf.content, 1, 100)
            FROM memories_fts mf
            WHERE memories_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (safe_q, k))
        rows = cur.fetchall()
        if rows:
            out.append("### Brain lessons")
            for mid, snippet in rows:
                out.append(f"- [{mid[:8]}] {(snippet or '').strip()[:90]}…")
    except Exception:
        pass

    if len(out) == 1:
        return ""  # no matches — don't add an empty section
    return "\n".join(out) + "\n"


def _format_design_context_markdown(design_ctx: dict) -> str:
    if not design_ctx:
        return ""
        
    lines = ["\n## 🎨 Design & Styling Guidelines (UX/UI)"]
    
    # Frameworks
    fws = design_ctx.get("frameworks", [])
    if fws:
        lines.append(f"- **UI Tech / Stylesheets**: {', '.join(fws)}")
        
    # CSS variables - Light / Default
    css_vars = design_ctx.get("css_variables", {})
    light_vars = css_vars.get("light", {})
    dark_vars = css_vars.get("dark", {})
    
    def categorize_vars(variables: dict) -> dict:
        cats = {"Colors": {}, "Spacing / Sizing": {}, "Typography": {}, "Other": {}}
        for k, v in variables.items():
            k_lower = k.lower()
            if any(x in k_lower for x in ["color", "bg", "text", "border", "primary", "secondary", "accent", "muted", "background", "foreground", "popover", "card", "destructive", "ring", "input"]):
                cats["Colors"][k] = v
            elif any(x in k_lower for x in ["spacing", "space", "radius", "width", "height", "padding", "margin", "gap"]):
                cats["Spacing / Sizing"][k] = v
            elif any(x in k_lower for x in ["font", "text-", "line-height", "tracking"]):
                cats["Typography"][k] = v
            else:
                cats["Other"][k] = v
        return {cat: items for cat, items in cats.items() if items}

    if light_vars:
        lines.append("\n### 💡 CSS Variables (Default / Light Theme)")
        cats = categorize_vars(light_vars)
        for cat, items in cats.items():
            lines.append(f"#### {cat}")
            for k, v in sorted(items.items()):
                marker = ""
                if cat == "Colors":
                    if v.startswith("#"):
                        marker = "🎨 "
                    elif "hsl" in v.lower() or "rgb" in v.lower():
                        marker = "🎨 "
                lines.append(f"- `{k}`: {marker}`{v}`")
                
    if dark_vars:
        lines.append("\n### 🌙 CSS Variables (Dark Theme)")
        cats = categorize_vars(dark_vars)
        for cat, items in cats.items():
            lines.append(f"#### {cat}")
            for k, v in sorted(items.items()):
                marker = ""
                if cat == "Colors":
                    if v.startswith("#"):
                        marker = "🎨 "
                    elif "hsl" in v.lower() or "rgb" in v.lower():
                        marker = "🎨 "
                lines.append(f"- `{k}`: {marker}`{v}`")
                
    # Tailwind configurations
    tw = design_ctx.get("tailwind_theme", {})
    if tw:
        lines.append("\n### ⚡ Tailwind CSS Config Theme")
        
        tw_colors = tw.get("colors", {})
        if tw_colors:
            lines.append("#### Colors")
            for c_name, c_val in sorted(tw_colors.items()):
                if isinstance(c_val, dict):
                    inner_vals = ", ".join(f"{ik}: `{iv}`" for ik, iv in c_val.items())
                    lines.append(f"- `{c_name}`: {{ {inner_vals} }}")
                else:
                    lines.append(f"- `{c_name}`: `{c_val}`")
                    
        tw_spacing = tw.get("spacing", {})
        if tw_spacing:
            lines.append("#### Spacing Scale")
            spacing_list = [f"`{k}` ({v})" for k, v in sorted(tw_spacing.items(), key=lambda x: (len(x[0]), x[0]))[:15]]
            lines.append("- " + ", ".join(spacing_list) + ("..." if len(tw_spacing) > 15 else ""))
            
        tw_radius = tw.get("borderRadius", {})
        if tw_radius:
            lines.append("#### Border Radius")
            radius_list = [f"`{k}`: `{v}`" for k, v in sorted(tw_radius.items())]
            lines.append("- " + ", ".join(radius_list))

        tw_fonts = tw.get("fontFamily", {})
        if tw_fonts:
            lines.append("#### Fonts")
            for f_name, f_val in sorted(tw_fonts.items()):
                lines.append(f"- `{f_name}`: `{', '.join(f_val) if isinstance(f_val, list) else f_val}`")

    # If no custom properties were found, append Tailwind v4 custom recommendations & utilities
    if not light_vars and not dark_vars and not tw.get("colors") and not tw.get("spacing"):
        if "TailwindCSS" in fws:
            lines.append("\n### ⚡ Standard Tailwind CSS v4 Guidelines")
            lines.append("This project relies on the default Tailwind CSS v4 design tokens and semantic variables. Follow these standards:")
            lines.append("- **Dark theme background**: Use `bg-[#030712]` or `bg-slate-950` as defined in `index.css` (:root background-color: #030712).")
            lines.append("- **Accent / Highlights**: Use violet or cyan colors (`text-violet-500`, `bg-cyan-600`, `shadow-cyan-900`) for glow and selections.")
            lines.append("- **Typography**: The primary Thai/English sans-serif font is `'Sarabun', sans-serif` as configured in `index.css` (:root). Use `font-sans` for main copy, and `font-mono` (`'JetBrains Mono'`) for code snippets.")
            lines.append("- **Icon Library**: Use **Lucide React** icons (e.g. `import { Trash, Sparkles } from 'lucide-react'`).")
            lines.append("- **Glassmorphism Utilities**: Custom utilities are available in `index.css`:")
            lines.append("  - Use `@utility glass-panel` for glass backdrops: `class=\"glass-panel\"`.")
            lines.append("  - Use `@utility glass-sidebar` for blurred sidebars: `class=\"glass-sidebar\"`.")
            lines.append("  - Use `@utility glow-text-primary` for neon purple glow: `class=\"glow-text-primary\"`.")

    files = design_ctx.get("files_found", [])
    if files:
        lines.append(f"\n_Extracted from style assets: {', '.join(f'`{f}`' for f in files)}_")
        
    return "\n".join(lines) + "\n"


def hot_tier(conn, project_id: str, project_path: str, intent: str = "") -> str:
    """D.1/D.2 Hot tier — minimum context. Always-available preamble.

    NOTE: code_index stores file_path as RELATIVE paths (`docs_src/...`)
    for the indexed source files, but the project_registry stores the
    ABSOLUTE project root. A naïve LIKE 'absolute_path%' matches only
    the project metadata row, missing all the real symbols. Fix #1
    (0.2.16): when the relative-path convention applies, count all
    indexed symbols not under another known project's root. For the
    common single-project case this is just COUNT(*).
    """
    cur = conn.cursor()

    # Detect the file_path convention. If most rows start with a relative
    # path (no leading '/'), the project's symbols live under relative
    # paths and the absolute-prefix filter would miss them.
    cur.execute("SELECT COUNT(*) FROM code_index")
    total_all = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM code_index WHERE file_path NOT LIKE '/%'")
    rel_count = cur.fetchone()[0]
    use_relative = rel_count > 0 and (rel_count / max(total_all, 1)) > 0.5

    # Dedupe by symbol_name (V1.2 fix): previously grouped by c.id,
    # which surfaced 10 separate "get" rows from FastAPI's APIRouter.get
    # callsites and confused readers. Now collapse same-name symbols and
    # show occurrence count alongside total refs.
    #
    # Pull a wider candidate set (LIMIT 100) and Python-filter by
    # checking whether the symbol is defined in a file that exists on
    # disk under project_path. Without this, hot_tier surfaces symbols
    # like fastapi's `Item`/`Depends`/`post` for an AI-Bran preamble —
    # they live in sibling watched projects but share the relative-path
    # namespace because code_index has no project_id column. Same
    # workaround as warm_tier; both go away once project_id lands.
    if use_relative:
        top_query = """
            SELECT c.symbol_name,
                   COUNT(DISTINCT c.id) AS occurrences,
                   COALESCE(SUM(r.cnt), 0) AS refs
            FROM code_index c
            LEFT JOIN (
                SELECT target_id, COUNT(*) AS cnt FROM code_links GROUP BY target_id
            ) r ON r.target_id = c.id
            WHERE c.file_path NOT LIKE '/%'
            GROUP BY c.symbol_name
            ORDER BY refs DESC
            LIMIT 100
        """
        count_query = "SELECT COUNT(*) FROM code_index WHERE file_path NOT LIKE '/%'"
        ts_query = "SELECT MAX(updated_at) FROM code_index WHERE file_path NOT LIKE '/%'"
        top_args = ()
    else:
        top_query = """
            SELECT c.symbol_name,
                   COUNT(DISTINCT c.id) AS occurrences,
                   COALESCE(SUM(r.cnt), 0) AS refs
            FROM code_index c
            LEFT JOIN (
                SELECT target_id, COUNT(*) AS cnt FROM code_links GROUP BY target_id
            ) r ON r.target_id = c.id
            WHERE c.file_path LIKE ?
            GROUP BY c.symbol_name
            ORDER BY refs DESC
            LIMIT 100
        """
        count_query = "SELECT COUNT(*) FROM code_index WHERE file_path LIKE ?"
        ts_query = "SELECT MAX(updated_at) FROM code_index WHERE file_path LIKE ?"
        top_args = (f"{project_path}%",)

    cur.execute(top_query, top_args)
    raw_top = cur.fetchall()

    # Per-symbol scope check: keep symbols defined in at least one file
    # that exists under project_path.
    top_syms: list[tuple] = []
    for row in raw_top:
        sym_name = row[0]
        cur.execute(
            "SELECT file_path FROM code_index WHERE symbol_name = ? LIMIT 8",
            (sym_name,),
        )
        files = [r[0] for r in cur.fetchall()]
        if any(os.path.isfile(os.path.join(project_path, f)) for f in files):
            top_syms.append(row)
        if len(top_syms) >= 20:
            break

    # Last lessons
    try:
        cur.execute("""
            SELECT id, symptom FROM code_errors
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 10
        """, (project_id,))
        lessons = cur.fetchall()
    except Exception:
        lessons = []

    cur.execute(count_query, top_args)
    total_syms = cur.fetchone()[0]
    cur.execute(ts_query, top_args)
    last_indexed = (cur.fetchone() or [None])[0] or "never"

    # 0.2.19 Issue #10 — align content_hash with _index_fingerprint
    # exactly. The previous formula included lesson_count which
    # differed from index_metadata's hash, producing two different
    # "indexed_at + hash" pairs that confused LLM clients.
    fingerprint = _index_fingerprint(conn)

    # 0.2.19 Issue #10 — use the same fingerprint everywhere.
    # b_layer.index_metadata() produces this same value, so trace /
    # callers / preamble now report a consistent hash.
    content_hash = fingerprint[:12]

    # Watched-projects block (V1.1 prep — preamble-local, no schema change).
    # Without this, an AI asked about a project by name has no way to tell
    # whether that project is registered in Cosmos or not, and falls back
    # to semantic-match against the currently-active index — which produces
    # wrong results when the named project isn't watched. The block below
    # lists every registered project so any AI can ground project-name
    # references before answering.
    try:
        from core.code_indexer.project_registry import get_project_registry
        all_projects = get_project_registry().list()
    except Exception:
        all_projects = []

    # Resolve the display name for THIS preamble's project: prefer the
    # registry's user-curated `name`, fall back to the path's basename
    # only if the project isn't registered. Previously every preamble
    # title used `basename(project_path)`, which surfaces the on-disk
    # folder name (e.g. "AI-Bran" for /Users/.../AI-Bran) even after the
    # user renamed the project to "Cosmos" — the AI would then call
    # the product by its old name in conversation. Treats the registry
    # as the source of truth for human-facing names.
    active_name = os.path.basename(project_path)
    for _proj in all_projects:
        if _normalize_path_safe(_proj.get("path", "")) == _normalize_path_safe(project_path):
            active_name = _proj.get("name") or active_name
            break

    watched_lines = []
    if all_projects:
        watched_lines.append(f"## 📂 Currently watched projects ({len(all_projects)})")
        watched_lines.append("")
        watched_lines.append("_If the user names a project, match against THIS list first — "
                             "don't infer from semantic keyword matches in the active index._")
        watched_lines.append("")
        watched_lines.append("| Name | Path | Symbols | Last indexed |")
        watched_lines.append("|---|---|---|---|")
        for p in all_projects:
            stats = p.get("stats") or {}
            sym_count = stats.get("symbols", "?")
            active = " ← _active_" if _normalize_path_safe(p.get("path", "")) == _normalize_path_safe(project_path) else ""
            li = p.get("last_indexed_at") or "never"
            if isinstance(li, str) and len(li) >= 10:
                li = li[:10]
            watched_lines.append(f"| **{p.get('name', '?')}**{active} | `{p.get('path', '?')}` | {sym_count} | {li} |")
        watched_lines.append("")

    lines = [
        f"# Cosmos Project Preamble (Hot) — {active_name}",
        f"",
        f"**indexed_at:** {last_indexed}",
        f"**content_hash:** {content_hash}",
        f"**total_symbols:** {total_syms}",
        f"**lesson_count:** {len(lessons)}",
        f"",
        *watched_lines,
        # Capabilities map — printed up-front so an AI that has never read
        # SKILL.md / `.cursor/rules` / `.clinerules` still understands what
        # Cosmos is and which tool to reach for. This block is the
        # universal "business card" for clients that don't auto-load
        # rules files (Cursor's new format, Cline, Windsurf, Aider,
        # Continue, Goose, raw MCP clients).
        f"## Cosmos — what this MCP server is for",
        f"",
        f"Cosmos is the user's local-first memory + code-aware index. It sits between",
        f"their folders and you (their AI tool) — you read from it, write to it, and",
        f"recall past lessons through MCP. **Complete tool inventory below** — you",
        f"should NEVER need to `grep` mcp_server.py to discover tools; everything is",
        f"listed here.",
        f"",
        f"| Domain | When user asks about… | Reach for |",
        f"|---|---|---|",
        f"| 💻 **Code search & navigation** | symbols, functions, files, structure | `code_search`, `code_find_file`, `code_get_symbol`, `code_find_function`, `code_skeleton`, `code_hierarchy`, `code_explain_project` |",
        f"| 🕸️ **Call graph & impact** | who calls X, what does X call, refactor blast radius | `code_callers`, `code_callees`, `code_uses`, `code_trace_value`, `code_analyze_refactor_impact`, `code_boundaries`, `code_diff`, `code_context_bundle` |",
        f"| ✨ **Smart routing** | vague symptom, \"why does X break\", \"where do I look\" | `find_relevant_code` (joins code FTS + past lessons in one round-trip — call FIRST for any symptom-style question) |",
        f"| 📚 **Project lessons** | known bugs, past gotchas, recurring mistakes | `code_list_errors` (call FIRST before edits), `code_remember_error` (call AFTER non-trivial fix) |",
        f"| 📝 **Brain memory** | saved notes, decisions, research, project log | `brain_search`, `brain_get`, `brain_remember`, `brain_aggregate`, `brain_pattern_recall` |",
        f"| 🗺️ **Orientation** | session start, \"what is this project\", \"where am I\" | `brain_sitemap`, `brain_session_context`, `brain_status`, `cosmos_get_preamble(intent=...)` (all callable FIRST) |",
        f"| 📂 **Brain edits** | create/delete folders, move memories | `brain_create_folder`, `brain_delete_folder`, `brain_move_memory`, `brain_rebuild_links` |",
        f"| 🔧 **Project ops** | reindex, refresh map, register a watched folder | `code_reindex`, `cosmos_refresh_map`, `code_explain` (LLM annotation — Tier 2+) |",
        f"| 🤖 **Agent + dogfood** | provision an agent, log a Claude task | `brain_create_agent`, `claude_log_task`, `claude_report` |",
        f"",
        f"_Tools count: 30+ registered. If a tool you need isn't in the table, call_",
        f"_`tools/list` over MCP — don't read source files._",
        f"",
        f"**Decision rules:**",
        f"- User mentions **code structure / files / symbols** → start with `code_*`, NOT a broad `brain_search`.",
        f"- User mentions **a saved note / a decision / a past idea** → start with `brain_*`.",
        f"- User describes a **symptom** with no clear scope → call `find_relevant_code` first (it joins code + past lessons in one round-trip).",
        f"- Before editing any file in a watched project: call `code_list_errors` (or `find_relevant_code`) so prior lessons surface BEFORE the change, not after.",
        f"- After fixing a non-trivial error: call `code_remember_error` — root cause differed from message, took >1 try, or surprised you.",
        f"",
        f"**Refactor-impact triggers — call `code_analyze_refactor_impact` (or at minimum `find_relevant_code`) when the user says ANY of:**",
        f"- \"edit / modify / refactor X\"",
        f"- \"what breaks if I change X\"",
        f"- \"impact of changing X\" / \"downstream of changing X\"",
        f"- \"consequences of refactoring X\" / \"is it safe to change X\"",
        f"- \"blast radius of X\" / \"what depends on X\"",
        f"- \"rename / remove / replace X\"",
        f"",
        f"  These look like questions, but they are **edit precursors** — Cosmos's "
        f"compound-lesson loop only fires if you fetch lessons BEFORE editing. "
        f"Skipping this and answering from inference re-derives bugs the user "
        f"already shipped a fix for.",
        f"",
        f"**Behavioral guardrails:**",
        f"- Pinned + scope-globbed lessons (`code_list_errors` results with `pinned=true`) carry stronger weight than your own re-derivation. If a lesson contradicts your plan, surface it to the user before proceeding.",
        f"- `brain_search` without a category/folder filter is a recall-heavy query — prefer it as a fallback, not a starting point.",
        f"- Destructive ops (`brain_delete_folder`, `code_remember_error` overwriting an existing entry) require user confirmation, not silent execution.",
        f"",
        f"## Top symbols (by reference count)",
    ]
    for row in top_syms[:10]:
        # Row shape changed in V1.2 dedup: (symbol_name, occurrences, refs).
        # Keep tolerant of the legacy 2-tuple shape so a re-run against an
        # older snapshot during build doesn't crash.
        if len(row) == 3:
            sym, occ, refs = row
            if occ > 1:
                lines.append(f"- `{sym}` ({refs} refs · {occ} occurrences)")
            else:
                lines.append(f"- `{sym}` ({refs} refs)")
        else:
            sym, refs = row
            lines.append(f"- `{sym}` ({refs} refs)")

    if lessons:
        lines.append(f"\n## Last 10 past lessons (cosmos_errors)")
        for lid, sym in lessons:
            lines.append(f"- [{lid[:8]}] {(sym or '')[:80]}")

    # NOTE — earlier (2026-05-12) the hot_tier briefly auto-injected
    # Module breakdown + Entry points from project_map.py to address
    # the "no accumulated lessons" Day-1 gap surfaced by replication.
    # Per pre-commit gate (Path A) the injection was REVERTED because:
    #   1. The Day-1 metric did not actually lift (the relevant regime
    #      tests topic-specific surfacing, which the intent-aware
    #      preamble already covers).
    #   2. code_index has no project_id column → multi-project setups
    #      saw cross-project contamination in the injected sections.
    # The MOC remains available as a standalone file (cosmos_refresh_map
    # writes <project>/.cosmos/project_summary.md), and the auto-trigger
    # in watcher_manager keeps it fresh. Re-introduce here only after
    # the multi-project isolation issue is fixed in code_index.

    # Track 3.5 — topic-scoped augmentation when intent is supplied.
    topic_block = _topic_scoped_section(conn, intent, k=5)
    if topic_block:
        lines.append(topic_block)

    # Fetch project overview to extract design context (Phase 5.13)
    try:
        cur.execute("SELECT content FROM code_index WHERE id = ?", (f"project_overview:{project_path}",))
        row = cur.fetchone()
        if row:
            overview = json.loads(row[0])
            design_ctx = overview.get("design_context")
            design_md = _format_design_context_markdown(design_ctx)
            if design_md:
                lines.append(design_md)
    except Exception:
        pass

    lines.append(f"\n---")
    lines.append(f"_If a question maps to one of these symbols or lessons, prefer applying the lesson over re-deriving._")
    lines.append(f"_Fetch the Warm tier via `cosmos_get_preamble(path=..., tier=\"warm\")` for module map + hotspots._")

    return "\n".join(lines)


def warm_tier(conn, project_id: str, project_path: str) -> str:
    """D.1/D.2 Warm tier — module map + hotspots + lesson index."""
    cur = conn.cursor()
    # Same relative-path detection as hot_tier (Fix #1 from 0.2.16)
    cur.execute("SELECT COUNT(*) FROM code_index WHERE file_path NOT LIKE '/%'")
    rel_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM code_index")
    total_all = cur.fetchone()[0]
    use_relative = rel_count > 0 and (rel_count / max(total_all, 1)) > 0.5

    # code_index has no project_id column (multi-project tracking issue),
    # so files from other watched projects share the same relative-path
    # namespace and would otherwise leak into this project's preamble —
    # e.g. `cli/` or `webview-ui/` rows from a sibling project surfacing
    # in AI-Bran's module map. The on-disk existence check is the
    # workaround until a `project_id` column lands.
    #
    # Two separate queries so the module map sees the full breadth of
    # dirs (not just dirs containing the top-N hottest files): one
    # aggregates ALL rows by top-level dir, one fetches the hottest
    # individual files for the "hottest files" section.
    # Single per-file-row pass: count symbols per file (group), then for
    # each file check `os.path.isfile(project_path/file_path)`. Rows that
    # don't resolve to a real file under project_path belong to another
    # watched project (relative-path namespace collision until project_id
    # column lands). Aggregating dir counts only over surviving rows
    # avoids the over-count we'd get from naïve top-dir grouping
    # (e.g. AI-Bran's `src/` showing 5017 symbols because cline's
    # `src/core/api/adapters/*` rows share the same top dir).
    if use_relative:
        cur.execute("""
            SELECT file_path, COUNT(*) AS sym_count
            FROM code_index
            WHERE file_path NOT LIKE '/%'
            GROUP BY file_path
            ORDER BY sym_count DESC
        """)
    else:
        cur.execute("""
            SELECT file_path, COUNT(*) AS sym_count
            FROM code_index
            WHERE file_path LIKE ?
            GROUP BY file_path
            ORDER BY sym_count DESC
        """, (f"{project_path}%",))
    all_rows = cur.fetchall()

    by_dir: dict[str, int] = {}
    file_counts: list[tuple[str, int]] = []
    for fpath, count in all_rows:
        rel = fpath.replace(project_path, "").lstrip("/") if not use_relative else fpath
        if not os.path.isfile(os.path.join(project_path, rel)):
            continue
        top_dir = rel.split("/")[0] if "/" in rel else "."
        by_dir[top_dir] = by_dir.get(top_dir, 0) + count
        if len(file_counts) < 40:
            file_counts.append((fpath, count))

    # Warm tier uses the same name-resolution rule as hot_tier — pull
    # the user-curated display name from the registry, fall back to
    # basename only for unregistered paths.
    warm_name = os.path.basename(project_path)
    try:
        from core.code_indexer.project_registry import get_project_registry
        for _proj in get_project_registry().list():
            if _normalize_path_safe(_proj.get("path", "")) == _normalize_path_safe(project_path):
                warm_name = _proj.get("name") or warm_name
                break
    except Exception:
        pass

    lines = [
        f"# Cosmos Project Preamble (Warm) — {warm_name}",
        f"",
        f"## Top-level module map",
    ]
    for d, c in sorted(by_dir.items(), key=lambda x: -x[1]):
        lines.append(f"- `{d}/` — {c} symbols")

    lines.append(f"\n## Hottest files (by symbol count)")
    for fpath, count in file_counts[:15]:
        rel = fpath.replace(project_path, "").lstrip("/")
        lines.append(f"- {rel} ({count} symbols)")

    # Lesson index
    try:
        cur.execute("""
            SELECT id, symptom, severity FROM code_errors
            WHERE project_id = ?
            ORDER BY created_at DESC
        """, (project_id,))
        all_lessons = cur.fetchall()
    except Exception:
        all_lessons = []

    if all_lessons:
        lines.append(f"\n## All lessons ({len(all_lessons)})")
        for lid, sym, sev in all_lessons:
            sev_marker = "🔴" if sev == 1 else ("🟡" if sev == 2 else "🔵")
            lines.append(f"- {sev_marker} [{lid[:8]}] {(sym or '')[:120]}")

    lines.append(f"\n---")
    lines.append(f"_Fetch Cold tier via `cosmos_get_preamble(path=..., tier=\"cold\")` for full file/symbol tree._")
    return "\n".join(lines)


def cold_tier(conn, project_id: str, project_path: str, offset: int = 0,
              limit: int = 100) -> str:
    """D.2 Cold tier — full skeleton, paginated."""
    cur = conn.cursor()
    cur.execute("""
        SELECT file_path, symbol_name, symbol_type, start_line
        FROM code_index
        WHERE file_path LIKE ?
        ORDER BY file_path, start_line
        LIMIT ? OFFSET ?
    """, (f"{project_path}%", limit, offset))
    rows = cur.fetchall()
    lines = [
        f"# Cosmos Project Preamble (Cold) — page offset={offset}, limit={limit}",
        "",
    ]
    last_file = None
    for fpath, sym, stype, line in rows:
        if fpath != last_file:
            lines.append(f"\n## {fpath.replace(project_path,'').lstrip('/')}")
            last_file = fpath
        lines.append(f"- {stype} `{sym}` :{line}")
    if len(rows) == limit:
        lines.append(f"\n_More entries available — fetch with offset={offset+limit}._")
    return "\n".join(lines)


# ── MCP resource handlers (D.3/D.4) ────────────────────────────────────

def get_preamble(conn, project_id: str, project_path: str,
                 tier: str = "hot", intent: str = "") -> tuple[str, str]:
    """Returns (content_hash, body). Honors D.7 within-session cache
    keyed by index fingerprint + intent (Track 3.5 — different intents
    must not share cache entries since topic-scoped augmentation
    differs)."""
    fp = _index_fingerprint(conn)
    # Mix intent into the tier-cache key so topic-scoped preambles don't
    # collide with the unscoped version (or with each other).
    tier_key = tier if not intent else f"{tier}:{hashlib.sha256(intent.encode()).hexdigest()[:8]}"
    cached = _cache_get(project_id, tier_key, fp)
    if cached:
        return cached
    if tier == "hot":
        body = hot_tier(conn, project_id, project_path, intent=intent)
    elif tier == "warm":
        body = warm_tier(conn, project_id, project_path)
    elif tier == "cold":
        body = cold_tier(conn, project_id, project_path)
    else:
        raise ValueError(f"unknown tier: {tier}")
    h = _cache_set(project_id, tier_key, fp, body)
    return h, body


# ── CLAUDE.md merge (D.5) ──────────────────────────────────────────────

_BEGIN_MARKER = "<!-- COSMOS:BEGIN auto-generated, edits above this line are preserved -->"
_END_MARKER = "<!-- COSMOS:END -->"


def merge_into_claude_md(project_path: str, preamble: str) -> str:
    """D.5 — Merge preamble into <project>/CLAUDE.md using section markers
    so user-curated content above the marker is preserved.

    Returns the merged file path. Creates CLAUDE.md if missing.
    """
    target = os.path.join(project_path, "CLAUDE.md")
    block = f"{_BEGIN_MARKER}\n{preamble}\n{_END_MARKER}\n"

    if not os.path.exists(target):
        with open(target, "w") as f:
            f.write(block)
        return target

    with open(target) as f:
        existing = f.read()

    if _BEGIN_MARKER in existing and _END_MARKER in existing:
        before = existing.split(_BEGIN_MARKER)[0]
        after = existing.split(_END_MARKER, 1)[1] if _END_MARKER in existing else ""
        merged = before.rstrip() + "\n\n" + block + after.lstrip()
    else:
        merged = existing.rstrip() + "\n\n" + block

    with open(target, "w") as f:
        f.write(merged)
    return target


# ── pyright LSP integration (D.9) — deferred stub ──────────────────────

def trace_with_pyright(symbol: str, project_path: str) -> Optional[dict]:
    """D.9 — DEFERRED. Will invoke pyright via LSP to get type-aware
    flow analysis for code_trace_value. Until then, return None and
    let the regex-based boundary detection in b_layer.py do the job."""
    return None
