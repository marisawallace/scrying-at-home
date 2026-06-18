"""Opt-in local-source refresh for the search and view CLIs.

The Stop hook copies a transcript microseconds before Claude Code flushes the
turn's final assistant message (~180ms window observed), so the archive — and
thus the search index and local views — sits one assistant turn behind until
the next Stop/SessionEnd sweep catches up. `--refresh` closes that gap on
demand: it runs the same local-host sweep the hooks run, mirroring this
machine's live ~/.claude/projects and ~/.codex/sessions into their archives
before the caller reconciles.

Only the LOCAL host's live sources exist on this machine, so remote hosts are
untouched — they sync asynchronously regardless. A provider with no archive
entry for this host (the user runs only one of the two CLIs) is skipped
silently; its RuntimeError is cosmetic here, not a failure.
"""

from __future__ import annotations

from pathlib import Path

from scrying_at_home.config.paths import (
    CLAUDE_CODE_SOURCES_ENV_KEY,
    CODEX_SOURCES_ENV_KEY,
    parse_claude_code_sources,
    parse_codex_sources,
    resolve_provider_archive_dir,
)
from scrying_at_home.sync import claude_code_hook, codex_sync


def refresh_local_sources(config: dict, env_file: Path) -> None:
    """Sweep this machine's live Claude Code and Codex transcripts into their
    archives. Best-effort: a provider not configured for this host is skipped."""
    try:
        archive_dir = resolve_provider_archive_dir(
            config, parse_claude_code_sources(config),
            env_key=CLAUDE_CODE_SOURCES_ENV_KEY, env_file=env_file,
            setup_command="python migrations/002_setup_claude_code_archival.py",
        )
        claude_code_hook.sync_directory(
            claude_code_hook.CLAUDE_PROJECTS_DIR, archive_dir, "refresh")
    except RuntimeError:
        pass

    try:
        archive_dir = resolve_provider_archive_dir(
            config, parse_codex_sources(config),
            env_key=CODEX_SOURCES_ENV_KEY, env_file=env_file,
            setup_command="python migrations/004_setup_codex_archival.py",
        )
        codex_sync.sync_directory(
            codex_sync.SESSIONS_DIR, archive_dir, "refresh")
    except RuntimeError:
        pass
