"""Runtime configuration — env-var overrides for paths + side-effects.

Used by the benchmark harness so a Phase 3 sandbox can point Cosmos at
a cloned DB + isolated activity log + suppressed file-mirror writes,
without touching the user's real brain. Default values match the
production install so a normal launch reads no env vars and behaves
exactly as before.

Why a single module instead of inline `os.environ.get` calls scattered
across store_v2 / mcp_activity / errors:
- One place to audit + document the contract
- One place for the benchmark to import + assert against
- New flags don't require touching every caller

Convention: every override key starts with `COSMOS_`. Returning a
resolved path / boolean rather than raw string lets tests mock by
patching this module instead of stomping process-wide env state.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


# ── Path defaults ────────────────────────────────────────────────────────
# These resolve relative to the current working directory at import time
# (where rest_server_v2 / mcp_server are launched from). They produce the
# same paths the singletons used to hardcode, so no behavior change for
# unconfigured installs.

DEFAULT_DATA_DIR = Path("data/brain_v2")

# Folder name under ~/Library/Application Support (macOS) and
# %APPDATA% (Windows). Pre-rebrand the folder was "AI-Brain"; the
# brand is "Cosmos" everywhere user-facing now, so the folder was
# renamed to match. `default_app_support_dir()` migrates an existing
# AI-Brain folder to Cosmos on first run with the new code so users
# don't have to do anything manual.
APP_SUPPORT_FOLDER = "Cosmos"
APP_SUPPORT_FOLDER_LEGACY = "AI-Brain"


def default_app_support_dir() -> str:
    """Platform-appropriate default user data dir for the bundled app
    (macOS: ~/Library/Application Support/Cosmos; Windows: %APPDATA%
    \\Cosmos; Linux: ~/.cosmos).

    Decision matrix on every call (idempotent, safe):

      Cosmos exists  | AI-Brain exists  | action
      ---------------+------------------+-----------------------------
      yes (real)     | no               | use Cosmos                 (stable case)
      yes (real)     | yes (real)       | use Cosmos + WARN — user
                                          had data in both, we don't
                                          touch AI-Brain so nothing
                                          gets silently merged
      yes (real)     | yes (symlink)    | use Cosmos                 (post-rename
                                          bridge; symlink is benign)
      no             | yes (real)       | rename AI-Brain → Cosmos   (the migration)
      no             | yes (symlink)    | unlink dangling symlink,
                                          create fresh Cosmos        (cleanup)
      no             | no               | create fresh Cosmos        (new install)

    Returns the absolute path string. Sub-callers always get the
    Cosmos path unless rename failed (then legacy fallback so the
    user keeps their data).
    """
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        new_path = os.path.join(home, "Library", "Application Support", APP_SUPPORT_FOLDER)
        legacy_path = os.path.join(home, "Library", "Application Support", APP_SUPPORT_FOLDER_LEGACY)
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        new_path = os.path.join(appdata, APP_SUPPORT_FOLDER)
        legacy_path = os.path.join(appdata, APP_SUPPORT_FOLDER_LEGACY)
    else:
        # Linux / BSD — lowercase + dot-prefixed convention. .aibrain
        # was the pre-rebrand name; .cosmos is the post-rebrand one.
        new_path = os.path.join(home, ".cosmos")
        legacy_path = os.path.join(home, ".aibrain")

    cosmos_exists = os.path.exists(new_path)
    legacy_exists = os.path.exists(legacy_path)
    legacy_is_symlink = os.path.islink(legacy_path) if legacy_exists else False

    if cosmos_exists and legacy_exists and not legacy_is_symlink:
        # Both real folders. Don't silently merge — that's how data
        # gets clobbered. Warn loudly + use Cosmos. Operator can
        # review AI-Brain manually and either restore it or delete.
        print(
            f"[cosmos] WARNING: Both data folders exist as real "
            f"directories:\n"
            f"  active:  {new_path}\n"
            f"  legacy:  {legacy_path}\n"
            f"Cosmos will use {new_path}. Inspect the legacy folder "
            f"and merge or delete manually if needed.",
            file=sys.stderr,
        )
        return new_path

    if cosmos_exists:
        # Stable / post-migration / benign-symlink case. No action.
        return new_path

    if legacy_exists and not legacy_is_symlink:
        # The actual one-shot migration. Rename atomically; on failure
        # fall back to legacy so the user never loses access to their
        # data because of a transient OS error (file lock, perms, etc).
        try:
            shutil.move(legacy_path, new_path)
            print(f"[cosmos] Migrated data dir: {legacy_path} → {new_path}",
                  file=sys.stderr)
        except OSError as e:
            print(f"[cosmos] Auto-migration failed ({e}); "
                  f"continuing to use legacy path {legacy_path}",
                  file=sys.stderr)
            return legacy_path

    if legacy_is_symlink and not cosmos_exists:
        # Dangling symlink (e.g. left over from an earlier bridge that
        # now points to nothing). Clean it so the next `os.makedirs`
        # below doesn't fail or create the dir under the symlink.
        try:
            os.unlink(legacy_path)
        except OSError:
            pass

    os.makedirs(new_path, exist_ok=True)
    return new_path


def data_dir() -> Path:
    """Where Cosmos stores brain.db, mcp_activity.jsonl, and any auxiliary
    JSON state. Override with COSMOS_DATA_DIR (absolute or cwd-relative)."""
    raw = os.environ.get("COSMOS_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    # Packaged/installed app: anchor to App Support (absolute, cwd-independent)
    # so the DB never depends on the launch working directory. The relative
    # DEFAULT_DATA_DIR is a dev-from-source convenience; in a FROZEN build it
    # would resolve against whatever cwd the OS handed the process (the repo
    # when launched from a repo shell, "/" via Finder, a fresh account's home)
    # — the exact mismatch behind "my data changed / is empty". brain_v2/
    # matches the on-disk layout under the Cosmos app-support folder.
    if getattr(sys, "frozen", False):
        return Path(default_app_support_dir()) / "brain_v2"
    return DEFAULT_DATA_DIR


def tier2_libs_dir() -> Path:
    """Where the Tier 2 add-on (torch, sentence_transformers, transformers,
    huggingface_hub, tokenizers, lancedb, pyarrow) gets installed to when
    a bundled .dmg user clicks Download.

    Lives under the platform's app-support dir, sibling to brain_v2/, so it
    survives app updates and uninstalling the .app does not auto-delete it
    (matches user expectation that "I downloaded 2 GB of models — don't
    nuke them just because I upgraded").

    Override with COSMOS_TIER2_LIBS_DIR for tests / portable installs."""
    raw = os.environ.get("COSMOS_TIER2_LIBS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(default_app_support_dir()) / "tier2-libs"


def brain_db_path() -> Path:
    """Path to the SQLite database file.

    Resolution order (first hit wins):
      1. COSMOS_BRAIN_DB  — explicit absolute path
      2. COSMOS_DATA_DIR  — directory; brain.db lives inside it
      3. default          — data/brain_v2/brain.db

    The benchmark sandbox sets (1) so the clone is unambiguous.
    """
    explicit = os.environ.get("COSMOS_BRAIN_DB", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return data_dir() / "brain.db"


def activity_log_path() -> Path:
    """Path to mcp_activity.jsonl — the per-call log the Outcome dashboard
    + benchmark activation-rate metrics read from. Override with
    COSMOS_ACTIVITY_LOG (absolute) or rely on COSMOS_DATA_DIR. Critical
    for benchmarks: a Fresh-brain sweep that writes into the user's real
    log would pollute future Outcome numbers."""
    explicit = os.environ.get("COSMOS_ACTIVITY_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return data_dir() / "mcp_activity.jsonl"


# ── Side-effect toggles ─────────────────────────────────────────────────

def lesson_mirror_enabled() -> bool:
    """When False, .cosmos/lessons.{md,json} writes are skipped entirely.

    Set COSMOS_DISABLE_LESSON_MIRROR=1 during benchmarks. Otherwise:
    project_registry resolves project_path from the user's real registry
    (not the sandbox) and a code_remember_error call from the benchmark
    would clobber the operator's working .cosmos/ directory mid-run.
    """
    return _truthy(os.environ.get("COSMOS_DISABLE_LESSON_MIRROR")) is False


def is_benchmark_sandbox() -> bool:
    """True when COSMOS_BENCHMARK=1. Useful for code paths that want to
    short-circuit any "phone home" or registry side-effect even if the
    individual flags weren't set. Belt-and-suspenders for the harness."""
    return _truthy(os.environ.get("COSMOS_BENCHMARK"))


# ── Helpers ─────────────────────────────────────────────────────────────

def _truthy(v: str | None) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def describe() -> dict:
    """Snapshot of every override that's currently in effect — included
    in /api/v2/status so the PreflightBanner / benchmark report can
    confirm the sandbox env var actually reached the sidecar."""
    return {
        "data_dir": str(data_dir()),
        "brain_db_path": str(brain_db_path()),
        "activity_log_path": str(activity_log_path()),
        "lesson_mirror_enabled": lesson_mirror_enabled(),
        "is_benchmark_sandbox": is_benchmark_sandbox(),
        "env_overrides_set": sorted(
            k for k in os.environ
            if k.startswith("COSMOS_") and os.environ[k].strip()
        ),
    }
