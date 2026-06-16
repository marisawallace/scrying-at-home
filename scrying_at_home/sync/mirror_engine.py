"""
Generic append-only transcript mirror engine.

Local-CLI coding tools (Claude Code, OpenAI Codex) write each session as an
append-only JSONL transcript. This module mirrors those transcripts into a
per-host archive directory, copying only newly-appended complete lines and
preserving the source's path layout under the archive root. It is the engine
behind claude_code_hook.py (and, for Codex, a forthcoming codex_sync.py); those
modules are thin adapters that supply the provider-specific anchor, traversal
root, and anomaly logger.

Leaf module: stdlib only, no project imports, so anything can depend on it.

Key assumption (the same one the hook documents): source transcripts are
immutable append-only logs — context compression happens at API-request time
and does not rewrite the file. The line-count incremental sync depends on this;
truncation (source shorter than the archive) is reported via the injected
`log_anomaly` callback as a canary, never silently reconciled.
"""

from __future__ import annotations

import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def validate_source_path(transcript_path: Path, root: Path) -> None:
    """Ensure `transcript_path` is under `root` to prevent path traversal.

    The adapter passes its provider's home (e.g. ~/.claude, ~/.codex); only
    files physically inside it may be archived.
    """
    try:
        transcript_path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Refusing to archive file outside {root}: {transcript_path}")


def get_archive_path(transcript_path: Path, archive_dir: Path,
                     anchor: str = "projects") -> Path:
    """Mirror the path tail below `anchor` under `archive_dir`.

    Claude Code anchors on 'projects' (~/.claude/projects/<slug>/<id>.jsonl);
    Codex anchors on 'sessions' (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).
    The tail after the anchor component is reproduced verbatim under the archive.
    """
    parts = transcript_path.resolve().parts
    try:
        anchor_idx = parts.index(anchor)
    except ValueError:
        raise ValueError(f"Transcript path has no {anchor!r} component: {transcript_path}")
    result = (archive_dir / Path(*parts[anchor_idx + 1:])).resolve()
    if not result.is_relative_to(archive_dir.resolve()):
        raise ValueError(f"Derived archive path escapes archive directory: {result}")
    return result


def sync_transcript(transcript_path: Path, archive_path: Path,
                    log_anomaly: Callable[[str], None]) -> int:
    """Append new lines from transcript to archive. Returns new lines written.

    Truncation (the source has fewer complete lines than the archive already
    holds) is reported via `log_anomaly` and never reconciled — the archive is
    left intact, since the append-only assumption has been violated.
    """
    if not transcript_path.exists():
        return 0

    # Cheap shortcut: if the archive exists and is at least as large as the
    # source, there can be no new bytes to append. Skips the read+line-count
    # entirely on every Stop where nothing changed.
    if archive_path.exists():
        try:
            if archive_path.stat().st_size >= transcript_path.stat().st_size:
                return 0
        except OSError:
            pass  # fall through to the full sync path

    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with open(archive_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            existing_lines = sum(1 for _ in f)

            source_lines = []
            source_total = 0
            with open(transcript_path, "r", encoding="utf-8") as src:
                for i, line in enumerate(src):
                    if not line.endswith("\n"):
                        # Incomplete line — writer hasn't flushed. Defer to next sync.
                        break
                    source_total = i + 1
                    if i < existing_lines:
                        continue
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        print(
                            f"Warning: skipping corrupt JSONL at line {i+1}: {line[:80]!r}",
                            file=sys.stderr,
                        )
                        continue
                    source_lines.append(line)

            if existing_lines > source_total > 0:
                log_anomaly(
                    f"TRUNCATION DETECTED: {transcript_path}\n"
                    f"  archive has {existing_lines} lines, "
                    f"source has {source_total} complete lines\n"
                    f"  archive: {archive_path}"
                )

            if source_lines:
                f.seek(0, 2)
                f.writelines(source_lines)

            return len(source_lines)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sync_directory(scan_root: Path, archive_dir: Path, event: str, *,
                   anchor: str, validate_root: Path,
                   log_anomaly: Callable[[str], None],
                   glob: str = "*.jsonl") -> None:
    """Sync every file matching `glob` under scan_root into archive_dir.

    `glob` defaults to all '*.jsonl' (Claude Code: every file under a project
    dir is a transcript). An adapter narrows it when its tree mixes transcripts
    with other .jsonl files — Codex passes 'rollout-*.jsonl' so the archive
    holds exactly what the indexer treats as a transcript and nothing else.

    Files that fail validation or path mapping are skipped (with a note) so one
    stray file never aborts the sweep.
    """
    total_files = 0
    total_lines = 0
    for jsonl in scan_root.rglob(glob):
        try:
            validate_source_path(jsonl, validate_root)
            archive_path = get_archive_path(jsonl, archive_dir, anchor=anchor)
        except ValueError as e:
            print(f"Skipping {jsonl}: {e}", file=sys.stderr)
            continue
        new_lines = sync_transcript(jsonl, archive_path, log_anomaly)
        if new_lines > 0:
            total_files += 1
            total_lines += new_lines
    if total_lines > 0:
        print(
            f"[{event}] Archived {total_lines} new lines across {total_files} file(s) under {scan_root}",
            file=sys.stderr,
        )


def log_anomaly(message: str, log_path: Path) -> None:
    """Append a timestamped anomaly entry to `log_path`, mirrored to stderr.

    Best-effort: if the log can't be written (read-only repo, missing parent),
    the ANOMALY line still reaches stderr and the write failure is noted.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"[{timestamp}] {message}\n"
    print(f"ANOMALY: {message}", file=sys.stderr)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        print(f"  (could not write {log_path}: {e})", file=sys.stderr)
