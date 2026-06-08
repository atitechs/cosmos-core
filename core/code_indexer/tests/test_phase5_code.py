"""
Phase 5 Code Indexer — Standalone Test Suite
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Verifies symbol-level granularity (functions/classes/methods are separate
units), cross-references, project overview, and the 9 MCP tools added in
this phase. Runs without pytest — follows the same pattern as the other
phase test suites in this repo.
"""
from __future__ import annotations
import os
import sys
import json
import tempfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, PROJECT_ROOT)

from core.memory.store_v2 import MemoryStoreV2
from core.code_indexer.indexer import CodeIndexer
from core.code_indexer.project_analyzer import ProjectAnalyzer


# ── Helpers ──
def run_test(name, fn):
    try:
        fn()
        print(f"   ✅ {name}")
        return True
    except AssertionError as e:
        print(f"   ❌ {name}: {e}")
        return False
    except Exception as e:
        print(f"   💥 {name}: {type(e).__name__}: {e}")
        return False


def _make_workspace(td: str) -> tuple[str, MemoryStoreV2]:
    """Build a small polyglot project + return (path, store)."""
    db_path = os.path.join(td, "test.db")
    store = MemoryStoreV2(db_path)

    with open(os.path.join(td, "main.py"), "w") as f:
        f.write(
            'import os\nfrom utils import helper\n\n'
            'class Calculator:\n'
            '    def add(self, a, b):\n'
            '        """Adds two numbers"""\n'
            '        return a + b\n\n'
            'def process():\n'
            '    c = Calculator()\n'
            '    return c.add(1, 2)\n'
        )

    with open(os.path.join(td, "app.js"), "w") as f:
        f.write(
            'import { something } from "./utils";\n\n'
            'function greet(name) {\n'
            '    console.log("Hello", name);\n'
            '}\n\n'
            'const arrowFunc = () => { greet("World"); };\n'
        )

    os.makedirs(os.path.join(td, "src"), exist_ok=True)
    with open(os.path.join(td, "src", "main.rs"), "w") as f:
        f.write(
            'use std::fs;\n\n'
            'fn main() {\n'
            '    println!("Hello");\n'
            '}\n\n'
            'struct User { name: String }\n\n'
            'impl User {\n'
            '    fn new(name: String) -> Self { User { name } }\n'
            '}\n'
        )

    with open(os.path.join(td, "package.json"), "w") as f:
        f.write(json.dumps({"dependencies": {"react": "^18.0.0"}}))

    return td, store


def _scan(td: str, store: MemoryStoreV2):
    indexer = CodeIndexer(td)
    indexer.store = store
    indexer.scan_all()
    return indexer


# ── Tests ──
def test_indexer_extracts_python_symbols():
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute("SELECT symbol_type, scope, body FROM code_index WHERE symbol_name='add'")
        row = cur.fetchone()
        assert row is not None, "should index `add` method"
        assert row[0] == "method", f"expected method, got {row[0]}"
        assert row[1] == "Calculator", f"expected scope Calculator, got {row[1]}"
        assert "return a + b" in (row[2] or ""), "body should contain return statement"


def test_indexer_extracts_javascript_symbols():
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute("SELECT symbol_type FROM code_index WHERE symbol_name='greet'")
        row = cur.fetchone()
        assert row is not None and row[0] == "function", "JS function `greet` missing"


def test_indexer_extracts_rust_symbols():
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute("""
            SELECT symbol_type FROM code_index
            WHERE symbol_name='main' AND language='rust'
        """)
        row = cur.fetchone()
        assert row is not None, "rust `main` function missing"


def test_indexer_creates_fts_index():
    """FTS5 virtual table should be queryable for symbol lookup."""
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name LIKE 'code_%fts%'
        """)
        names = [r[0] for r in cur.fetchall()]
        assert any(n.startswith("code_fts") or n.startswith("code_index_fts") for n in names), \
            f"code FTS5 virtual table should exist, got: {names}"


def test_indexer_incremental_with_hash():
    """Re-running scan with no changes should not crash and stay healthy."""
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM code_index")
        first = cur.fetchone()[0]
        assert first > 0, "first scan should index symbols"

        # Run again — should not crash, count should remain non-zero and similar
        _scan(td, store)
        cur.execute("SELECT COUNT(*) FROM code_index")
        second = cur.fetchone()[0]
        assert second > 0, "second scan should not wipe the index"
        # Allow small drift (e.g. transient overview rows); reject big drops
        assert abs(second - first) <= 2, \
            f"unexpected drift {first}→{second} (>2 symbols changed without file edits)"


def test_project_analyzer_detects_react():
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        analyzer = ProjectAnalyzer(td)
        overview = analyzer.analyze(store.conn)
        assert "React" in overview.get("frameworks", []), \
            f"React not detected, got: {overview.get('frameworks')}"


def test_project_analyzer_finds_entry_points():
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        analyzer = ProjectAnalyzer(td)
        overview = analyzer.analyze(store.conn)
        eps = overview.get("entry_points", [])
        assert "main.py" in eps, f"main.py should be entry point, got: {eps}"


def test_mcp_handler_get_symbol():
    """`code_get_symbol` returns formatted symbol body."""
    from core.api.mcp_server import _handle_code_get_symbol
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        text, summary = _handle_code_get_symbol(
            {"symbol_name": "add"}, lambda: store
        )
        assert "Calculator" in text or "method" in text.lower(), \
            f"should describe symbol metadata, got: {text[:150]}"
        assert "code_get_symbol" in summary


def test_indexer_does_not_delete_other_projects():
    """Re-indexing project A must not wipe project B's rows.
    Pre-Phase 5.12.4 the deletion pass nuked any code_index row whose
    rel_path wasn't visible from the current scan — which for two
    sibling projects meant scanning A wiped B and vice versa.
    """
    with tempfile.TemporaryDirectory() as t1:
        with tempfile.TemporaryDirectory() as t2:
            # Project A — Python
            with open(os.path.join(t1, "main.py"), "w") as f:
                f.write("def alpha():\n    return 1\n")
            with open(os.path.join(t1, "package.json"), "w") as f:
                f.write('{"dependencies": {}}')
            # Project B — also Python, different files
            with open(os.path.join(t2, "lib.py"), "w") as f:
                f.write("def beta():\n    return 2\n")
            with open(os.path.join(t2, "package.json"), "w") as f:
                f.write('{"dependencies": {}}')

            # Single shared store across both indexer runs.
            store_path = tempfile.mkdtemp()
            store = MemoryStoreV2(db_path=store_path)

            # Scan A
            ia = CodeIndexer(t1)
            ia.store = store
            ia.scan_all()

            # Scan B — must NOT remove A's rows.
            ib = CodeIndexer(t2)
            ib.store = store
            ib.scan_all()

            cur = store.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM code_index WHERE symbol_name='alpha'")
            assert cur.fetchone()[0] >= 1, \
                "scanning project B must not delete project A's `alpha` symbol"
            cur.execute("SELECT COUNT(*) FROM code_index WHERE symbol_name='beta'")
            assert cur.fetchone()[0] >= 1, \
                "project B's `beta` should be present after its scan"


def test_indexer_synthesizes_http_edges():
    """`fetch('/functions/v1/notify')` should register as a call to the
    notify Edge Function even though the link is a string literal, not
    an AST call. Phase 5.12.4."""
    with tempfile.TemporaryDirectory() as td:
        # Edge Function file (the callee)
        os.makedirs(os.path.join(td, "supabase/functions/notify"))
        with open(os.path.join(td, "supabase/functions/notify/index.ts"), "w") as f:
            f.write("""
import { serve } from 'std/http/server.ts';
serve(async (req) => new Response('ok'));
""")
        # Caller file: another Edge Function that fetches notify by URL
        os.makedirs(os.path.join(td, "supabase/functions/stripe-webhook"))
        with open(os.path.join(td, "supabase/functions/stripe-webhook/index.ts"), "w") as f:
            f.write("""
async function handleEvent() {
    await fetch('https://example.supabase.co/functions/v1/notify', {
        method: 'POST',
        body: JSON.stringify({ user_id: 'x' }),
    });
}
""")
        with open(os.path.join(td, "package.json"), "w") as f:
            f.write('{"dependencies": {}}')

        store = MemoryStoreV2(db_path=os.path.join(td, "store.db"))
        _scan(td, store)

        cur = store.conn.cursor()
        # The synthetic endpoint symbol must exist.
        cur.execute(
            "SELECT id, file_path FROM code_index "
            "WHERE symbol_type='endpoint' AND symbol_name='notify'"
        )
        endpoint = cur.fetchone()
        assert endpoint is not None, "endpoint symbol for notify must be created"
        endpoint_id = endpoint[0]
        # And there must be at least one inbound `call` link.
        cur.execute(
            "SELECT source_id FROM code_links "
            "WHERE target_id = ? AND link_type = 'call'",
            (endpoint_id,),
        )
        callers = cur.fetchall()
        assert len(callers) >= 1, \
            f"notify should have at least one HTTP caller, got {callers}"


def test_call_graph_disambiguates_by_receiver():
    """Two functions named `log` — one in Logger, one as a free function.
    A caller that does `Logger.log(...)` should resolve only to the
    class method, not both. Phase 5.12.3."""
    from core.code_indexer.call_graph import CallGraphBuilder
    builder = CallGraphBuilder()
    callers = [{
        "id": "caller-1",
        "type": "function",
        "meta": {"calls": ["Logger.log"]},
    }]
    global_defs = {
        "log": [
            {"id": "fixture-log", "scope": "", "file_path": "tests/fixture.ts"},
            {"id": "logger-log", "scope": "Logger", "file_path": "src/log.ts"},
        ],
    }
    links = builder.build_links(callers, global_defs)
    target_ids = [l["target_id"] for l in links]
    assert "logger-log" in target_ids, \
        f"Logger.log should resolve to logger-log, got {target_ids}"
    assert "fixture-log" not in target_ids, \
        "should NOT also link to the fixture's free `log` once receiver disambiguates"


def test_call_graph_falls_back_when_no_receiver_signal():
    """If the chain receiver matches nothing, keep the over-approximation
    rather than dropping the edge entirely — better to over-link than
    silently miss the only real edge."""
    from core.code_indexer.call_graph import CallGraphBuilder
    builder = CallGraphBuilder()
    callers = [{"id": "c1", "type": "function",
                "meta": {"calls": ["someUnknownReceiver.handle"]}}]
    global_defs = {
        "handle": [
            {"id": "h1", "scope": "Other", "file_path": "a.ts"},
            {"id": "h2", "scope": "Another", "file_path": "b.ts"},
        ],
    }
    links = builder.build_links(callers, global_defs)
    target_ids = sorted(l["target_id"] for l in links)
    assert target_ids == ["h1", "h2"], \
        f"with no receiver match, should link to all candidates; got {target_ids}"


def test_indexer_extracts_typescript_class_methods():
    """TypeScript classes use `class_body` for their bodies (not `block`
    or `declaration_list`). Pre-Phase 5.12.2 the indexer's class-method
    walk only checked block/declaration_list, so every TS class method
    was silently dropped — that's why the cline benchmark saw
    SqliteLockManager with zero methods in code_skeleton output."""
    ts_source = '''
export class SqliteLockManager {
    private db: any;

    constructor(path: string) {
        this.db = path;
    }

    async acquire(name: string): Promise<boolean> {
        return true;
    }

    async release(name: string): Promise<void> {
        return;
    }

    isLocked(name: string): boolean {
        return false;
    }
}
'''
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "lock.ts"), "w") as f:
            f.write(ts_source)
        with open(os.path.join(td, "package.json"), "w") as f:
            f.write('{"dependencies": {}}')
        store = MemoryStoreV2(db_path=os.path.join(td, "store.db"))
        _scan(td, store)
        cur = store.conn.cursor()
        cur.execute(
            "SELECT symbol_name, symbol_type, scope FROM code_index "
            "WHERE language='typescript' AND symbol_type='method' "
            "ORDER BY start_line"
        )
        rows = cur.fetchall()
        method_names = [r[0] for r in rows]
        # Constructor + 3 named methods.
        assert "constructor" in method_names or "acquire" in method_names, \
            f"expected TS class methods, got {method_names}"
        assert "acquire" in method_names, f"missing acquire: {method_names}"
        assert "release" in method_names, f"missing release: {method_names}"
        assert "isLocked" in method_names, f"missing isLocked: {method_names}"
        # Every method must be scoped to the class.
        for r in rows:
            assert r[2] == "SqliteLockManager", \
                f"method {r[0]} should be scoped to SqliteLockManager, got '{r[2]}'"


def test_mcp_handler_get_symbol_header_mode():
    """`mode='header'` skips the body block — saves tokens on
    'where is X' lookups. Phase 5.12.1."""
    from core.api.mcp_server import _handle_code_get_symbol
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        full, _ = _handle_code_get_symbol(
            {"symbol_name": "add", "mode": "full"}, lambda: store
        )
        header, summary = _handle_code_get_symbol(
            {"symbol_name": "add", "mode": "header"}, lambda: store
        )
        # Header version must not embed the **Body:** block
        assert "**Body:**" not in header, \
            "header mode should not include the body section"
        # But should still have **Signature:** + location
        assert "**Signature:**" in header or "**Location:**" in header, \
            "header mode should still include signature/location"
        # And should be strictly smaller than full
        assert len(header) < len(full), \
            f"header ({len(header)}) should be < full ({len(full)})"
        # Summary tag must reflect mode for benchmark logging
        assert "mode=header" in summary


def test_mcp_handler_callees():
    """`code_callees` should not crash even if no calls found."""
    from core.api.mcp_server import _handle_code_callees
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        text, summary = _handle_code_callees(
            {"symbol_name": "process"}, lambda: store
        )
        # Either lists callees or reports none — both are valid
        assert isinstance(text, str) and len(text) > 0
        assert "code_callees" in summary


def test_mcp_handler_uses():
    """`code_uses` finds identifier across symbols' bodies."""
    from core.api.mcp_server import _handle_code_uses
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        text, summary = _handle_code_uses(
            {"identifier": "Calculator"}, lambda: store
        )
        # `Calculator` appears in the `process` function body
        assert "Calculator" in text, "should find Calculator usage"
        assert "code_uses" in summary


def test_mcp_handler_hierarchy_root():
    """`code_hierarchy` with empty path returns top-level entries."""
    from core.api.mcp_server import _handle_code_hierarchy
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        text, summary = _handle_code_hierarchy({"path": ""}, lambda: store)
        assert "project root" in text.lower() or "📁" in text or "📄" in text, \
            f"hierarchy view missing, got: {text[:150]}"
        assert "code_hierarchy" in summary


def test_mcp_handler_explain_without_llm():
    """`code_explain` falls back to raw body when no Tier 2/Cloud LLM."""
    from core.api.mcp_server import _handle_code_explain
    try:
        from core.ai.tier_manager import get_tier_manager
        tm = get_tier_manager()
        tm.set_tier("tier0")  # ensure no LLM
    except ImportError:
        pass
    with tempfile.TemporaryDirectory() as td:
        _, store = _make_workspace(td)
        _scan(td, store)
        text, summary = _handle_code_explain(
            {"symbol_name": "add"}, lambda: store
        )
        # Should warn about tier + still show raw body
        assert "tier" in text.lower() or "calculator" in text.lower(), \
            f"explain output missing fallback, got: {text[:200]}"


def main():
    print("=" * 60)
    print("🧪 Cosmos v5 Phase 5 — Code Indexer Test Suite")
    print("=" * 60)

    tests = [
        ("Indexer extracts Python symbols",        test_indexer_extracts_python_symbols),
        ("Indexer extracts JavaScript symbols",    test_indexer_extracts_javascript_symbols),
        ("Indexer extracts Rust symbols",          test_indexer_extracts_rust_symbols),
        ("Indexer builds FTS5 search table",       test_indexer_creates_fts_index),
        ("Indexer is idempotent (hash-based)",     test_indexer_incremental_with_hash),
        ("ProjectAnalyzer detects React",          test_project_analyzer_detects_react),
        ("ProjectAnalyzer finds entry points",     test_project_analyzer_finds_entry_points),
        ("MCP code_get_symbol returns metadata",   test_mcp_handler_get_symbol),
        ("MCP code_get_symbol header mode",        test_mcp_handler_get_symbol_header_mode),
        ("Indexer extracts TS class methods",      test_indexer_extracts_typescript_class_methods),
        ("Call graph disambiguates by receiver",   test_call_graph_disambiguates_by_receiver),
        ("Call graph falls back without signal",   test_call_graph_falls_back_when_no_receiver_signal),
        ("Indexer synthesizes HTTP edges",         test_indexer_synthesizes_http_edges),
        ("Indexer scopes deletion per project",    test_indexer_does_not_delete_other_projects),
        ("MCP code_callees safe handler",          test_mcp_handler_callees),
        ("MCP code_uses finds identifier",         test_mcp_handler_uses),
        ("MCP code_hierarchy root view",           test_mcp_handler_hierarchy_root),
        ("MCP code_explain Tier 0 fallback",       test_mcp_handler_explain_without_llm),
    ]

    passed = sum(run_test(n, fn) for n, fn in tests)
    print("=" * 60)
    print(f"📊 Results: {passed}/{len(tests)} passed")
    print("=" * 60)
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
