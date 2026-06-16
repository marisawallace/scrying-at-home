#!/usr/bin/env python3
"""
Claude Code session archival hook.

Wired into Claude Code via two hooks in ~/.claude/settings.json:

  Stop          -> reconcile JSONLs in the current session's project dir
                   (catches the parent session and any subagent transcripts)
  SessionEnd    -> full sweep of ~/.claude/projects/ (catches anything missed,
                   e.g. crashed sessions, abandoned project dirs)

Reconciliation, not event-reaction: each invocation makes the archive match
the source for the JSONLs in scope, rather than archiving only the file
named in the hook payload. This means a missed event gets swept up by the
next one, and subagent transcripts (which don't appear as transcript_path)
are caught by the per-Stop scoped scan.

Archive destination: read from CLAUDE_CODE_SOURCES in the repo's .env, using
the entry matching the current hostname. The migration script
(migrations/002_setup_claude_code_archival.py) sets this up automatically.

This is a thin adapter over the generic mirror_engine: it pins the Claude Code
specifics (the ~/.claude traversal root, the 'projects/' path anchor, and the
claude_code_anomalies.log canary) and delegates the append-only copy logic. The
Codex sync will be a sibling adapter anchoring on 'sessions/' under ~/.codex.

Key assumption: source JSONL transcripts are immutable append-only logs.
Claude Code's context compression operates at API request time and does not
rewrite transcript files. The line-count based sync depends on this — if it
ever changes, archives could diverge. Truncation detection writes to
claude_code_anomalies.log as a canary for this assumption.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scrying_at_home.config.paths import (
    REPO_ROOT,
    CLAUDE_CODE_SOURCES_ENV_KEY,
    load_env_file,
    parse_claude_code_sources,
    resolve_env_path,
    resolve_provider_archive_dir,
)
from scrying_at_home.sync import mirror_engine

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
ANOMALY_LOG = REPO_ROOT / "claude_code_anomalies.log"
ENV_FILE = resolve_env_path(REPO_ROOT, None)


def resolve_archive_dir() -> Path:
    """Return the archive path for this machine from CLAUDE_CODE_SOURCES.

    The host key is read from MACHINE_NAME (or the legacy CLAUDE_CODE_HOST) if
    set, else falls back to a normalized socket.gethostname().
    """
    config = load_env_file(ENV_FILE)
    return resolve_provider_archive_dir(
        config, parse_claude_code_sources(config),
        env_key=CLAUDE_CODE_SOURCES_ENV_KEY, env_file=ENV_FILE,
        setup_command="python migrations/002_setup_claude_code_archival.py",
    )


# --- Mirror-engine adapters -------------------------------------------------
# Thin wrappers over mirror_engine that bind the Claude Code specifics. Each
# reads its module-level constant (CLAUDE_DIR / ANOMALY_LOG) at call time, so
# they stay overridable (tests monkeypatch them).

def validate_source_path(transcript_path: Path) -> None:
    """Ensure the transcript path is under ~/.claude/ to prevent path traversal."""
    mirror_engine.validate_source_path(transcript_path, CLAUDE_DIR)


def get_archive_path(transcript_path: Path, archive_dir: Path) -> Path:
    """Mirror project/session structure under archive_dir.

    Source: ~/.claude/projects/<project-slug>/<session-id>.jsonl
    Dest:   <archive_dir>/<project-slug>/<session-id>.jsonl
    """
    return mirror_engine.get_archive_path(transcript_path, archive_dir, anchor="projects")


def _log_anomaly(message: str) -> None:
    mirror_engine.log_anomaly(message, ANOMALY_LOG)


def sync_transcript(transcript_path: Path, archive_path: Path) -> int:
    """Append new lines from transcript to archive. Returns number of new lines written."""
    return mirror_engine.sync_transcript(transcript_path, archive_path, _log_anomaly)


def sync_directory(scan_root: Path, archive_dir: Path, event: str) -> None:
    """Sync every *.jsonl under scan_root into archive_dir."""
    mirror_engine.sync_directory(
        scan_root, archive_dir, event,
        anchor="projects", validate_root=CLAUDE_DIR, log_anomaly=_log_anomaly,
    )


def main():
    hook_input = json.load(sys.stdin)
    transcript_path = Path(hook_input.get("transcript_path", ""))
    event = hook_input.get("hook_event_name", "unknown")

    try:
        archive_dir = resolve_archive_dir()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    if event == "SessionEnd":
        scan_root = CLAUDE_PROJECTS_DIR
    else:
        if not transcript_path.exists():
            print(f"Warning: transcript not found: {transcript_path}", file=sys.stderr, flush=True)
            return
        try:
            validate_source_path(transcript_path)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        scan_root = transcript_path.parent

    sync_directory(scan_root, archive_dir, event)


if __name__ == "__main__":
    main()
