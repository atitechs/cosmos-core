"""Canonical / legacy tool-name aliases for the Cosmos MCP server.

Lives in its own module so the permission engine (`mcp_permissions.py`)
and the agent registry (`core/agents/registry.py`) can consult the alias
table without importing `mcp_server.py` — which would create a circular
import cycle.

Every memory-domain tool is exposed under two names:

    cosmos_memory_<X>   (canonical)   ←→   brain_<X>   (legacy)

Every code-domain tool likewise:

    cosmos_code_<X>     (canonical)   ←→   code_<X>    (legacy)

Both names route to the same handler. Permission rules and agent
whitelists must respect this: an entry stored under either name applies
to calls made under either name. Without that, a user/admin who writes a
deny rule using the canonical name (or whitelists the canonical name)
would silently have no effect.
"""
from __future__ import annotations


# Canonical → legacy. Adding a new tool? Register the legacy name in
# `mcp_server.py:list_tools` and a single entry here; everything else
# (alias surfacing, perm/whitelist normalization, dispatch) follows.
CANONICAL_ALIASES: dict[str, str] = {
    # Memory domain
    "cosmos_memory_search":          "brain_search",
    "cosmos_memory_get":             "brain_get",
    "cosmos_memory_aggregate":       "brain_aggregate",
    "cosmos_memory_remember":        "brain_remember",
    "cosmos_memory_status":          "brain_status",
    "cosmos_memory_sitemap":         "brain_sitemap",
    "cosmos_memory_session_context": "brain_session_context",
    "cosmos_memory_pattern_recall":  "brain_pattern_recall",
    "cosmos_memory_rebuild_links":   "brain_rebuild_links",
    "cosmos_memory_create_folder":   "brain_create_folder",
    "cosmos_memory_delete_folder":   "brain_delete_folder",
    "cosmos_memory_move_memory":     "brain_move_memory",
    "cosmos_memory_create_agent":    "brain_create_agent",
    "cosmos_memory_link":            "brain_link",
    "cosmos_memory_update":          "brain_update_memory",
    "cosmos_memory_delete":          "brain_delete_memory",
    # Code domain
    "cosmos_code_search":                  "code_search",
    "cosmos_code_get_symbol":              "code_get_symbol",
    "cosmos_code_find_function":           "code_find_function",
    "cosmos_code_find_callers":            "code_find_callers",
    "cosmos_code_callees":                 "code_callees",
    "cosmos_code_uses":                    "code_uses",
    "cosmos_code_hierarchy":               "code_hierarchy",
    "cosmos_code_skeleton":                "code_skeleton",
    "cosmos_code_context_bundle":          "code_context_bundle",
    "cosmos_code_diff":                    "code_diff",
    "cosmos_code_trace_value":             "code_trace_value",
    "cosmos_code_analyze_refactor_impact": "code_analyze_refactor_impact",
    "cosmos_code_boundaries":              "code_boundaries",
    "cosmos_code_explain":                 "code_explain",
    "cosmos_code_explain_project":         "code_explain_project",
    "cosmos_code_reindex":                 "code_reindex",
    "cosmos_code_find_file":               "code_find_file",
    "cosmos_code_remember_error":          "code_remember_error",
    "cosmos_code_list_errors":             "code_list_errors",
    "cosmos_code_find_relevant_code":      "find_relevant_code",
}

# Legacy → set of canonical aliases pointing at it. Built once at import
# time. Used so a lookup keyed off the legacy name can fan out and also
# inspect the canonical form (e.g. permission rules stored under the
# canonical name must apply to calls made under the legacy name).
LEGACY_TO_CANONICAL: dict[str, frozenset[str]] = {}
for _canonical, _legacy in CANONICAL_ALIASES.items():
    LEGACY_TO_CANONICAL.setdefault(_legacy, set()).add(_canonical)  # type: ignore[arg-type]
LEGACY_TO_CANONICAL = {k: frozenset(v) for k, v in LEGACY_TO_CANONICAL.items()}


def resolve_canonical(name: str) -> str:
    """Map a `cosmos_*` canonical alias to its legacy handler name.
    Pass-through when `name` is already a legacy name or unmapped."""
    return CANONICAL_ALIASES.get(name, name)


def all_forms(name: str) -> frozenset[str]:
    """Return every name (legacy + every canonical alias) that refers to
    the same handler as `name`. Use this when checking permission rules
    or agent whitelists — an entry stored under any form must apply to
    calls made under any other form."""
    legacy = resolve_canonical(name)
    canonical = LEGACY_TO_CANONICAL.get(legacy, frozenset())
    return frozenset({name, legacy}) | canonical
