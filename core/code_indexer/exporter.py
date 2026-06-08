"""Project context exporter — emits HTML / Markdown / JSON snapshots of an
indexed project's structure to the user's Downloads folder
(`~/Downloads/cosmos-exports/<project-slug>/`).

Use case: feed the artifact to AI assistants outside the MCP loop (Cursor
without MCP, ChatGPT, Gemini, code review buddies). Mirrors the value prop
of tools like Graphify but keeps everything 100% local + adds Cosmos-only
features: per-project past-lessons + JSX component outlines for React.

Note on output location: exports land in `~/Downloads/cosmos-exports/` so
the user can immediately drag/share the artifact (the share use case).
NOT into the watched project's folder — that would pollute external repos
with `.aibran/exports/` files that could leak into git. The portable
per-project `.aibran/lessons.md` (a separate artifact) DOES still live
with the project, since it's read by external AI tools.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _aibran_root() -> str:
    """Absolute path to the Cosmos project root, derived from this file's
    location (core/code_indexer/exporter.py → up 2 levels)."""
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _slugify(name: str) -> str:
    """Make a filesystem-safe folder name from a project name."""
    s = re.sub(r"[^\w\-]+", "-", name.strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "project"


def export_project(
    project_id: str,
    formats: Optional[List[str]] = None,
    include_lessons: bool = True,
    include_jsx: bool = True,
) -> Dict[str, Any]:
    """Generate export artifacts for a watched project. Returns metadata
    (project_id, generated_at, outputs[{format, path, size}], stats)."""
    from core.code_indexer.project_registry import get_project_registry

    formats = [f.lower() for f in (formats or ["html", "md", "json"])]
    proj = get_project_registry().get(project_id)
    if not proj:
        raise ValueError(f"Unknown project: {project_id}")

    project_root = os.path.abspath(os.path.expanduser(proj["path"]))
    data = _collect_project_data(project_id, proj, project_root, include_lessons, include_jsx)

    # Output goes to the user's Downloads folder so the file is immediately
    # findable + shareable (the share use case). Subfolder per project keeps
    # multi-format exports grouped and avoids polluting Downloads root.
    out_dir = os.path.join(
        os.path.expanduser("~/Downloads"),
        "cosmos-exports",
        _slugify(proj["name"]),
    )
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    outputs: List[Dict[str, Any]] = []

    if "json" in formats:
        path = os.path.join(out_dir, f"graph-{timestamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        outputs.append({"format": "json", "path": path, "size": os.path.getsize(path)})

    if "md" in formats:
        path = os.path.join(out_dir, f"graph-{timestamp}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_render_markdown(data))
        outputs.append({"format": "md", "path": path, "size": os.path.getsize(path)})

    if "html" in formats:
        path = os.path.join(out_dir, f"graph-{timestamp}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_render_html(data))
        outputs.append({"format": "html", "path": path, "size": os.path.getsize(path)})

    return {
        "project_id": project_id,
        "project_name": proj["name"],
        "generated_at": timestamp,
        "out_dir": out_dir,
        "outputs": outputs,
        "stats": {
            "files": len(data["files"]),
            "symbols": sum(len(f["symbols"]) for f in data["files"]),
            "lessons": len(data.get("lessons", [])),
            "languages": len({f["language"] for f in data["files"]}),
        },
    }


# ── Data collection ────────────────────────────────────────────────────

def _collect_project_data(
    project_id: str, proj: Dict, project_root: str,
    include_lessons: bool, include_jsx: bool,
) -> Dict[str, Any]:
    from core.memory.store_v2 import get_store_v2
    from core.code_indexer.errors import get_code_errors

    cur = get_store_v2().conn.cursor()
    cur.execute("""
        SELECT file_path, symbol_name, symbol_type, scope, content, docstring,
               language, start_line, end_line
        FROM code_index
        WHERE symbol_type != 'overview' AND symbol_type != 'file'
        ORDER BY file_path, start_line
    """)
    rows = cur.fetchall()

    by_file: Dict[str, Dict[str, Any]] = {}
    for fp, sn, st, scope, content, doc, lang, sl, el in rows:
        # Filter to files inside this project (file_path is stored relative
        # to project root by the indexer, so just include all rows for now —
        # in single-project setups this is correct; multi-project would need
        # a project_id column on code_index, deferred).
        rec = by_file.setdefault(fp, {
            "path": fp,
            "language": lang or "?",
            "symbols": [],
        })
        rec["symbols"].append({
            "name": sn,
            "type": st,
            "scope": scope,
            "signature": (content or sn or "").strip().split("\n")[0][:200],
            "docstring": (doc or "").strip().splitlines()[0][:120] if doc else None,
            "start_line": sl,
            "end_line": el,
        })

    files = sorted(by_file.values(), key=lambda f: f["path"])

    data: Dict[str, Any] = {
        "project": {
            "id": project_id,
            "name": proj["name"],
            "path": project_root,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }

    if include_lessons:
        try:
            data["lessons"] = get_code_errors().list_for_project(project_id, limit=200)
        except Exception:
            data["lessons"] = []

    if include_jsx:
        try:
            from core.api.mcp_server import _extract_jsx_outline
            for f in files:
                if not f["path"].lower().endswith((".tsx", ".jsx")):
                    continue
                cur.execute(
                    "SELECT body FROM code_index WHERE file_path = ? AND symbol_type = 'file' LIMIT 1",
                    (f["path"],),
                )
                row = cur.fetchone()
                if row and row[0]:
                    outline = _extract_jsx_outline(row[0])
                    if outline:
                        f["jsx_outline"] = outline
        except Exception:
            pass

    return data


# ── Markdown renderer ──────────────────────────────────────────────────

def _render_markdown(data: Dict[str, Any]) -> str:
    p = data["project"]
    sym_count = sum(len(f["symbols"]) for f in data["files"])
    lines = [
        f"# {p['name']} — Code Graph",
        "",
        f"_Generated: {data['generated_at']} · Path: `{p['path']}`_",
        "",
        "## Stats",
        f"- Files: **{len(data['files'])}**",
        f"- Symbols: **{sym_count}**",
        f"- Languages: {', '.join(sorted({f['language'] for f in data['files']}))}",
    ]
    if data.get("lessons"):
        lines.append(f"- Past lessons: **{len(data['lessons'])}**")
    lines.extend(["", "---", "", "## Files", ""])

    for f in data["files"]:
        lines.append(f"### `{f['path']}` _{f['language']}_ — {len(f['symbols'])} symbols")
        for s in f["symbols"]:
            scope = f"{s['scope']}." if s.get("scope") and s["type"] == "method" else ""
            lines.append(f"- **{scope}{s['name']}** `{s['type']}` — `{s['signature']}`")
            if s.get("docstring"):
                lines.append(f"  - _{s['docstring']}_")
        if f.get("jsx_outline"):
            lines.extend(["", "**JSX outline:**", "```", f["jsx_outline"], "```"])
        lines.append("")

    if data.get("lessons"):
        lines.extend(["---", "", "## Past Lessons", ""])
        for r in data["lessons"]:
            sev = {1: "🔴", 2: "🟡", 3: "🟢"}.get(r.get("severity"), "⚪")
            lines.append(f"### {sev} sev{r['severity']} · {r.get('last_seen_at', '')}")
            lines.append(f"**Symptom:** {r['symptom']}")
            if r.get("root_cause"):
                lines.append(f"**Root cause:** {r['root_cause']}")
            if r.get("fix"):
                lines.append(f"**Fix:** {r['fix']}")
            if r.get("files_affected"):
                lines.append(f"**Files:** {', '.join(r['files_affected'])}")
            lines.append("")

    return "\n".join(lines)


# ── HTML renderer ──────────────────────────────────────────────────────

def _render_html(data: Dict[str, Any]) -> str:
    p = data["project"]
    files = data["files"]
    lessons = data.get("lessons", []) or []
    sym_count = sum(len(f["symbols"]) for f in files)
    lang_count = len({f["language"] for f in files})
    json_blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(p['name'])} — Code Graph</title>
<style>
  body {{ font: 13px/1.5 ui-sans-serif, system-ui, -apple-system; background: #030712; color: #e2e8f0; padding: 32px; max-width: 1100px; margin: 0 auto; }}
  h1 {{ color: #fff; margin: 0 0 4px; font-size: 28px; }}
  h2 {{ color: #fff; margin: 32px 0 12px; font-size: 18px; }}
  .meta {{ color: #94a3b8; font-size: 11px; margin-bottom: 24px; font-family: monospace; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
  .stat {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; padding: 14px; text-align: center; }}
  .stat .num {{ font-size: 24px; font-weight: bold; color: #22d3ee; font-family: monospace; }}
  .stat .lbl {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.1em; margin-top: 2px; }}
  .filter {{ width: 100%; background: #0f172a; border: 1px solid #1e293b; color: #fff; padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 12px; }}
  .filter:focus {{ outline: none; border-color: #22d3ee; }}
  details {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; padding: 10px 14px; margin-bottom: 6px; }}
  details summary {{ cursor: pointer; color: #fff; font-family: monospace; font-size: 13px; user-select: none; outline: none; }}
  details summary .lang {{ color: #64748b; font-size: 11px; margin-left: 8px; }}
  details summary .count {{ float: right; color: #64748b; font-size: 11px; }}
  .sym {{ font-family: monospace; font-size: 12px; padding: 3px 0 3px 28px; color: #cbd5e1; word-break: break-word; }}
  .sym .stype {{ display: inline-block; min-width: 64px; color: #64748b; font-size: 10px; text-transform: uppercase; }}
  .sym .sname {{ color: #67e8f9; }}
  .sym .doc {{ display: block; padding-left: 76px; color: #64748b; font-size: 11px; font-style: italic; }}
  .jsx {{ background: #050b14; padding: 10px 14px; border-radius: 4px; font-family: monospace; font-size: 11px; white-space: pre; color: #94a3b8; margin: 8px 0 4px 28px; border-left: 2px solid #064e3b; }}
  .lesson {{ background: #0f172a; border-left: 3px solid #f59e0b; padding: 12px 16px; margin-bottom: 8px; border-radius: 4px; }}
  .lesson .sym {{ color: #fff; font-weight: bold; padding-left: 0; font-family: inherit; font-size: 13px; margin-bottom: 6px; }}
  .lesson .row {{ font-size: 12px; color: #cbd5e1; margin-bottom: 4px; }}
  .lesson .row b {{ color: #f59e0b; }}
  footer {{ margin-top: 48px; padding-top: 16px; border-top: 1px solid #1e293b; color: #475569; font-size: 11px; text-align: center; }}
</style>
</head><body>

<h1>{_html.escape(p['name'])}</h1>
<div class="meta">Generated {data['generated_at']} · <code>{_html.escape(p['path'])}</code></div>

<div class="stats">
  <div class="stat"><div class="num">{len(files)}</div><div class="lbl">Files</div></div>
  <div class="stat"><div class="num">{sym_count}</div><div class="lbl">Symbols</div></div>
  <div class="stat"><div class="num">{len(lessons)}</div><div class="lbl">Lessons</div></div>
  <div class="stat"><div class="num">{lang_count}</div><div class="lbl">Languages</div></div>
</div>

<h2>Files</h2>
<input class="filter" id="ff" placeholder="Filter files (path or language)…" />
<div id="files">
{_render_html_files(files)}
</div>

{('<h2>Past Lessons</h2><div>' + _render_html_lessons(lessons) + '</div>') if lessons else ''}

<footer>Generated by Cosmos v5 · 100% local · <code>core/code_indexer/exporter.py</code></footer>

<script>
  window.__GRAPH_DATA__ = {json_blob};
  // Live filter
  const ff = document.getElementById('ff');
  if (ff) ff.addEventListener('input', e => {{
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('#files details').forEach(d => {{
      d.style.display = !q || d.dataset.search.includes(q) ? '' : 'none';
    }});
  }});
</script>
</body></html>
"""


def _render_html_files(files: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for f in files:
        path = _html.escape(f["path"])
        lang = _html.escape(f["language"])
        search_key = f"{f['path']} {f['language']}".lower()
        sym_html_parts: List[str] = []
        for s in f["symbols"]:
            scope = f'{_html.escape(s["scope"])}.' if s.get("scope") and s["type"] == "method" else ""
            sig = _html.escape(s["signature"])
            doc = _html.escape(s["docstring"]) if s.get("docstring") else None
            sym_html_parts.append(
                f'<div class="sym"><span class="stype">[{_html.escape(s["type"])}]</span> '
                f'<span class="sname">{scope}{_html.escape(s["name"])}</span> {sig}'
                + (f'<span class="doc">{doc}</span>' if doc else '')
                + '</div>'
            )
        jsx_html = f'<div class="jsx">{_html.escape(f["jsx_outline"])}</div>' if f.get("jsx_outline") else ""
        out.append(
            f'<details data-search="{_html.escape(search_key)}">'
            f'<summary>{path}<span class="lang">{lang}</span><span class="count">{len(f["symbols"])} symbols</span></summary>'
            f'{"".join(sym_html_parts)}{jsx_html}</details>'
        )
    return "".join(out)


def _render_html_lessons(lessons: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for r in lessons:
        sev_emoji = {1: "🔴", 2: "🟡", 3: "🟢"}.get(r.get("severity"), "⚪")
        head = f'{sev_emoji} sev{r.get("severity")} · {_html.escape(str(r.get("last_seen_at", "")))}'
        rows = [f'<div class="row"><b>Symptom:</b> {_html.escape(r["symptom"])}</div>']
        if r.get("root_cause"):
            rows.append(f'<div class="row"><b>Root cause:</b> {_html.escape(r["root_cause"])}</div>')
        if r.get("fix"):
            rows.append(f'<div class="row"><b>Fix:</b> {_html.escape(r["fix"])}</div>')
        out.append(f'<div class="lesson"><div class="sym">{head}</div>{"".join(rows)}</div>')
    return "".join(out)
