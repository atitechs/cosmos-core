"""Tests for the lesson ranker (score_lesson_for_query).

These pin the relative weights documented in errors.py — if we tune them
later we should also update these tests so the contract stays explicit.

Run with:
    .venv/bin/python -m core.code_indexer.tests.test_lesson_ranker
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from core.code_indexer.errors import (  # noqa: E402
    score_lesson_for_query,
    _glob_to_regex,
    _path_matches_globs,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _lesson(**overrides) -> dict:
    """Default lesson with neutral fields the test can override."""
    base = {
        "id": "test-lesson",
        "symptom": "Telegram bot fails to start in bundled .dmg",
        "root_cause": "PyInstaller excluded telegram",
        "fix": "Add telegram to HIDDEN_IMPORTS",
        "files_affected": [],
        "tags": [],
        "scope_globs": [],
        "severity": 2,
        "pinned": False,
        "disabled": False,
        "last_seen_at": _now_iso(),
    }
    base.update(overrides)
    return base


def _tokens(symptom: str) -> set[str]:
    import re
    return set(re.findall(r"[A-Za-z0-9_]{3,}|[฀-๿]{2,}", symptom.lower()))


# ── Tests ────────────────────────────────────────────────────────────────

def test_token_overlap_drives_baseline_score():
    l = _lesson()
    s = score_lesson_for_query(l, symptom_tokens=_tokens("Telegram bot bundled"))
    # 3 token hits ("telegram", "bot", "bundled") + 0.3 recency
    assert 3.0 <= s <= 3.5, f"expected ~3.3, got {s}"


def test_pinned_adds_bias():
    base = score_lesson_for_query(_lesson(), symptom_tokens=_tokens("telegram"))
    pinned = score_lesson_for_query(
        _lesson(pinned=True), symptom_tokens=_tokens("telegram"),
    )
    assert pinned - base >= 0.55, "pinned should add ~+0.6"


def test_severity_3_sinks():
    cosmetic = score_lesson_for_query(_lesson(severity=3), symptom_tokens=_tokens("telegram"))
    normal = score_lesson_for_query(_lesson(severity=2), symptom_tokens=_tokens("telegram"))
    assert cosmetic < normal, "severity 3 should score lower than severity 2"


def test_files_affected_direct_match_boosts():
    no_path = score_lesson_for_query(
        _lesson(files_affected=["scripts/build-sidecar.sh"]),
        symptom_tokens=_tokens("telegram"),
    )
    with_path = score_lesson_for_query(
        _lesson(files_affected=["scripts/build-sidecar.sh"]),
        symptom_tokens=_tokens("telegram"),
        current_path="scripts/build-sidecar.sh",
    )
    assert with_path - no_path >= 0.7, "direct file match should add ~+0.8"


def test_files_affected_prefix_match_smaller_boost():
    plain = score_lesson_for_query(
        _lesson(files_affected=["scripts/build-sidecar.sh"]),
        symptom_tokens=_tokens("telegram"),
    )
    prefix = score_lesson_for_query(
        _lesson(files_affected=["scripts/build-sidecar.sh"]),
        symptom_tokens=_tokens("telegram"),
        current_path="scripts/other.sh",
    )
    # 'scripts/other.sh' shares directory with files_affected → +0.4
    assert 0.3 < (prefix - plain) < 0.5, f"prefix match should add ~+0.4, got Δ={prefix - plain}"


def test_scope_glob_matches_outweighs_other_signals():
    no_match = score_lesson_for_query(
        _lesson(scope_globs=["src/api/**"]),
        symptom_tokens=_tokens("telegram"),
        current_path="src/components/Foo.tsx",
    )
    matched = score_lesson_for_query(
        _lesson(scope_globs=["src/api/**"]),
        symptom_tokens=_tokens("telegram"),
        current_path="src/api/auth.py",
    )
    assert matched - no_match >= 1.4, "glob match should add ~+1.5"


def test_recency_bonus_only_within_window():
    fresh = _lesson(last_seen_at=_now_iso())
    stale = _lesson(last_seen_at=(datetime.now(timezone.utc) - timedelta(days=120))
                    .isoformat(timespec="seconds").replace("+00:00", "Z"))
    fs = score_lesson_for_query(fresh, symptom_tokens=_tokens("telegram"))
    ss = score_lesson_for_query(stale, symptom_tokens=_tokens("telegram"))
    assert fs - ss >= 0.25, f"fresh should outscore stale by ~0.3, got Δ={fs - ss}"


def test_zero_token_match_still_can_score_if_path_matches():
    """A lesson with no symptom-token overlap can still surface if scope_glob
    or files_affected directly hits the current path. This is the "always
    remind for X" workflow."""
    s = score_lesson_for_query(
        _lesson(scope_globs=["src/api/**"]),
        symptom_tokens=_tokens("totally unrelated query"),
        current_path="src/api/auth.py",
    )
    assert s >= 1.0, f"glob-only score should still be > 0, got {s}"


def test_disabled_filtered_at_source():
    """Caller is responsible for not passing disabled rows. Function still
    handles them gracefully if it gets one — score normally; downstream
    behaves as if it weren't there."""
    s = score_lesson_for_query(
        _lesson(disabled=True), symptom_tokens=_tokens("telegram"),
    )
    # We don't assign a special penalty for disabled — the caller filters.
    assert s > 0, "disabled is upstream's concern, not the ranker's"


# ── Glob unit tests ──────────────────────────────────────────────────────

def test_glob_double_star_recursive():
    assert _path_matches_globs("src/api/auth.py", ["src/**"])
    assert _path_matches_globs("src/api/sub/auth.py", ["src/**"])
    # "src/**" requires at least the slash + something after it. Bare "src"
    # is not a descendant of itself — that matches conventional glob
    # behavior in tools like rg / fd / git pathspec.
    assert not _path_matches_globs("src", ["src/**"])
    assert not _path_matches_globs("other/api.py", ["src/**"])


def test_glob_single_star_one_segment():
    assert _path_matches_globs("foo.py", ["*.py"])
    assert not _path_matches_globs("dir/foo.py", ["*.py"])


def test_glob_question_mark():
    assert _path_matches_globs("a.py", ["?.py"])
    assert not _path_matches_globs("ab.py", ["?.py"])


def test_glob_empty_globs_never_matches():
    assert not _path_matches_globs("any/path.py", [])
    assert not _path_matches_globs("any/path.py", [""])


# ── Runner ───────────────────────────────────────────────────────────────

def main() -> int:
    tests = [g for g in globals() if g.startswith("test_")]
    passed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"   ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"   ❌ {name}: {e}")
        except Exception as e:
            print(f"   💥 {name}: {type(e).__name__}: {e}")

    print("=" * 60)
    print(f"📊 Lesson ranker: {passed}/{len(tests)} passed")
    print("=" * 60)
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
