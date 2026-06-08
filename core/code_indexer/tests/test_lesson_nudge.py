"""Tests for the auto-memory lesson-hygiene nudge.

Covers:
- no lessons → no nudge (first-time users aren't pestered)
- recent lesson within threshold → no nudge
- lessons N commits behind HEAD (N >= threshold) → nudge appears
- COSMOS_LESSON_NUDGE_THRESHOLD env override is honored

Run with:
    .venv/bin/python -m core.code_indexer.tests.test_lesson_nudge
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from core.code_indexer import errors as errors_mod  # noqa: E402
from core.code_indexer.errors import (  # noqa: E402
    _lesson_nudge_threshold,
    commits_since,
    lesson_hygiene_nudge,
    lessons_staleness_summary,
)


# ── Fixtures ────────────────────────────────────────────────────────────

def _make_repo(tmp: Path) -> str:
    """Init a git repo with one commit. Returns the commit hash."""
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp, check=True)
    (tmp / "README").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True, check=True,
    )
    return head.stdout.strip()


def _bump(tmp: Path, n: int) -> None:
    """Add `n` empty commits past HEAD."""
    for i in range(n):
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", f"bump {i}"],
            cwd=tmp, check=True,
        )


def _stub_errors_with(rows: list[dict]) -> SimpleNamespace:
    """Spin up an in-memory sqlite mirroring the code_errors schema so the
    helper's COUNT + SELECT path runs without touching the real brain.db."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE code_errors (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            symptom TEXT,
            commit_hash TEXT,
            last_seen_at TEXT,
            disabled INTEGER NOT NULL DEFAULT 0
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO code_errors (id, project_id, symptom, commit_hash, "
            "last_seen_at, disabled) VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], r["project_id"], r.get("symptom", ""),
             r.get("commit_hash"), r.get("last_seen_at", "2026-05-18T00:00:00Z"),
             int(r.get("disabled", 0))),
        )
    conn.commit()
    return SimpleNamespace(conn=conn)


# ── Tests ────────────────────────────────────────────────────────────────

def test_threshold_default_is_5():
    os.environ.pop("COSMOS_LESSON_NUDGE_THRESHOLD", None)
    assert _lesson_nudge_threshold() == 5


def test_threshold_env_override():
    os.environ["COSMOS_LESSON_NUDGE_THRESHOLD"] = "2"
    try:
        assert _lesson_nudge_threshold() == 2
    finally:
        os.environ.pop("COSMOS_LESSON_NUDGE_THRESHOLD", None)


def test_threshold_env_bad_value_falls_back():
    os.environ["COSMOS_LESSON_NUDGE_THRESHOLD"] = "not-an-int"
    try:
        assert _lesson_nudge_threshold() == 5
    finally:
        os.environ.pop("COSMOS_LESSON_NUDGE_THRESHOLD", None)


def test_no_lessons_returns_zero_count(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_repo(tmp_path)
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with([]))
        summary = lessons_staleness_summary(str(tmp_path), "proj-empty")
        assert summary is not None
        assert summary["lesson_count"] == 0
        assert summary["commits_since_latest"] is None

        nudge = lesson_hygiene_nudge(str(tmp_path), "proj-empty")
        assert nudge is None, "no lessons → no nudge (don't pester new users)"


def test_recent_lesson_under_threshold_no_nudge(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head = _make_repo(tmp_path)
        _bump(tmp_path, 2)  # 2 commits past the lesson — below default 5
        rows = [{"id": "l1", "project_id": "p1", "commit_hash": head,
                 "last_seen_at": "2026-05-10T00:00:00Z"}]
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with(rows))

        summary = lessons_staleness_summary(str(tmp_path), "p1")
        assert summary["lesson_count"] == 1
        assert summary["commits_since_latest"] == 2

        nudge = lesson_hygiene_nudge(str(tmp_path), "p1")
        assert nudge is None, "2 commits < default threshold 5 → no nudge"


def test_drifted_lessons_trigger_nudge(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head = _make_repo(tmp_path)
        _bump(tmp_path, 8)  # 8 commits past — above default 5
        rows = [{"id": "l1", "project_id": "p1", "commit_hash": head,
                 "last_seen_at": "2026-04-01T00:00:00Z"}]
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with(rows))

        nudge = lesson_hygiene_nudge(str(tmp_path), "p1")
        assert nudge is not None, "8 commits ≥ threshold 5 → nudge should fire"
        assert "8 commits since the last lesson" in nudge
        assert "code_remember_error" in nudge
        assert "2026-04-01" in nudge  # timestamp surfaces in the message


def test_env_threshold_override_lowers_bar(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head = _make_repo(tmp_path)
        _bump(tmp_path, 2)
        rows = [{"id": "l1", "project_id": "p1", "commit_hash": head,
                 "last_seen_at": "2026-05-10T00:00:00Z"}]
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with(rows))

        os.environ["COSMOS_LESSON_NUDGE_THRESHOLD"] = "1"
        try:
            nudge = lesson_hygiene_nudge(str(tmp_path), "p1")
            assert nudge is not None, "threshold 1 + 2 commits → nudge"
        finally:
            os.environ.pop("COSMOS_LESSON_NUDGE_THRESHOLD", None)


def test_lesson_with_null_commit_hash_no_nudge(monkeypatch):
    """Old lessons (recorded before commit_hash was added in 2026-05-03) have
    NULL commit_hash — we can't compute drift, so don't nudge."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_repo(tmp_path)
        _bump(tmp_path, 10)
        rows = [{"id": "l1", "project_id": "p1", "commit_hash": None,
                 "last_seen_at": "2026-04-01T00:00:00Z"}]
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with(rows))

        summary = lessons_staleness_summary(str(tmp_path), "p1")
        # lesson exists but has no usable hash → commits_since_latest is None
        assert summary["lesson_count"] == 1
        assert summary["commits_since_latest"] is None

        nudge = lesson_hygiene_nudge(str(tmp_path), "p1")
        assert nudge is None, "null commit_hash → can't compute drift → silent"


def test_disabled_lessons_excluded_from_count(monkeypatch):
    """Disabled lessons (soft-deleted via the Lessons UI) shouldn't count
    toward 'has lessons' — a project with only disabled ones is effectively
    fresh, so we shouldn't nudge."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head = _make_repo(tmp_path)
        _bump(tmp_path, 10)
        rows = [{"id": "l1", "project_id": "p1", "commit_hash": head,
                 "last_seen_at": "2026-04-01T00:00:00Z", "disabled": 1}]
        monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with(rows))

        summary = lessons_staleness_summary(str(tmp_path), "p1")
        assert summary["lesson_count"] == 0

        nudge = lesson_hygiene_nudge(str(tmp_path), "p1")
        assert nudge is None


def test_missing_project_path_returns_none(monkeypatch):
    monkeypatch.setattr(errors_mod, "get_code_errors", lambda: _stub_errors_with([]))
    assert lessons_staleness_summary(None, "p1") is None
    assert lessons_staleness_summary("", "p1") is None
    assert lessons_staleness_summary("/some/path", None) is None
    assert lesson_hygiene_nudge(None, "p1") is None


# ── Runner ───────────────────────────────────────────────────────────────

def _monkeypatch_factory():
    """Light-weight stand-in for pytest's monkeypatch — these tests run
    via the standalone runner, no pytest dependency."""
    saved: list[tuple] = []

    def patch(target, attr, value):
        saved.append((target, attr, getattr(target, attr)))
        setattr(target, attr, value)

    def undo():
        while saved:
            target, attr, original = saved.pop()
            setattr(target, attr, original)

    patch.undo = undo
    patch.setattr = patch
    return patch


TESTS = [
    test_threshold_default_is_5,
    test_threshold_env_override,
    test_threshold_env_bad_value_falls_back,
    test_no_lessons_returns_zero_count,
    test_recent_lesson_under_threshold_no_nudge,
    test_drifted_lessons_trigger_nudge,
    test_env_threshold_override_lowers_bar,
    test_lesson_with_null_commit_hash_no_nudge,
    test_disabled_lessons_excluded_from_count,
    test_missing_project_path_returns_none,
]


def main() -> int:
    import inspect

    passed = 0
    failed: list[tuple] = []

    for fn in TESTS:
        sig = inspect.signature(fn)
        wants_mp = "monkeypatch" in sig.parameters
        mp = _monkeypatch_factory() if wants_mp else None
        try:
            if wants_mp:
                fn(mp)
            else:
                fn()
            print(f"  ✅ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            failed.append((fn.__name__, str(e)))
            print(f"  ❌ {fn.__name__} — {e}")
        except Exception as e:
            failed.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  💥 {fn.__name__} — {type(e).__name__}: {e}")
        finally:
            if mp is not None:
                mp.undo()

    print()
    print(f"Lesson-nudge tests: {passed} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
