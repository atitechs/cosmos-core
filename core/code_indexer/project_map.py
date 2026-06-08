"""Project Map (MOC) — Obsidian-style Map of Content for a code project.

Generates `<project>/.cosmos/project_summary.md` from the indexed code
graph. Two reasons this exists:

1. **Day-1 value.** A fresh Cosmos install has zero accumulated lessons,
   so the hot-tier preamble's "past lessons" section is empty. The
   replication on the requests corpus (no prior lessons) confirmed the
   preamble hit-rate drops from 0.60 → 0.40 vs the fastapi corpus
   (which had 3 lessons). The MOC gives day-1 users an architectural
   view that does NOT depend on lesson accumulation.

2. **Obsidian-style discoverability.** Power users coming from Obsidian
   expect a project map (MOC = Map of Content) — a single navigable
   markdown file that links into the structured graph. This mirrors
   that pattern for code: a hand-readable summary plus a machine-
   generated section bounded by markers.

Output file: `<project_root>/.cosmos/project_summary.md`
- Section between MOC:BEGIN/END markers is auto-regenerated.
- Anything else in the file (above or below the markers) is preserved
  across regen — that's where the user adds team conventions, ADRs,
  intent notes.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

_BEGIN = "<!-- COSMOS:MOC:BEGIN auto-generated, edits outside this block are preserved -->"
_END = "<!-- COSMOS:MOC:END -->"


def _project_prefixes(conn, project_path: str) -> list[str]:
    """Detect whether code_index for this project uses relative or
    absolute file paths. The exporter convention varies by project."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM code_index WHERE file_path NOT LIKE '/%'")
    rel = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM code_index")
    total = cur.fetchone()[0]
    if total == 0:
        return []
    if rel > total * 0.5:
        # Relative-path project — accept any non-absolute path.
        return [""]
    return [project_path.rstrip("/") + "/"]


def _top_symbols(conn, prefixes: list[str], limit: int = 15) -> list[tuple[str, int]]:
    cur = conn.cursor()
    if prefixes == [""]:
        cur.execute("""
            SELECT c.symbol_name, COUNT(l.target_id) AS refs
            FROM code_index c
            LEFT JOIN code_links l ON l.target_id = c.id
            WHERE c.file_path NOT LIKE '/%'
              AND c.symbol_type IN ('function','method','async_function','class')
            GROUP BY c.id
            ORDER BY refs DESC LIMIT ?
        """, (limit,))
    else:
        like = prefixes[0] + "%"
        cur.execute("""
            SELECT c.symbol_name, COUNT(l.target_id) AS refs
            FROM code_index c
            LEFT JOIN code_links l ON l.target_id = c.id
            WHERE c.file_path LIKE ?
              AND c.symbol_type IN ('function','method','async_function','class')
            GROUP BY c.id
            ORDER BY refs DESC LIMIT ?
        """, (like, limit))
    return cur.fetchall()


def _entry_points(conn, prefixes: list[str], limit: int = 10) -> list[tuple[str, str]]:
    """Symbols defined at module-top-level in __init__.py or with
    decorators that signal entry-ness (route, command, etc.)."""
    cur = conn.cursor()
    if prefixes == [""]:
        cur.execute("""
            SELECT symbol_name, file_path
            FROM code_index
            WHERE file_path NOT LIKE '/%'
              AND (file_path LIKE '%__init__.py' OR file_path LIKE '%main.py'
                   OR file_path LIKE '%app.py' OR file_path LIKE '%cli.py')
              AND symbol_type IN ('function','method','class')
            LIMIT ?
        """, (limit,))
    else:
        like = prefixes[0] + "%"
        cur.execute("""
            SELECT symbol_name, file_path
            FROM code_index
            WHERE file_path LIKE ?
              AND (file_path LIKE '%__init__.py' OR file_path LIKE '%main.py'
                   OR file_path LIKE '%app.py' OR file_path LIKE '%cli.py')
              AND symbol_type IN ('function','method','class')
            LIMIT ?
        """, (like, limit))
    return cur.fetchall()


def _module_breakdown(conn, prefixes: list[str], limit: int = 8) -> list[tuple[str, int]]:
    cur = conn.cursor()
    if prefixes == [""]:
        cur.execute("""
            SELECT
              CASE
                WHEN instr(file_path, '/') > 0
                THEN substr(file_path, 1, instr(file_path, '/') - 1)
                ELSE file_path
              END AS module,
              COUNT(*) AS n
            FROM code_index
            WHERE file_path NOT LIKE '/%'
              AND symbol_type IN ('function','method','async_function','class')
            GROUP BY module
            ORDER BY n DESC LIMIT ?
        """, (limit,))
    else:
        like = prefixes[0] + "%"
        cur.execute("""
            SELECT
              CASE
                WHEN instr(substr(file_path, length(?)+1), '/') > 0
                THEN substr(file_path, length(?)+1,
                            instr(substr(file_path, length(?)+1), '/') - 1)
                ELSE substr(file_path, length(?)+1)
              END AS module,
              COUNT(*) AS n
            FROM code_index
            WHERE file_path LIKE ?
              AND symbol_type IN ('function','method','async_function','class')
            GROUP BY module
            ORDER BY n DESC LIMIT ?
        """, (prefixes[0], prefixes[0], prefixes[0], prefixes[0], like, limit))
    return cur.fetchall()


def _lessons(conn, project_id: Optional[str], limit: int = 8) -> list[tuple[str, str]]:
    cur = conn.cursor()
    try:
        if project_id:
            cur.execute("""
                SELECT id, symptom FROM code_errors
                WHERE project_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, (project_id, limit))
        else:
            cur.execute("SELECT id, symptom FROM code_errors ORDER BY created_at DESC LIMIT ?",
                        (limit,))
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []


def _stats(conn, prefixes: list[str]) -> dict:
    cur = conn.cursor()
    if prefixes == [""]:
        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT file_path)
            FROM code_index WHERE file_path NOT LIKE '/%'
        """)
    else:
        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT file_path)
            FROM code_index WHERE file_path LIKE ?
        """, (prefixes[0] + "%",))
    symbols, files = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM code_links WHERE link_type='call'")
    calls = cur.fetchone()[0]
    return {"symbols": symbols, "files": files, "call_edges": calls}


def render_moc(conn, project_id: Optional[str], project_path: str) -> str:
    """Build the machine-generated MOC body (between markers)."""
    prefixes = _project_prefixes(conn, project_path)
    stats = _stats(conn, prefixes)
    top_syms = _top_symbols(conn, prefixes, limit=15)
    entry = _entry_points(conn, prefixes, limit=8)
    modules = _module_breakdown(conn, prefixes, limit=8)
    lessons = _lessons(conn, project_id, limit=8)

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f"# Project Map — {os.path.basename(project_path.rstrip('/'))}",
        f"",
        f"_Auto-generated by Cosmos · last update: {ts}_",
        f"",
        f"**Files:** {stats['files']} · "
        f"**Symbols:** {stats['symbols']} · "
        f"**Call edges:** {stats['call_edges']}",
        f"",
    ]

    if modules:
        lines.append("## Module breakdown")
        lines.append("")
        for mod, n in modules:
            if mod:
                lines.append(f"- `{mod}/` — {n} symbols")
        lines.append("")

    if entry:
        lines.append("## Entry points")
        lines.append("")
        seen_files = set()
        for sym, fp in entry:
            key = (sym, fp)
            if key in seen_files:
                continue
            seen_files.add(key)
            lines.append(f"- `{sym}` in `{fp}`")
        lines.append("")

    if top_syms:
        lines.append("## Top symbols (by reference count)")
        lines.append("")
        for sym, refs in top_syms[:15]:
            lines.append(f"- `{sym}` ({refs} refs)")
        lines.append("")

    if lessons:
        lines.append("## Active lessons")
        lines.append("")
        for lid, sym in lessons:
            lines.append(f"- [{(lid or '')[:8]}] {(sym or '')[:120]}")
        lines.append("")

    lines.append("---")
    lines.append("_If your question maps to an entry point, top symbol, or a "
                 "logged lesson — start there. The MOC is a navigational "
                 "shortcut, not a replacement for `find_relevant_code`._")
    return "\n".join(lines)


def write_moc(project_path: str, project_id: Optional[str], conn) -> Path:
    """Write `<project_root>/.cosmos/project_summary.md`, preserving any
    content outside the BEGIN/END markers.

    If the file does not exist yet, creates a stub with the machine block
    plus a "## Team conventions" placeholder for the user to fill in.
    """
    body = render_moc(conn, project_id, project_path)
    out_dir = Path(project_path) / ".cosmos"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "project_summary.md"

    auto_block = f"{_BEGIN}\n\n{body}\n\n{_END}"

    if out.exists():
        existing = out.read_text(encoding="utf-8")
        if _BEGIN in existing and _END in existing:
            pre = existing.split(_BEGIN)[0]
            post = existing.split(_END, 1)[1]
            new_text = pre + auto_block + post
        else:
            # Append marker block at the top, keep existing as user notes below.
            new_text = (auto_block + "\n\n" + existing).rstrip() + "\n"
    else:
        new_text = (
            auto_block + "\n\n"
            "## Team conventions\n\n"
            "_Add anything you want preserved across regenerations here — "
            "ADRs, naming conventions, intent notes. Cosmos rewrites only "
            "the block between the COSMOS:MOC markers above._\n"
        )

    out.write_text(new_text, encoding="utf-8")
    return out
