"""
Bootstrap Folders — idempotently create the default folder structure
defined in the brain manifest.

Run on first launch and any time the user wants to "reset to defaults"
without losing data (only missing folders are created).
"""
from __future__ import annotations
import sqlite3
from typing import Optional

from core.memory.store_v2 import get_store_v2
from core.memory.folder import FolderTree
from core.setup.brain_manifest import get_manifest


def bootstrap(conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Walk every folder path in the manifest and create it if missing.
    Nested paths (e.g. /Finance/Expenses) auto-create parents.

    Returns:
        {
          "created": [list of paths created],
          "existing": [paths already present],
          "total": int,
        }
    """
    if conn is None:
        store = get_store_v2()
        conn = store.conn

    tree = FolderTree(conn)
    manifest = get_manifest()

    created, existing = [], []

    for path, _rule in manifest.data.get("folders", {}).items():
        result = _ensure_folder_chain(tree, path)
        if result["created"]:
            created.extend(result["created"])
        if result["existing"]:
            existing.extend(result["existing"])

    # After folders exist, mirror permissions into MCP layer
    manifest.sync_to_mcp_permissions()

    # Seed agent-template memories (idempotent — only writes if missing).
    # External AI tools (OpenCode, Claude Code, Cursor, Cline) connect
    # MCP scoped to a single /Agents/<name> folder, so each agent reads
    # its own prompt/context/rules and writes notes that don't bleed
    # into other agents' memories.
    _seed_agent_starters(conn, tree)

    return {
        "created": created,
        "existing": existing,
        "total": len(created) + len(existing),
    }


# ── Agent template seeding ────────────────────────────────────────────
#
# v2 layout (2026-05-08) — inspired by Claude Skills + CrewAI:
#
#   /Agents/<name>/
#   ├── IDENTITY.md      (5-10 lines: role + tone)
#   ├── WORKFLOW.md      (step-by-step, not abstract)
#   ├── RULES.md         (hard CAN / CANNOT)
#   ├── HANDOFF.md       (input/output schema for multi-agent pipelines)
#   ├── tools.json       (MCP tool whitelist)
#   ├── knowledge/
#   │   ├── core/        (always-injected fundamentals)
#   │   ├── advanced/    (RAG-pulled when relevant)
#   │   └── refs/        (cheatsheets, formulas)
#   ├── examples/
#   │   ├── good/        (few-shot exemplars to mimic)
#   │   └── bad/         (anti-patterns + why they're wrong)
#   ├── playbooks/       (pre-baked SOPs for repeated situations)
#   ├── memory/
#   │   ├── learned.md   (auto-update with lessons)
#   │   └── session.json (current state)
#   └── evals/
#       ├── cases.jsonl  (test inputs + expected outputs)
#       └── results.jsonl (eval runs)
#
# Why this beats the legacy 3-file (prompt/context/rules) layout:
#   - IDENTITY stays tight → LLM doesn't lose priority in a long prompt
#   - WORKFLOW gives a deterministic path → fewer hallucinated steps
#   - examples/good/ is the strongest quality lever (3 good shots > 3
#     paragraphs of explanation)
#   - examples/bad/ converts past mistakes into curriculum
#   - knowledge/ split: small core vs RAG-pulled advanced — keeps
#     context budget manageable as the brain grows
#   - tools.json: per-agent MCP whitelist → narrower attack surface
#   - memory/learned.md: secret sauce — every fix appended → next run
#     inherits the lesson (mirrors code_remember_error behavior)
#   - evals/: regression-test prompts + workflow before you ship
#
# `_AGENT_TEMPLATE_FILES` lists root-level files (IDENTITY/WORKFLOW/
# RULES/HANDOFF/tools.json). `_AGENT_FOLDER_TREE` is the subfolder
# topology. `_AGENT_NESTED_FILES` seeds README/placeholder files
# inside each subfolder so the AI tools see explanatory text on
# first read instead of empty directories.

_AGENT_TEMPLATE_FILES = [
    (
        "setup",
        "_SETUP.md",
        """# _SETUP.md — first-read instructions for the connected AI

> **AI agent reading this**: this file is for YOU, not the user. Read
> it FIRST before anything else in this folder. After running the
> setup interview below, mark this file `## Status: COMPLETED` (set
> via `brain_remember` updating this memory) and stop reading it on
> subsequent connections.

## Status: AWAITING_SETUP

You're connecting to a freshly-created Cosmos agent folder. The
files in this folder (`IDENTITY.md`, `WORKFLOW.md`, `RULES.md`,
`HANDOFF.md`, `tools.json`) are template placeholders — the user
hasn't filled them in yet.

**Before doing the user's first real task**, run this 7-question
interview in their language (they wrote in Thai → reply Thai;
English → reply English; mixed → match the dominant one). Be
warm, brief, and skip questions that are obviously not relevant.

## Interview script

1. **What does this agent do?** (one sentence — e.g. "Write LinkedIn
   posts for B2B SaaS launches" / "Analyze daily forex trades for
   risk")
2. **If this agent were a person, what's their experience and
   working style?** (e.g. "5 years copywriting, terse not flowery"
   / "10 years prop trader, conservative, never YOLO")
3. **Inputs and outputs:** what does the user feed in, what should
   the agent produce? (be specific about format — bullets vs
   paragraphs vs JSON)
4. **Show 2-3 examples of the kind of output you want** (paste OK,
   or describe). These become `examples/good/`.
5. **Hard rules — what must this agent NEVER do?** (e.g. "never
   recommend leverage > 2x" / "never use emojis" / "always cite
   sources")
6. **Knowledge files to bring in** — paste any reference material,
   formulas, vocab. These become `knowledge/core/` (always-loaded)
   or `knowledge/refs/` (cheatsheets).
7. **Pipeline neighbors** — which agent (if any) feeds into this one
   or receives its output? (for `HANDOFF.md` — JSON I/O contract)

## After the interview

Use the answers to:

- **Rewrite `IDENTITY.md`** — 5-10 lines distilled from Q1+Q2.
  Keep it tight; long identity dilutes priority.
- **Rewrite `WORKFLOW.md`** — turn the agent's process into 5-7
  numbered concrete steps. NOT abstract — atomic actions an LLM
  can follow deterministically. Reference `tools.json` allowed
  tools by name where applicable.
- **Rewrite `RULES.md`** — extract Q5's hard rules. Critically:
  scan for contradictions. If two rules can conflict, surface
  that to the user before saving.
- **Rewrite `HANDOFF.md`** — turn Q3 + Q7 into a JSON schema for
  inputs and outputs. Make it specific (field names, types).
- **Update `tools.json`** — pick the MCP tools this agent
  realistically needs from the available list. Default-deny the
  rest.
- **Save Q4 examples** as `examples/good/01-<short-slug>.md`
  through `examples/good/0N-<slug>.md` — one per example.
- **Save Q6 knowledge** to `knowledge/core/<topic>.md` for small
  fundamentals or `knowledge/refs/<topic>.md` for cheatsheets.

## When you're done

1. Use `brain_remember` (or your equivalent edit-memory tool) to
   update this `_SETUP.md` content — change `## Status:
   AWAITING_SETUP` to `## Status: COMPLETED` and append a one-line
   timestamp note.
2. Briefly summarize to the user what you set up and ask if
   they want to refine anything before starting the real task.
3. On future connections to this folder, see Status: COMPLETED at
   the top → skip the interview, proceed to the user's actual
   request.

## If user wants to skip setup

If the user says "use the defaults / I'll edit it myself / skip
setup", that's fine. Mark this file as `## Status: SKIPPED` and
proceed with their task using whatever's in the existing template
files. They can always re-trigger setup by editing this file back
to `## Status: AWAITING_SETUP` later.

---

_Generated by Cosmos `+Agent` — `bootstrap_folders.py` template v3 (2026-05-08)_
""",
    ),
    (
        "identity",
        "IDENTITY.md",
        """# IDENTITY

> **AI reading this**: if you haven't yet, open `_SETUP.md` in this
> folder first. If its Status is `AWAITING_SETUP`, run the interview
> there before doing the user's task — the rest of this file is just
> a placeholder until then.

Replace these 5-10 lines with the agent's role + tone. Keep it tight
— this gets injected into every prompt as system context. Long
identity = LLM loses priority on what matters.

**Role:** _e.g. "Senior content writer for SaaS launch posts"_
**Voice:** _e.g. "terse, declarative, no marketing fluff"_
**Default language:** _e.g. "Thai for body, English for code"_
**Audience:** _e.g. "indie devs in Southeast Asia"_

Background knowledge → `knowledge/core/`. Hard rules → `RULES.md`.
This file is only "who am I" — nothing else.
""",
    ),
    (
        "workflow",
        "WORKFLOW.md",
        """# WORKFLOW

Step-by-step procedure the agent follows on every task. Be concrete
— "1. Do X. 2. Then do Y. 3. If A, do B." LLMs follow numbered
deterministic flows much better than abstract instructions.

Example for a research agent:

1. Read the user's prompt + extract 3-5 search keywords.
2. Call `brain_search` for each keyword. Save hits to scratchpad.
3. If fewer than 3 unique hits across keywords, call
   `find_relevant_code` and merge.
4. For each top hit, summarise into one bullet (≤ 15 words).
5. Output: `{"summary": [bullets...], "citations": [memory_ids...]}`
6. Save the summary into `memory/learned.md` for next time.

Replace this with your agent's workflow. The flow becomes the
contract — the LLM stops improvising routing.
""",
    ),
    (
        "rules",
        "RULES.md",
        """# RULES

Hard constraints. Rules here must NOT contradict each other — if two
rules conflict, the LLM picks one and silently drops the other.

CAN:
- Read everything inside this folder (`/Agents/<your-name>/**`)
- Write new memories under `memory/` (learned, session)
- Append outputs to `examples/good/` when user confirms quality
- Search the rest of the brain via `brain_search` — only when the
  user provides the search keyword

CANNOT:
- Write outside this agent's folder
- Read `/Private` or `/Archive` — operator-only zones
- Take actions outside the brain (shell commands, third-party API
  calls) unless the user explicitly wires that up
- Modify `RULES.md`, `IDENTITY.md`, or `tools.json` — those are
  configuration, not output
""",
    ),
    (
        "handoff",
        "HANDOFF.md",
        """# HANDOFF — input/output contract

Schema this agent expects to receive and produces. Defines the
boundary so multi-agent pipelines don't drop information.

**Input** (what other agents / the user send to me):
```json
{
  "task": "string — what to do",
  "context": "string — relevant background (optional)",
  "constraints": ["array of strings — hard limits (optional)"]
}
```

**Output** (what I produce for the next agent / the user):
```json
{
  "result": "string — primary deliverable",
  "confidence": "number 0-1 — self-rated certainty",
  "citations": ["array of memory IDs used"],
  "follow_up": ["array of strings — questions worth asking"]
}
```

Adapt the schemas to your agent's role. The point is to make the
contract explicit so a downstream agent knows exactly what it's
getting.
""",
    ),
    (
        "tools",
        "tools.json",
        """{
  "version": 1,
  "agent": "<your-name>",
  "_comment": "MCP tool whitelist for this agent. The MCP server narrows tool surface per-folder so this agent only sees these. Add tool names from CLAUDE.md routing table.",
  "allowed": [
    "brain_search",
    "brain_get",
    "brain_session_context",
    "find_relevant_code"
  ],
  "denied": [
    "brain_remember",
    "code_remember_error"
  ]
}
""",
    ),
]

# Subfolder topology — created under every /Agents/<name>/ during
# bootstrap (idempotent). README files inside each are seeded by
# _AGENT_NESTED_FILES below.
_AGENT_FOLDER_TREE = [
    "knowledge",
    "knowledge/core",
    "knowledge/advanced",
    "knowledge/refs",
    "examples",
    "examples/good",
    "examples/bad",
    "playbooks",
    "memory",
    "evals",
]

# Nested files — (relative_subpath, filename, body). Seeded once when
# the parent subfolder is missing or empty; never overwritten.
_AGENT_NESTED_FILES = [
    (
        "knowledge/core",
        "README.md",
        """# knowledge/core

Fundamentals the agent reads on EVERY turn. Keep it small (~1-2 KB
total across all files in this folder) — anything large goes in
`advanced/` and gets RAG-pulled only when relevant.

Examples of what belongs here:
- 1 paragraph: "what does this domain mean"
- A formula or 2 the agent uses constantly
- Vocabulary mappings ("when user says X they mean Y")
""",
    ),
    (
        "knowledge/advanced",
        "README.md",
        """# knowledge/advanced

Larger reference material — long-form writeups, technical depth,
rare-but-needed details. The MCP server pulls these into context
ONLY when the user's query embeddings match (RAG selection).

Drop full chapters / 50-page guides / playbook detail here. Don't
worry about size — it's gated behind retrieval.
""",
    ),
    (
        "knowledge/refs",
        "README.md",
        """# knowledge/refs

Cheatsheets and quick-reference: formulas, lookup tables, command
syntax, API endpoint lists. The agent treats these as fast-access
during workflow steps.

Example files: `mcp-tool-cheatsheet.md`, `regex-patterns.md`,
`fibonacci-levels.md`.
""",
    ),
    (
        "examples/good",
        "README.md",
        """# examples/good

Few-shot exemplars the agent mimics. **3 good examples here are
worth more than 3 paragraphs of instruction.**

Add files like `01-trade-analysis.md` containing a complete
example of an output the user loved. Each example should follow
the format: input description, then the actual good output.

Auto-update tip: when the user says "save this as a good example",
append the conversation to a new file here.
""",
    ),
    (
        "examples/bad",
        "README.md",
        """# examples/bad

Anti-patterns. Each file pairs a bad output with WHY it's bad. The
agent reads these to know what NOT to produce.

Format:
```
## Output (bad)
<the bad text>

## Why bad
- Reason 1: ...
- Reason 2: ...
```

This is more efficient than writing rules — the LLM internalizes
the failure mode from the example.
""",
    ),
    (
        "playbooks",
        "README.md",
        """# playbooks

One markdown file per repeated situation. Pre-baked SOPs the agent
matches against the current task and applies wholesale.

Example structure for a trading agent:
- `breakout-long.md` — entry criteria, stop placement, target rule
- `earnings-fade.md` — same structure for a different setup
- `range-fade.md`

The workflow becomes "1. Detect situation. 2. Pick playbook. 3.
Apply." — much less prompt budget than re-deriving the SOP each
time.
""",
    ),
    (
        "memory",
        "learned.md",
        """# learned.md — auto-updating lesson library

This file accumulates lessons the agent learned from mistakes or
user corrections. The MCP layer appends here whenever:
- The user says "you got X wrong, fix it"
- A `code_remember_error` fires from this agent's workflow
- The eval suite catches a regression

Format: dated entries, terse.

```
## 2026-05-08
- Stop placing position size > 2% equity. Caught by user.
- "RSI 70 is overbought" — wrong; current rule uses 80 for crypto.
```

Inject the last N entries into context every turn so the agent
inherits its own past lessons.
""",
    ),
    (
        "memory",
        "session.json",
        """{
  "_comment": "Current session state. The agent reads/writes this between turns. Keep small.",
  "current_task": null,
  "turn_count": 0,
  "scratchpad": {},
  "last_updated": null
}
""",
    ),
    (
        "evals",
        "cases.jsonl",
        """{"id": "case-001", "input": "Replace this with a real test prompt", "expected_keywords": ["expected", "output", "tokens"], "must_call_tools": ["brain_search"], "comment": "1 test case per line. Run before deploying any prompt change."}
""",
    ),
    (
        "evals",
        "results.jsonl",
        """""",
    ),
]


def _seed_agent_starters(conn, tree: FolderTree) -> None:
    """For every /Agents/<name> folder, ensure the v2 layout exists:
      1. Root files: IDENTITY.md, WORKFLOW.md, RULES.md, HANDOFF.md, tools.json
      2. Subfolder tree: knowledge/{core,advanced,refs}, examples/{good,bad},
         playbooks, memory, evals
      3. Nested README/seed files inside each subfolder

    All checks are idempotent — if a file or folder already exists,
    skip it. Never overwrites user edits.

    The same logic runs from `POST /api/v2/agents` (the +Agent button)
    so manual creation and bootstrap converge to identical structure."""
    from core.memory.store_v2 import MemoryStoreV2

    cur = conn.cursor()
    cur.execute(
        "SELECT id, path FROM folders WHERE path LIKE '/Agents/%' "
        "AND path NOT LIKE '/Agents/%/%'"  # only direct children of /Agents
    )
    agent_folders = cur.fetchall()
    if not agent_folders:
        return

    # 1. Ensure full subfolder topology under each agent — idempotent.
    for folder_id, folder_path in agent_folders:
        for subpath in _AGENT_FOLDER_TREE:
            full = f"{folder_path}/{subpath}"
            if _find_by_path(tree, full):
                continue
            try:
                # Resolve parent — innermost segment under this agent
                parts = subpath.split("/")
                parent_id = folder_id
                accumulated = folder_path
                for part in parts:
                    accumulated = f"{accumulated}/{part}"
                    existing = _find_by_path(tree, accumulated)
                    if existing:
                        parent_id = existing["id"]
                    else:
                        new = tree.create(part, parent_id=parent_id)
                        parent_id = new["id"] if isinstance(new, dict) else new
            except Exception as e:
                print(f"⚠️ failed creating {full}: {e}")

    # We need a MemoryStoreV2 to use store() + correct FTS sync.
    # Build a thin wrapper around the existing connection.
    class _ConnStore:
        def __init__(self, conn):
            self.conn = conn
            import threading
            self.lock = threading.RLock()
        # delegate the methods MemoryStoreV2.store uses
        store = MemoryStoreV2.store
        _get_folder_path = MemoryStoreV2._get_folder_path
        _sync_fts_insert = MemoryStoreV2._sync_fts_insert

    store = _ConnStore(conn)

    for folder_id, folder_path in agent_folders:
        agent_name = folder_path.rsplit("/", 1)[-1]

        # 2. Root files (IDENTITY.md, WORKFLOW.md, RULES.md, HANDOFF.md, tools.json)
        for slug, title, body in _AGENT_TEMPLATE_FILES:
            # Dedupe by typed_data.slug — robust against title rewrites
            # by the user. Earlier `LIKE '# Title%'` would re-seed if user
            # changed the heading.
            cur.execute(
                "SELECT id FROM memories_v2 "
                "WHERE folder_id = ? AND json_extract(typed_data, '$.slug') = ? "
                "LIMIT 1",
                (folder_id, slug),
            )
            if cur.fetchone():
                continue
            try:
                # Inject agent name into tools.json placeholder so each
                # agent's whitelist file references its own folder. All
                # other files use generic templates.
                final_body = body.replace("<your-name>", agent_name)
                store.store(
                    content=final_body,
                    category="note",
                    folder_id=folder_id,
                    source="agent-template",
                    typed_data={"title": title, "agent": agent_name, "slug": slug},
                    tags=["agent-template", agent_name.lower()],
                )
            except Exception as e:
                print(f"⚠️ failed seeding {folder_path}/{slug}: {e}")

        # 3. Nested README/seed files inside subfolders
        for subpath, filename, body in _AGENT_NESTED_FILES:
            full_sub = f"{folder_path}/{subpath}"
            sub_folder = _find_by_path(tree, full_sub)
            if not sub_folder:
                continue  # subfolder failed to create — skip its seed
            sub_id = sub_folder["id"]
            sub_slug = f"{subpath}/{filename}".replace("/", "-")
            cur.execute(
                "SELECT id FROM memories_v2 "
                "WHERE folder_id = ? AND json_extract(typed_data, '$.slug') = ? "
                "LIMIT 1",
                (sub_id, sub_slug),
            )
            if cur.fetchone():
                continue
            try:
                store.store(
                    content=body,
                    category="note",
                    folder_id=sub_id,
                    source="agent-template",
                    typed_data={
                        "title": filename, "agent": agent_name, "slug": sub_slug,
                    },
                    tags=["agent-template", agent_name.lower()],
                )
            except Exception as e:
                print(f"⚠️ failed seeding {full_sub}/{filename}: {e}")


def _ensure_folder_chain(tree: FolderTree, full_path: str) -> dict:
    """
    Walk from / down to full_path, creating each segment if missing.
    Returns lists of newly-created vs already-existing segments.
    """
    parts = [p for p in full_path.strip("/").split("/") if p]
    created, existing = [], []
    parent_id = None
    accumulated = ""

    for part in parts:
        accumulated = f"{accumulated}/{part}"
        existing_folder = _find_by_path(tree, accumulated)
        if existing_folder:
            existing.append(accumulated)
            parent_id = existing_folder["id"]
        else:
            new_folder = tree.create(part, parent_id=parent_id)
            created.append(accumulated)
            parent_id = new_folder["id"] if isinstance(new_folder, dict) else new_folder

    return {"created": created, "existing": existing}


def _find_by_path(tree: FolderTree, path: str) -> Optional[dict]:
    # FolderTree exposes get_by_path in the v5 API
    if hasattr(tree, "get_by_path"):
        return tree.get_by_path(path)
    # Defensive fallback if older folder.py: query directly
    row = tree.conn.execute(
        "SELECT id, parent_id, name, path FROM folders WHERE path = ?", (path,)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "parent_id": row[1], "name": row[2], "path": row[3]}


if __name__ == "__main__":
    result = bootstrap()
    print(f"✅ Created {len(result['created'])} new folders, "
          f"{len(result['existing'])} already existed")
    for p in result["created"]:
        print(f"   + {p}")
