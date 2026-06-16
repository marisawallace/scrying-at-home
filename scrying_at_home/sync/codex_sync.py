#!/usr/bin/env python3
"""
OpenAI Codex session archival hook.

Wired into Codex via a Stop lifecycle hook in ~/.codex/hooks.json (see
migrations/004_setup_codex_archival.py), which fires when a conversation turn
completes. Sibling to claude_code_hook.py: a thin adapter over the generic
mirror_engine that pins the Codex specifics — the $CODEX_HOME traversal root,
the 'sessions/' path anchor, and the codex_anomalies.log canary — and delegates
the append-only copy logic.

Why a SWEEP rather than a per-file sync: unlike Claude Code's Stop hook, the
Codex Stop payload does NOT carry the rollout transcript path. It provides
session_id, cwd, hook_event_name, turn_id, stop_hook_active,
last_assistant_message, and permission_mode — no transcript_path (confirmed
against codex-cli 0.133.0's hooks documentation). So every invocation reconciles
all rollouts under $CODEX_HOME/sessions into the archive. The sweep is idempotent
and cheap: mirror_engine.sync_transcript skips any file whose archive copy is
already at least as large (a single stat()), so unchanged sessions cost nothing
and only the just-appended active rollout is re-read. Codex rollouts are
append-only and complete-once-written, so a missed Stop loses nothing — the next
turn's Stop (or a manual run) sweeps it up.

Run modes:
  - As a Codex Stop hook (stdin = the hook JSON payload): full sessions sweep.
  - Manually with no stdin (initial backfill, or a periodic backstop): full sweep.
  - With an explicit transcript_path on stdin (forward-compat for events that do
    carry it, or manual use): scoped to that file's parent directory.

Archive destination: read from CODEX_SOURCES in the repo's .env, using the entry
matching the current host. migrations/004_setup_codex_archival.py sets this up.

Key assumption (shared with mirror_engine): rollout transcripts are immutable
append-only logs. Truncation (archive longer than source) is reported to
codex_anomalies.log as a canary, never silently reconciled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scrying_at_home.config.paths import (
    REPO_ROOT,
    CODEX_SOURCES_ENV_KEY,
    codex_home,
    load_env_file,
    parse_codex_sources,
    resolve_env_path,
    resolve_provider_archive_dir,
)
from scrying_at_home.sync import mirror_engine

CODEX_DIR = codex_home()
SESSIONS_DIR = CODEX_DIR / "sessions"
ANOMALY_LOG = REPO_ROOT / "codex_anomalies.log"
ENV_FILE = resolve_env_path(REPO_ROOT, None)


def resolve_archive_dir() -> Path:
    """Return the archive path for this machine from CODEX_SOURCES.

    The host key is read from MACHINE_NAME (or the legacy CLAUDE_CODE_HOST) if
    set, else a normalized socket.gethostname() (the host identity is shared
    with Claude Code).
    """
    config = load_env_file(ENV_FILE)
    return resolve_provider_archive_dir(
        config, parse_codex_sources(config),
        env_key=CODEX_SOURCES_ENV_KEY, env_file=ENV_FILE,
        setup_command="python migrations/004_setup_codex_archival.py",
    )


# --- Mirror-engine adapter --------------------------------------------------
# Thin wrapper over mirror_engine binding the Codex specifics. Reads its
# module-level constants (CODEX_DIR / ANOMALY_LOG) at call time so they stay
# overridable (tests monkeypatch them).

def _log_anomaly(message: str) -> None:
    mirror_engine.log_anomaly(message, ANOMALY_LOG)


def sync_directory(scan_root: Path, archive_dir: Path, event: str) -> None:
    """Sync every rollout-*.jsonl under scan_root into archive_dir, mirroring
    the sessions/ date tree (sessions/YYYY/MM/DD/rollout-*.jsonl).

    The 'rollout-*' filter keeps the archive in lockstep with what is actually
    searchable: search_index._is_codex_rollout indexes only rollout-* files, and
    the migration 004 backfill globs the same pattern. Without it the sweep would
    copy any other .jsonl Codex drops under sessions/ into the archive, where it
    would sit invisible to search (and defensively, it keeps non-transcript files
    out of the archive entirely)."""
    mirror_engine.sync_directory(
        scan_root, archive_dir, event,
        anchor="sessions", validate_root=CODEX_DIR, log_anomaly=_log_anomaly,
        glob="rollout-*.jsonl",
    )


# --- Imperative shell -------------------------------------------------------

def read_hook_payload() -> dict:
    """Parse the hook JSON from stdin; {} when there is no payload.

    A manual invocation (no piped stdin) or a non-JSON / empty body yields {},
    which resolve_scan_root reads as "full sweep".
    """
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def resolve_scan_root(payload: dict) -> Path:
    """The directory to reconcile this run.

    The Codex Stop payload carries no transcript_path, so default to a full
    sweep of the sessions tree. If a transcript_path IS present (a forward-compat
    event that carries it, or a manual invocation) and resolves to a real file
    under CODEX_DIR, scope to its parent directory for a cheaper sync.
    """
    transcript_path = payload.get("transcript_path")
    if transcript_path:
        path = Path(transcript_path)
        if path.is_file():
            try:
                mirror_engine.validate_source_path(path, CODEX_DIR)
                return path.parent
            except ValueError:
                pass  # outside CODEX_DIR — fall back to the safe full sweep
    return SESSIONS_DIR


def main():
    payload = read_hook_payload()
    event = payload.get("hook_event_name", "sweep")

    try:
        archive_dir = resolve_archive_dir()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    scan_root = resolve_scan_root(payload)
    if not scan_root.exists():
        print(f"Warning: Codex sessions directory not found: {scan_root}",
              file=sys.stderr, flush=True)
        return

    sync_directory(scan_root, archive_dir, event)


if __name__ == "__main__":
    main()
