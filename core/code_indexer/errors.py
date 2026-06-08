"""
Code Errors — per-project error log
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stores symptoms/fixes that an AI agent (or user) has seen while working on a
watched project, so future sessions can recall them BEFORE making edits and
avoid repeating mistakes.

Linked to ProjectRegistry via `project_id` (UUID from BrainManifest). Auto-
routing: given a `cwd` or absolute `path`, the longest matching watched-project
prefix wins.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any


def _log(message: str):
    """Log away from stdout so MCP stdio JSON-RPC stays clean."""
    print(message, file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(text: Optional[str]) -> List:
    if not text:
        return []
    try:
        return json.loads(text) or []
    except (ValueError, TypeError):
        return []


def _resolve_project_path(project_id: str) -> Optional[str]:
    """Reverse lookup: project_id → absolute path. Used to find where to append
    the per-project .aibran/lessons.md file."""
    try:
        from core.code_indexer.project_registry import get_project_registry
        registry = get_project_registry()
        for proj in registry.list():
            if proj["id"] == project_id:
                return os.path.abspath(proj["path"])
    except Exception:
        return None
    return None


_SEV_LABEL = {1: "🔴 critical", 2: "🟡 normal", 3: "🟢 minor"}


def _format_lesson_md(entry: Dict) -> str:
    """Render a single lesson entry as markdown — a section in the project
    lessons file. Designed to be readable by both humans and AI tools that
    can't query MCP."""
    sev = int(entry.get("severity") or 2)
    sev_label = _SEV_LABEL.get(sev, "")
    tags = entry.get("tags") or []
    tag_str = ", ".join(f"`{t}`" for t in tags) if tags else "—"
    files = entry.get("files_affected") or []
    files_str = ", ".join(f"`{f}`" for f in files) if files else "—"

    symptom = (entry.get("symptom") or "").strip()
    title = symptom.split("\n")[0][:90]
    date = (entry.get("created_at") or "").split("T")[0]
    pin_marker = "📌 " if entry.get("pinned") else ""
    times = int(entry.get("times_seen") or 0)
    times_str = f" · seen {times}×" if times > 1 else ""

    parts = [
        f"### {pin_marker}{title}",
        "",
        f"_{date} · severity {sev} {sev_label}{times_str} · tags: {tag_str}_",
        "",
        "**Symptom:** " + symptom,
    ]
    if entry.get("root_cause"):
        parts.append("")
        parts.append("**Root cause:** " + str(entry["root_cause"]))
    if entry.get("fix"):
        parts.append("")
        parts.append("**Fix:** " + str(entry["fix"]))
    parts.append("")
    parts.append(f"**Files:** {files_str}")
    globs = entry.get("scope_globs") or []
    if globs:
        parts.append("")
        parts.append("**Always remind for:** " + ", ".join(f"`{g}`" for g in globs))
    parts.append("")
    parts.append(f"<!-- lesson-id: {entry.get('id', '')} -->")
    parts.append("")
    return "\n".join(parts)


def _format_lessons_md(project_name: str, lessons: List[Dict]) -> str:
    """Build the full lessons.md document. Pinned lessons go first under
    their own header, then everything else by severity ASC, last_seen DESC.

    The frontmatter at the top is plain YAML so any tool that reads
    markdown frontmatter (Obsidian, Cursor's @-mention, etc.) can pick up
    project metadata without parsing the body.
    """
    from datetime import datetime, timezone
    pinned = [l for l in lessons if l.get("pinned")]
    active = [l for l in lessons if not l.get("pinned")]

    # Both sub-sections are ordered: severity 1 (critical) first, then
    # most-recently-seen first within the same severity.
    def _sort_key(l: Dict):
        return (int(l.get("severity") or 2), -1 * (l.get("last_seen_at") or "" > ""))
    pinned.sort(key=lambda l: (int(l.get("severity") or 2), -(l.get("times_seen") or 0)))
    active.sort(key=lambda l: (int(l.get("severity") or 2), l.get("last_seen_at") or ""), reverse=False)
    # Within active, we still want recent-first when severity ties:
    active.sort(key=lambda l: (int(l.get("severity") or 2), -(int(_iso_to_unix(l.get("last_seen_at"))))))

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    out: List[str] = []
    out.append("---")
    out.append(f"project: {project_name}")
    out.append("generated_by: cosmos")
    out.append(f"generated_at: {now_iso}")
    out.append(f"lesson_count: {len(lessons)}")
    out.append("schema_version: 1")
    out.append("---")
    out.append("")
    out.append(f"# 🧠 Project Lessons — {project_name}")
    out.append("")
    out.append("> Auto-synced by [Cosmos](https://atitechs.com). The Cosmos brain DB")
    out.append("> is the source of truth — this file is a live mirror for AI tools")
    out.append("> that don't speak MCP, for `grep`-style discovery, and for sharing")
    out.append("> via git.")
    out.append(">")
    out.append("> **Read before editing files in this project** to avoid re-deriving")
    out.append("> fixes that already shipped. Disabled lessons are excluded; manage")
    out.append("> them from the Cosmos app → Project Lessons tab.")
    out.append("")

    if not lessons:
        out.append("_No lessons yet — this file repopulates automatically as soon as_")
        out.append("_you (or your AI) call_ `code_remember_error`.")
        out.append("")
        return "\n".join(out)

    if pinned:
        out.append(f"## 📌 Pinned ({len(pinned)})")
        out.append("")
        out.append("_These surface first whenever your AI calls_ `find_relevant_code`.")
        out.append("")
        for entry in pinned:
            out.append(_format_lesson_md(entry))
            out.append("---")
            out.append("")

    if active:
        out.append(f"## Active ({len(active)})")
        out.append("")
        for entry in active:
            out.append(_format_lesson_md(entry))
            out.append("---")
            out.append("")

    return "\n".join(out)


def _format_lessons_json(project_name: str, lessons: List[Dict]) -> str:
    """Compact JSON mirror — same data the MCP tools see, sans disabled rows.
    Useful for AI-tool plugins that prefer structured input over markdown."""
    from datetime import datetime, timezone
    payload = {
        "schema_version": 1,
        "project": project_name,
        "generated_by": "cosmos",
        "generated_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "lesson_count": len(lessons),
        "lessons": [
            {
                "id": l.get("id"),
                "symptom": l.get("symptom"),
                "root_cause": l.get("root_cause"),
                "fix": l.get("fix"),
                "files_affected": l.get("files_affected") or [],
                "tags": l.get("tags") or [],
                "scope_globs": l.get("scope_globs") or [],
                "severity": int(l.get("severity") or 2),
                "pinned": bool(l.get("pinned")),
                "times_seen": int(l.get("times_seen") or 0),
                "created_at": l.get("created_at"),
                "last_seen_at": l.get("last_seen_at"),
                "commit_hash": l.get("commit_hash"),
            }
            for l in lessons
        ],
    }
    import json as _json
    return _json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _iso_to_unix(s: Optional[str]) -> int:
    """Convert an ISO timestamp to a unix epoch int. 0 if missing/malformed —
    used as a sort key, so 0 sinks naturally to the bottom."""
    if not s:
        return 0
    try:
        from datetime import datetime
        # Strip timezone suffix variants — sort just needs ordering.
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


_XML_TAG_RX = re.compile(
    r"</?(?:symptom|root_cause|fix|files_affected|tags|parameter|invoke|"
    r"function_calls|function_results|tool_use|tool_result)"
    r"(?:\s+[a-z_-]+\s*=\s*\"[^\"]*\")*\s*/?>",
    re.IGNORECASE,
)


def _strip_xml_tags(text: Optional[str]) -> Optional[str]:
    """Remove the specific XML/HTML tags that leak into lesson fields when
    an AI tool-call serialization mishap dumps its raw args into the
    symptom/root_cause/fix slots. Conservative — only strips known
    Cosmos / MCP-shaped tags so user prose with `<` `>` survives.

    See incident 2026-05-07: lesson 42ada388 had </symptom> + <parameter
    name="root_cause"> embedded inside its symptom field. Caller's add()
    funnels every text field through this before INSERT.
    """
    if text is None:
        return None
    cleaned = _XML_TAG_RX.sub("", text)
    # Collapse the blank gap left by tag removal.
    cleaned = re.sub(r"\n\s*\n", "\n\n", cleaned)
    return cleaned.strip() or None


def _atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` via tmp + rename so a crash mid-write
    can't truncate the user's lessons file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def rebuild_lesson_files(project_path: str, project_name: str, lessons: List[Dict]) -> None:
    """Rebuild both .cosmos/lessons.md and .cosmos/lessons.json from a fresh
    list of lessons. Best-effort — never raises.

    `lessons` should be the *visible* set (i.e. disabled rows already
    excluded by the caller) so the file mirror never leaks lessons the
    user told us to hide.
    """
    try:
        cosmos_dir = os.path.join(project_path, ".cosmos")
        os.makedirs(cosmos_dir, exist_ok=True)

        # README.md once, only if missing — explains the folder so devs who
        # find it via git diff don't have to guess what wrote it.
        readme_path = os.path.join(cosmos_dir, "README.md")
        if not os.path.exists(readme_path):
            _atomic_write(readme_path,
                "# .cosmos/\n\n"
                "Auto-managed by [Cosmos](https://atitechs.com).\n\n"
                "- `lessons.md` — human-readable lesson library (the same data the\n"
                "  Cosmos MCP server feeds Claude / Cursor / Cline / Windsurf)\n"
                "- `lessons.json` — machine-readable mirror, useful for editor\n"
                "  plugins that prefer structured input\n\n"
                "Source of truth lives in the Cosmos brain DB; this folder is a\n"
                "live mirror that rebuilds on every lesson change. Safe to delete\n"
                "— Cosmos will recreate it.\n\n"
                "Commit or git-ignore this folder per your team's preference.\n"
            )

        _atomic_write(
            os.path.join(cosmos_dir, "lessons.md"),
            _format_lessons_md(project_name, lessons),
        )
        _atomic_write(
            os.path.join(cosmos_dir, "lessons.json"),
            _format_lessons_json(project_name, lessons),
        )
    except (IOError, OSError) as e:
        _log(f"[errors] warning: could not rebuild lesson files at {project_path}: {e}")


def _project_name_for(project_id: str) -> str:
    """Look up the human name for a project_id. Falls back to the id itself
    if the project was unwatched between writes."""
    try:
        from core.code_indexer.project_registry import get_project_registry
        for p in get_project_registry().list():
            if p["id"] == project_id:
                return p.get("name") or project_id
    except Exception:
        pass
    return project_id


def _git_head(project_path: str) -> Optional[str]:
    """Capture current git HEAD commit hash. None if path isn't a git repo or
    git isn't available — lessons stay valid even outside version control."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def commits_since(project_path: str, commit_hash: str) -> Optional[int]:
    """Count commits between `commit_hash` and current HEAD. None on failure
    (no git, missing commit, etc.)."""
    if not commit_hash or not project_path:
        return None
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{commit_hash}..HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def lessons_staleness_summary(project_path: Optional[str],
                              project_id: Optional[str]) -> Optional[Dict]:
    """Summarize how stale a project's lesson set is vs git HEAD.

    Returns None if path/id missing. Otherwise:
        {
          "lesson_count": int,
          "latest_commit_hash": str | None,
          "latest_lesson_at": str | None,
          "commits_since_latest": int | None,
        }

    Powers the auto-memory nudge — find_relevant_code / code_list_errors call
    this to detect when the project has drifted since the last recorded fix
    and surface a hint so the AI logs the next lesson via code_remember_error.
    """
    if not project_path or not project_id:
        return None

    svc = get_code_errors()
    try:
        cur = svc.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM code_errors "
            "WHERE project_id = ? AND COALESCE(disabled, 0) = 0",
            (project_id,),
        )
        lesson_count = int((cur.fetchone() or (0,))[0] or 0)
    except Exception:
        return None

    if lesson_count == 0:
        return {
            "lesson_count": 0,
            "latest_commit_hash": None,
            "latest_lesson_at": None,
            "commits_since_latest": None,
        }

    latest_hash: Optional[str] = None
    latest_at: Optional[str] = None
    try:
        cur.execute(
            "SELECT commit_hash, last_seen_at FROM code_errors "
            "WHERE project_id = ? AND COALESCE(disabled, 0) = 0 "
            "  AND commit_hash IS NOT NULL AND commit_hash != '' "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
        if row:
            latest_hash, latest_at = row[0], row[1]
    except Exception:
        pass

    commits = commits_since(project_path, latest_hash) if latest_hash else None
    return {
        "lesson_count": lesson_count,
        "latest_commit_hash": latest_hash,
        "latest_lesson_at": latest_at,
        "commits_since_latest": commits,
    }


def _lesson_nudge_threshold() -> int:
    """Min `commits_since_latest` before we nudge. Env-tunable so users with
    chatty repos can raise it, and tests can clamp it down."""
    raw = os.environ.get("COSMOS_LESSON_NUDGE_THRESHOLD")
    if not raw:
        return 5
    try:
        n = int(raw.strip())
        return max(1, n)
    except (ValueError, TypeError):
        return 5


def lesson_hygiene_nudge(project_path: Optional[str],
                         project_id: Optional[str]) -> Optional[str]:
    """Markdown nudge block when the project has drifted since the last
    lesson — or None when no nudge is warranted.

    Triggers ONLY when:
    - the project has ≥1 lesson recorded (first-time users aren't pestered)
    - the newest lesson's commit is ≥ threshold commits behind HEAD
    - git is available (commits_since returned a real int)

    Designed for find_relevant_code / code_list_errors output — appended at
    the end so the AI sees it after digesting results.
    """
    summary = lessons_staleness_summary(project_path, project_id)
    if not summary:
        return None
    n = summary.get("commits_since_latest")
    if n is None or n < _lesson_nudge_threshold():
        return None
    when = summary.get("latest_lesson_at") or "unknown"
    lines = [
        "## 📝 Lesson hygiene",
        "",
        f"⚠️ **{n} commits since the last lesson was recorded** "
        f"(latest: {when}).",
        "",
        "If any of those commits fixed a non-obvious bug, call "
        "`code_remember_error` to log it so future sessions inherit the "
        "lesson. Skip for typos / trial-and-error / one-line fixes. "
        "Triggers: root cause differed from the error message, took >1 try, "
        "user reported a bug after your work compiled, or any "
        '"wouldn\'t have guessed that" surprise.',
    ]
    return "\n".join(lines)


def _glob_to_regex(pat: str) -> str:
    """Convert a shell-style glob ('src/api/**', '*.py') to an anchored
    Python regex. Supports * (one segment) and ** (any number of segments
    incl. empty). Used by scope_globs matching — lighter than fnmatch
    because we want recursive ** semantics fnmatch doesn't give us.
    """
    import re as _re
    out: List[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                # ** matches across path separators (zero or more segments)
                out.append(".*")
                i += 2
                # Eat a following slash so 'src/**' also matches 'src'.
                if i < len(pat) and pat[i] == "/":
                    i += 1
            else:
                # Single * matches inside one segment only
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in r".+()[]{}|^$\\":
            out.append("\\" + c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "^" + "".join(out) + "$"


def _path_matches_globs(path: str, globs: List[str]) -> bool:
    """True if `path` matches any glob in `globs`. Empty list → False."""
    import re as _re
    if not path or not globs:
        return False
    for g in globs:
        if not g:
            continue
        try:
            if _re.search(_glob_to_regex(g), path):
                return True
        except _re.error:
            continue
    return False


def score_lesson_for_query(
    lesson: Dict,
    *,
    symptom_tokens: set,
    current_path: Optional[str] = None,
) -> float:
    """Rank a lesson against a free-text symptom + optional current path.

    Score components — additive so they're easy to debug. Higher = surface
    earlier. Empirically tuned but documented: tweak with eyes open.

      +1.0 per symptom token that hits the lesson body / tags
      +1.5 if `current_path` matches a scope_glob (explicit "always remind")
      +0.8 if `current_path` is in files_affected directly
      +0.4 if `current_path` shares a directory prefix with files_affected
      +0.6 if pinned (user said "this matters")
      -0.5 if severity == 3 (cosmetic) — break ties downward
      +0.3 if last_seen_at was within 30 days (recency)

    Disabled lessons should never be passed in — caller filters them out.
    """
    from datetime import datetime, timezone, timedelta
    score = 0.0

    # 1. Token overlap on symptom + root_cause + fix + tags
    haystack = " ".join([
        (lesson.get("symptom") or ""),
        (lesson.get("root_cause") or ""),
        (lesson.get("fix") or ""),
        " ".join(lesson.get("tags") or []),
    ]).lower()
    score += sum(1.0 for tok in symptom_tokens if tok in haystack)

    # 2. Path-based bonuses — only apply when caller passed a path
    if current_path:
        import os as _os
        files = lesson.get("files_affected") or []
        # Direct hit: same path string anywhere in files_affected. We accept
        # both "scripts/build.sh" and an absolute version that ends with it.
        direct = any(
            current_path == f or current_path.endswith("/" + f) or f.endswith("/" + current_path)
            for f in files
        )
        if direct:
            score += 0.8
        else:
            # Prefix hit: current path lives in (or near) a directory the
            # lesson already touched. We compute the parent dir of each
            # affected file and check whether current_path sits under it.
            cur_dir = _os.path.dirname(current_path)
            shared = False
            for f in files:
                f_dir = _os.path.dirname(f)
                if f_dir and (
                    current_path.startswith(f_dir + "/")
                    or (cur_dir and (cur_dir == f_dir or cur_dir.startswith(f_dir + "/") or f_dir.startswith(cur_dir + "/")))
                ):
                    shared = True
                    break
            if shared:
                score += 0.4

        globs = lesson.get("scope_globs") or []
        if _path_matches_globs(current_path, globs):
            score += 1.5

    # 3. Pinned bias
    if lesson.get("pinned"):
        score += 0.6

    # 4. Severity tiebreak — cosmetic lessons sink
    if int(lesson.get("severity") or 2) == 3:
        score -= 0.5

    # 5. Recency boost
    last = lesson.get("last_seen_at")
    if last:
        try:
            ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - ts < timedelta(days=30):
                score += 0.3
        except Exception:
            pass

    return score


def resolve_project_id(path: str, registry=None) -> Optional[str]:
    """
    Map an absolute path (typically a session cwd) to the watched project that
    contains it. Longest-prefix wins so nested watched projects pick the inner.
    Returns None if no watched project owns this path.
    """
    if not path:
        return None
    if registry is None:
        from core.code_indexer.project_registry import get_project_registry
        registry = get_project_registry()

    target = os.path.abspath(os.path.expanduser(path))
    best: Optional[Dict] = None
    best_len = -1
    for proj in registry.list():
        ppath = os.path.abspath(proj["path"])
        if target == ppath or target.startswith(ppath + os.sep):
            if len(ppath) > best_len:
                best, best_len = proj, len(ppath)
    return best["id"] if best else None


class CodeErrors:
    """CRUD + filters for the `code_errors` table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── Write ──
    def add(self, *, project_id: str, symptom: str,
            root_cause: Optional[str] = None,
            fix: Optional[str] = None,
            files_affected: Optional[List[str]] = None,
            tags: Optional[List[str]] = None,
            severity: int = 2) -> Dict:
        if not project_id:
            raise ValueError("project_id is required")
        if not symptom or not symptom.strip():
            raise ValueError("symptom must not be empty")

        # XML-tag sanitization — when an AI mistakenly sends arguments as
        # XML-formatted text (e.g. its tool-call output got serialized
        # with </symptom><parameter name="root_cause">…), the entire
        # blob ends up in the symptom field. Strip the tags so the
        # lesson is at least readable. Recovered lesson 42ada388 had
        # exactly this shape; this guard prevents recurrence.
        symptom = _strip_xml_tags(symptom)
        root_cause = _strip_xml_tags(root_cause) if root_cause else root_cause
        fix = _strip_xml_tags(fix) if fix else fix

        error_id = str(uuid.uuid4())
        now = _now_iso()
        sev = max(1, min(3, int(severity)))

        # Capture current git HEAD so future sessions can detect when the lesson
        # might be stale (commits since this hash > N).
        project_path = _resolve_project_path(project_id)
        commit_hash = _git_head(project_path) if project_path else None

        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO code_errors
            (id, project_id, symptom, root_cause, fix, files_affected, tags,
             severity, times_seen, created_at, last_seen_at, commit_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """, (
            error_id, project_id, symptom.strip(),
            (root_cause or "").strip() or None,
            (fix or "").strip() or None,
            _json_dumps(files_affected),
            _json_dumps(tags),
            sev, now, now, commit_hash,
        ))
        self.conn.commit()
        entry = self.get(error_id)

        # Best-effort: rebuild the project's .cosmos/ mirror so external AI
        # tools (Cursor/Aider/Copilot/Claude-without-MCP) see the latest
        # lesson set. Full rebuild rather than append — keeps the file
        # consistent with edits/disables/deletes that happen via the UI.
        if project_path and entry:
            self._rebuild_for_project(project_id)

        return entry

    def update(self, error_id: str, **fields) -> Optional[Dict]:
        allowed = {"symptom", "root_cause", "fix", "files_affected",
                   "tags", "severity", "pinned", "disabled", "scope_globs"}
        clean: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k in ("files_affected", "tags", "scope_globs"):
                clean[k] = _json_dumps(v)
            elif k == "severity":
                clean[k] = max(1, min(3, int(v)))
            elif k in ("pinned", "disabled"):
                clean[k] = 1 if bool(v) else 0
            else:
                clean[k] = v
        if not clean:
            return self.get(error_id)

        sets = ", ".join(f"{k} = ?" for k in clean)
        values = list(clean.values()) + [error_id]
        cur = self.conn.cursor()
        cur.execute(f"UPDATE code_errors SET {sets} WHERE id = ?", values)
        self.conn.commit()
        if not cur.rowcount:
            return None
        updated = self.get(error_id)
        # Mirror file rebuilds whenever we touch fields the file shows
        # (symptom/cause/fix/severity/pinned/disabled/files/tags). All of
        # `allowed` falls into that bucket, so any successful update bumps
        # the mirror.
        if updated and updated.get("project_id"):
            self._rebuild_for_project(updated["project_id"])
        return updated

    def touch(self, error_id: str) -> Optional[Dict]:
        """Increment times_seen + bump last_seen_at — call when a duplicate is detected."""
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE code_errors
            SET times_seen = times_seen + 1, last_seen_at = ?
            WHERE id = ?
        """, (_now_iso(), error_id))
        self.conn.commit()
        return self.get(error_id) if cur.rowcount else None

    def delete(self, error_id: str) -> bool:
        # Capture project_id BEFORE deleting so we know which mirror to rebuild.
        cur = self.conn.cursor()
        cur.execute("SELECT project_id FROM code_errors WHERE id = ?", (error_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM code_errors WHERE id = ?", (error_id,))
        self.conn.commit()
        if cur.rowcount > 0 and row:
            self._rebuild_for_project(row[0])
            return True
        return False

    def delete_for_project(self, project_id: str) -> int:
        """Used when a project is unwatched and the user opts to wipe its log."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM code_errors WHERE project_id = ?", (project_id,))
        self.conn.commit()
        # After wiping, rebuild produces an empty file — we keep .cosmos/
        # in place so the user knows the folder is "managed" rather than
        # leaving a stale full lesson file behind.
        if cur.rowcount > 0:
            self._rebuild_for_project(project_id)
        return cur.rowcount

    def _rebuild_for_project(self, project_id: str) -> None:
        """Re-render .cosmos/lessons.{md,json} for one project.

        Pulls the visible (non-disabled) lesson set fresh from the DB so
        the file always matches what the MCP recall tools would feed an
        AI agent. Best-effort — never raises into caller code paths.

        Skipped entirely when COSMOS_DISABLE_LESSON_MIRROR=1 — required
        guard for benchmark runs because the project_registry still
        points at the user's REAL repo even when the brain DB is a
        clone. Without this, a benchmark calling code_remember_error
        would clobber the operator's working .cosmos/ folder.
        """
        if not project_id:
            return
        from core.runtime_config import lesson_mirror_enabled
        if not lesson_mirror_enabled():
            return
        project_path = _resolve_project_path(project_id)
        if not project_path or not os.path.isdir(project_path):
            return
        try:
            visible = self.list_for_project(
                project_id, limit=10000, include_disabled=False,
            )
        except Exception:
            return
        rebuild_lesson_files(project_path, _project_name_for(project_id), visible)

    # ── Read ──
    def get(self, error_id: str) -> Optional[Dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM code_errors WHERE id = ?", (error_id,))
        row = cur.fetchone()
        return self._row_to_dict(cur, row) if row else None

    def list_for_project(self, project_id: str, *,
                         file_path: Optional[str] = None,
                         min_severity: int = 1,
                         limit: int = 100,
                         include_disabled: bool = False) -> List[Dict]:
        """List lessons for a project.

        Defaults exclude disabled rows because this method is called by the
        MCP tools that feed lessons to AI agents — disabled rows must never
        leak there. The Lessons UI explicitly opts-in via include_disabled
        so the user can still see and re-enable them.

        Order: pinned first (DESC), then severity ASC (1=critical first),
        then last_seen_at DESC.
        """
        cur = self.conn.cursor()
        disabled_clause = "" if include_disabled else "AND disabled = 0"
        if file_path:
            cur.execute(f"""
                SELECT * FROM code_errors
                WHERE project_id = ? AND severity >= ? {disabled_clause}
                ORDER BY pinned DESC, severity ASC, last_seen_at DESC
            """, (project_id, min_severity))
            kept: List[Dict] = []
            for raw in cur.fetchall():
                entry = self._row_to_dict(cur, raw)
                if file_path in (entry.get("files_affected") or []):
                    kept.append(entry)
                    if len(kept) >= limit:
                        break
            return kept

        cur.execute(f"""
            SELECT * FROM code_errors
            WHERE project_id = ? AND severity >= ? {disabled_clause}
            ORDER BY pinned DESC, severity ASC, last_seen_at DESC
            LIMIT ?
        """, (project_id, min_severity, limit))
        return [self._row_to_dict(cur, r) for r in cur.fetchall()]

    def list_all(self, *, search: Optional[str] = None,
                 limit: int = 500,
                 include_disabled: bool = True) -> List[Dict]:
        """Cross-project list, used by the Lessons UI.

        Defaults to include_disabled=True because the UI explicitly wants
        to surface disabled rows so the user can re-enable them. Search
        does a case-insensitive LIKE across symptom/root_cause/fix.
        """
        cur = self.conn.cursor()
        disabled_clause = "" if include_disabled else "WHERE disabled = 0"
        if search and search.strip():
            like = f"%{search.strip()}%"
            joiner = "AND" if disabled_clause else "WHERE"
            cur.execute(f"""
                SELECT * FROM code_errors
                {disabled_clause}
                {joiner} (
                    LOWER(symptom) LIKE LOWER(?) OR
                    LOWER(IFNULL(root_cause,'')) LIKE LOWER(?) OR
                    LOWER(IFNULL(fix,'')) LIKE LOWER(?)
                )
                ORDER BY pinned DESC, severity ASC, last_seen_at DESC
                LIMIT ?
            """, (like, like, like, limit))
        else:
            cur.execute(f"""
                SELECT * FROM code_errors
                {disabled_clause}
                ORDER BY pinned DESC, severity ASC, last_seen_at DESC
                LIMIT ?
            """, (limit,))
        return [self._row_to_dict(cur, r) for r in cur.fetchall()]

    def count_for_project(self, project_id: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM code_errors WHERE project_id = ?",
                    (project_id,))
        return cur.fetchone()[0]

    # ── helpers ──
    @staticmethod
    def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> Dict:
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, row))
        d["files_affected"] = _json_loads(d.get("files_affected"))
        d["tags"] = _json_loads(d.get("tags"))
        d["scope_globs"] = _json_loads(d.get("scope_globs"))
        # Schema stores 0/1 ints; expose to API consumers as proper bools so
        # the frontend doesn't need to coerce.
        d["pinned"] = bool(d.get("pinned"))
        d["disabled"] = bool(d.get("disabled"))
        return d


_singleton: Optional[CodeErrors] = None


def get_code_errors() -> CodeErrors:
    global _singleton
    if _singleton is None:
        from core.memory.store_v2 import get_store_v2
        _singleton = CodeErrors(get_store_v2().conn)
    return _singleton
