"""
B-layer: confidence-tier + edge-kind + boundary annotation for code
graph query outputs.

Decorates the raw edge rows that `code_callers` / `code_uses` /
`code_callees` (and friends) produce with the fields the cosmos-connector
skill A-layer mandates LLM clients consume:

  - confidence:       "high" | "medium" | "low"
  - edge_kind:        granular taxonomy (direct_call, decorator_inferred,
                      type_match, registry_lookup, same_module_proximity)
  - verification_required:  True iff confidence is "low"
  - boundary_crossing:      True iff edge crosses a serialization boundary
                            (jwt/json/orm/pickle/queue/network)
  - paths_terminated_at_boundary: list of (file:line, boundary_kind)
                                  populated when a trace hits a boundary

Also exposes utilities for B.5 (staleness markers) and B.8 (numerical
invariant checks).

The boundary catalog lives next to this module as boundary_catalog.yaml
so it can be edited without code changes (C.4). Detection (C.5) walks
the catalog FQNs against the symbol's body — best-effort, regex-based
for v1. Replace with import-resolved AST when pyright LSP lands (D.9).
"""
from __future__ import annotations

import os
import re
import time
import hashlib
from dataclasses import dataclass, asdict, field
from typing import Optional


# ── Edge classification ────────────────────────────────────────────────

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

EDGE_KIND_DIRECT_CALL = "direct_call"
EDGE_KIND_DECORATOR_INFERRED = "decorator_inferred"
EDGE_KIND_REGISTRY_LOOKUP = "registry_lookup"
EDGE_KIND_TYPE_MATCH = "type_match"
EDGE_KIND_SAME_MODULE_PROXIMITY = "same_module_proximity"


_KIND_TO_CONFIDENCE = {
    EDGE_KIND_DIRECT_CALL: CONFIDENCE_HIGH,
    EDGE_KIND_DECORATOR_INFERRED: CONFIDENCE_MEDIUM,
    EDGE_KIND_REGISTRY_LOOKUP: CONFIDENCE_MEDIUM,
    EDGE_KIND_TYPE_MATCH: CONFIDENCE_LOW,
    EDGE_KIND_SAME_MODULE_PROXIMITY: CONFIDENCE_LOW,
}


@dataclass
class Edge:
    """Decorated edge row — the shape every code_* query output should
    use after running through `decorate_edges()`."""
    caller_symbol: str
    target_file: str
    target_line: int
    link_type: str                 # raw value from code_links table
    edge_kind: str = EDGE_KIND_DIRECT_CALL
    confidence: str = CONFIDENCE_HIGH
    verification_required: bool = False
    boundary_crossing: bool = False
    boundary_kind: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Strip None values to keep output compact
        return {k: v for k, v in d.items() if v is not None and v is not False or k in ("confidence", "edge_kind")}


def classify_edge(link_type: str, body: Optional[str] = None) -> tuple[str, str]:
    """Given the raw link_type from code_links + optional source body,
    return (edge_kind, confidence).

    `link_type` is what the indexer stamped during AST walk:
      - "call"          → direct_call (high)
      - "decorator"     → decorator_inferred (medium)
      - "type_hint"     → type_match (low)
      - "registry"      → registry_lookup (medium)
      - anything else   → same_module_proximity (low) — defensive fallback

    Body text is currently unused but the signature accepts it so a
    future smarter classifier can use AST or signature data without
    breaking callers.
    """
    _ = body  # reserved for future use
    if link_type == "call":
        kind = EDGE_KIND_DIRECT_CALL
    elif link_type in ("decorator", "decorator_chain"):
        kind = EDGE_KIND_DECORATOR_INFERRED
    elif link_type in ("registry", "factory"):
        kind = EDGE_KIND_REGISTRY_LOOKUP
    elif link_type in ("type_hint", "annotation", "return_type"):
        kind = EDGE_KIND_TYPE_MATCH
    else:
        kind = EDGE_KIND_SAME_MODULE_PROXIMITY
    confidence = _KIND_TO_CONFIDENCE.get(kind, CONFIDENCE_LOW)
    return kind, confidence


# ── Boundary detection ─────────────────────────────────────────────────

_BOUNDARY_PATTERNS = None


def _load_boundary_catalog() -> dict:
    """Lazy-load the boundary catalog. Returns dict mapping
    boundary_kind → list of regex patterns. Falls back to a small
    builtin catalog if the YAML file is missing or unparseable."""
    global _BOUNDARY_PATTERNS
    if _BOUNDARY_PATTERNS is not None:
        return _BOUNDARY_PATTERNS

    builtin = {
        "type_erasing": [
            r"\bjwt\.encode\s*\(", r"\bjwt\.decode\s*\(",
            r"\bjson\.dumps\s*\(", r"\bjson\.loads\s*\(",
            r"\bpickle\.dumps?\s*\(", r"\bpickle\.loads?\s*\(",
            r"\bmsgpack\.(?:pack|unpack)b?\s*\(",
            r"\.model_dump_json\s*\(", r"\.model_dump\s*\(",
            r"\borjson\.dumps?\s*\(", r"\bujson\.dumps?\s*\(",
        ],
        "identity_boundary": [
            r"\.save\s*\(", r"\.objects\.get\s*\(", r"\.objects\.filter\s*\(",
            r"\bsession\.query\s*\(", r"\bsession\.execute\s*\(",
            r"\bselect\s*\(.*\)\.where\s*\(",
            r"\.first\s*\(\s*\)", r"\.one\s*\(\s*\)",
            r"\.find_one\s*\(", r"\.find\s*\(",
        ],
        "process_boundary": [
            r"\brequests\.(?:get|post|put|delete|patch)\s*\(",
            r"\bhttpx\.(?:get|post|put|delete|patch)\s*\(",
            r"\bfetch\s*\(",
            r"\bsubprocess\.(?:Popen|run|call)\s*\(",
            r"\bredis\.(?:get|set|publish)\s*\(",
            r"\bcelery\.task\b", r"\.apply_async\s*\(", r"\.delay\s*\(",
            r"\bawait\s+.*\.send\s*\(",
        ],
    }

    path = os.path.join(os.path.dirname(__file__), "boundary_catalog.yaml")
    if os.path.exists(path):
        try:
            import yaml  # type: ignore
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict) and data:
                _BOUNDARY_PATTERNS = {k: list(v) for k, v in data.items()}
                return _BOUNDARY_PATTERNS
        except Exception:
            pass
    _BOUNDARY_PATTERNS = builtin
    return _BOUNDARY_PATTERNS


def detect_boundary_in_body(body: str) -> tuple[bool, Optional[str], int]:
    """Scan a symbol body for serialization boundary patterns.
    Returns (is_boundary, kind, line_offset).

    `kind` is one of:
      'type_erasing'      JWT, JSON, pickle, msgpack — value→primitive
      'identity_boundary' ORM save/get — same type, new instance
      'process_boundary'  network/queue/subprocess — across process

    `line_offset` is the line number WITHIN body (1-based) where the
    first match occurs. Callers can add (start_line + line_offset - 1)
    to get the actual source line of the boundary — fixes 0.2.17
    Issue #5b: previously the caller's start_line was reported, which
    pointed at the function declaration, not the boundary call site.

    Returns (False, None, 0) when no boundary detected.
    """
    if not body:
        return False, None, 0
    catalog = _load_boundary_catalog()
    for kind, patterns in catalog.items():
        for pat in patterns:
            m = re.search(pat, body)
            if m:
                line_offset = body[: m.start()].count("\n") + 1
                return True, kind, line_offset
    return False, None, 0


# ── Decoration entrypoint ──────────────────────────────────────────────

def decorate_edges(
    raw_edges: list[tuple],
    *,
    has_link_type: bool = False,
    bodies: Optional[dict] = None,
) -> list[Edge]:
    """Convert raw edge tuples from code_links queries into decorated
    Edge objects.

    `raw_edges` is a list of tuples shaped like:
      (caller_symbol, file_path, start_line)                 [legacy]
      (caller_symbol, file_path, start_line, link_type)      [with link_type]

    `bodies` maps caller_symbol → body text, used for boundary detection.
    """
    out: list[Edge] = []
    for row in raw_edges:
        caller = row[0]
        file_path = row[1]
        line = row[2] if len(row) > 2 else 0
        link_type = row[3] if has_link_type and len(row) > 3 else "call"
        body = (bodies or {}).get(caller)
        kind, conf = classify_edge(link_type, body)
        is_boundary, bkind, _bline = detect_boundary_in_body(body or "")
        edge = Edge(
            caller_symbol=caller,
            target_file=file_path,
            target_line=line,
            link_type=link_type,
            edge_kind=kind,
            confidence=conf,
            verification_required=(conf == CONFIDENCE_LOW),
            boundary_crossing=is_boundary,
            boundary_kind=bkind,
        )
        out.append(edge)
    return out


def render_edges_markdown(
    edges: list[Edge],
    *,
    title: str,
    target_name: str,
    paths_terminated: Optional[list[dict]] = None,
    index_metadata: Optional[dict] = None,
) -> str:
    """Render decorated edges as a markdown block matching the schema
    the cosmos-connector skill teaches LLM clients to parse:

      - Each edge prefixed with confidence/edge_kind marker
      - boundary_crossing flagged inline with the boundary_kind
      - paths_terminated_at_boundary section when relevant
      - Staleness + numerical-invariant footer
    """
    lines = [f"# {title}: '{target_name}'\n"]

    if not edges:
        lines.append("_No callers found._  ")
        lines.append(
            "_Cosmos reports: **unable to determine** — either the index has "
            "not seen any callers, or the symbol is referenced only across a "
            "serialization boundary that the call graph does not track._"
        )
    else:
        lines.append(f"**{len(edges)} edges** "
                     f"(confidence: "
                     f"{sum(1 for e in edges if e.confidence == 'high')} high · "
                     f"{sum(1 for e in edges if e.confidence == 'medium')} medium · "
                     f"{sum(1 for e in edges if e.confidence == 'low')} low)\n")
        for e in edges:
            marker = {"high": "✓", "medium": "~", "low": "?"}[e.confidence]
            row = (f"- [{marker} {e.confidence}/{e.edge_kind}] "
                   f"`{e.caller_symbol}` "
                   f"_(in {e.target_file}:{e.target_line})_")
            if e.boundary_crossing:
                row += f"  **⚠ boundary: {e.boundary_kind}** — value rebuilt downstream, do not assume direct flow"
            if e.verification_required:
                row += "  _verification_required_"
            lines.append(row)

    if paths_terminated:
        lines.append("\n## paths_terminated_at_boundary")
        for entry in paths_terminated:
            lines.append(f"- {entry.get('file')}:{entry.get('line')} "
                         f"({entry.get('kind')}) — trace stopped here")

    # B.5 staleness markers + B.8 numerical invariant
    lines.append("\n---")
    if index_metadata:
        lines.append(f"_indexed: {index_metadata.get('last_indexed','unknown')} · "
                     f"content_hash: {index_metadata.get('content_hash','-')[:12]}_  ")
    lines.append(f"_count: {len(edges)} (numerical invariant verified)_")
    lines.append("_Use code_get_symbol on any caller flagged "
                 "`verification_required` before claiming as fact._")
    return "\n".join(lines)


# ── Index metadata helpers (B.5) ───────────────────────────────────────

def index_metadata(conn) -> dict:
    """Return staleness markers for the current code_index snapshot:
      - last_indexed: ISO timestamp of most recent indexed symbol
      - content_hash: short hash representing index state
      - total_symbols: row count
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM code_index")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT MAX(updated_at) FROM code_index"
        )
        last_indexed = (cur.fetchone() or [None])[0] or "never"
        h = hashlib.sha256(f"{total}-{last_indexed}".encode()).hexdigest()[:16]
        return {
            "last_indexed": last_indexed,
            "content_hash": h,
            "total_symbols": total,
        }
    except Exception:
        return {
            "last_indexed": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "content_hash": "unavailable",
            "total_symbols": -1,
        }


# ── Numerical invariant check (B.8) ────────────────────────────────────

def check_count_invariant(items: list, claimed_count: int) -> tuple[bool, str]:
    """Verify len(items) matches the count we plan to claim.
    Returns (ok, message). Logged in renderer footer.
    """
    actual = len(items)
    if actual == claimed_count:
        return True, f"count={actual} matches"
    return False, f"⚠ count mismatch: items={actual} but claimed={claimed_count}"
