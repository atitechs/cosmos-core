"""
Call Graph Builder
━━━━━━━━━━━━━━━━━━
Matches extracted function calls against known definitions
to build a directed graph of caller -> callee relationships.

Phase 5.12.3 update — calls are stored with their full receiver
chain when available (e.g. "Logger.log" instead of just "log").
The builder splits on `.` and `::` to extract the leaf name, then
prefers candidates whose declared scope or file path matches an
earlier segment of the chain. This collapses false positives from
several functions sharing the same leaf name (the cline benchmark
caught `Logger.log` resolving into a test fixture's `log`).
"""

from collections import defaultdict
from typing import Dict, List


def _split_chain(call: str) -> tuple[list[str], str]:
    """Return (receiver_segments, leaf_name) for a stored call entry.

    "controller.task.handleX" -> (["controller", "task"], "handleX")
    "Logger.log"              -> (["Logger"], "log")
    "Module::foo"             -> (["Module"], "foo")
    "plain"                   -> ([], "plain")
    """
    raw = call.replace("::", ".")
    parts = [p for p in raw.split(".") if p]
    if len(parts) <= 1:
        return [], call
    return parts[:-1], parts[-1]


def _score_match(receiver: list[str], target: dict) -> int:
    """Higher score = better match. 0 means no signal — fall back to
    over-approximation behaviour (link anyway)."""
    if not receiver:
        return 0
    score = 0
    target_scope = (target.get("scope") or "").lower()
    target_path = (target.get("file_path") or "").lower()
    target_id = target.get("id") or ""
    for seg in receiver:
        seg_l = seg.lower()
        # Exact scope hit (e.g. receiver "Logger" matches scope "Logger").
        if seg_l == target_scope:
            score += 5
        # Substring scope match (e.g. receiver "controller" matches
        # scope "ControllerClass").
        elif seg_l and seg_l in target_scope:
            score += 2
        # File path mention (e.g. receiver "telemetryService" matches
        # file "core/services/telemetry-service.ts").
        if seg_l and seg_l in target_path:
            score += 1
        # ID match catches transient cases where the symbol id encodes
        # the receiver (rare but cheap).
        if seg_l and seg_l in target_id.lower():
            score += 1
    return score


# A call that resolves to MORE than this many same-named definitions is too
# ambiguous to be a useful navigation edge — and linking to all of them is
# exactly what made the graph O(calls × definitions). On real-world common
# names (__init__, handle, get, run, process…) that fan-out exploded to tens
# of millions of `code_links` rows (5k synthetic files → 25M links → 6.7 GB
# DB → indexer hung). Above the cap we narrow to the caller's own file via an
# O(1) index, else drop the edge entirely.
MAX_FANOUT = 8


def _pick(receiver: list[str], candidates: List[Dict]) -> List[Dict]:
    """Choose link target(s) from a SMALL candidate list (≤ MAX_FANOUT).
    With a receiver, keep only the best-scoring tier; otherwise take all.
    Only ever called on small lists, so the scoring scan is bounded."""
    if not receiver or len(candidates) == 1:
        return candidates
    scored = [(t, _score_match(receiver, t)) for t in candidates]
    best = max(s for _, s in scored)
    if best > 0:
        return [t for t, s in scored if s == best]
    return candidates


class CallGraphBuilder:
    def __init__(self):
        pass

    def build_links(
        self,
        file_symbols: List[Dict],
        global_definitions: Dict[str, List[Dict]],
    ) -> List[Dict]:
        """Generate caller -> callee link rows from extracted symbols.

        global_definitions: {symbol_name: [target_dict, ...]} keyed by
        leaf name. Targets carry id / file_path / scope / type.

        Linear in (#calls + #definitions): the full target list for a leaf
        is NEVER scanned per-call. High-fan-out names (> MAX_FANOUT defs)
        are resolved only against the caller's own file through a one-time
        (name, file_path) index — both the scan and the score loops are
        otherwise bounded by MAX_FANOUT.
        """
        # One-time index for ambiguous leaf names only: (name, file) -> defs.
        by_name_file: Dict[tuple, List[Dict]] = defaultdict(list)
        for name, tlist in global_definitions.items():
            if len(tlist) > MAX_FANOUT:
                for t in tlist:
                    by_name_file[(name, t.get("file_path"))].append(t)

        links: list[dict] = []

        for caller in file_symbols:
            if caller.get("type") not in ("function", "method", "route"):
                continue

            calls_made = caller.get("meta", {}).get("calls", [])
            if not calls_made:
                continue

            caller_path = caller.get("file_path")

            for call_name in calls_made:
                receiver, leaf = _split_chain(call_name)
                targets = global_definitions.get(leaf)
                if not targets:
                    continue

                if len(targets) <= MAX_FANOUT:
                    # Small candidate set — safe to score in full.
                    chosen = _pick(receiver, targets)
                else:
                    # Ambiguous leaf. Do NOT scan all targets (quadratic);
                    # narrow to the caller's own file via the O(1) index.
                    same_file = by_name_file.get((leaf, caller_path)) if caller_path else None
                    if same_file and len(same_file) <= MAX_FANOUT:
                        chosen = _pick(receiver, same_file)
                    else:
                        continue  # too ambiguous — drop the edge

                for target in chosen:
                    links.append({
                        "source_id": caller["id"],
                        "target_id": target["id"],
                        "link_type": "call",
                    })

        return links
