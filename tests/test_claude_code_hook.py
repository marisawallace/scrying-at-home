"""
Characterization tests for claude_code_hook.py — the append-only mirror engine.

These pin the CURRENT behavior of the sync hook (line-count incremental copy,
torn-line deferral, corrupt-line skip, truncation canary, the projects-anchored
archive path mapping, and the traversal guard) so an upcoming refactor that
lifts this engine into a shared, re-parameterizable module (anchoring on
`sessions/` for Codex instead of `projects/`) cannot silently regress it.

All tests are pure-filesystem units: no subprocess, no .env, no network. The
two module-level path constants the hook hardcodes (CLAUDE_DIR, ANOMALY_LOG)
are monkeypatched into tmp_path; resolve_archive_dir()/main() (which read the
real .env) are never called.
"""
import json
import re
import sys
from pathlib import Path

import pytest

# Add project root to path (matches the other unit tests)
sys.path.insert(0, str(Path(__file__).parent.parent))
import claude_code_hook as hook


def _write_lines(path: Path, dicts: list[dict]) -> None:
    """Write `dicts` as one JSON object per newline-terminated line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(d) + "\n" for d in dicts), encoding="utf-8")


# ---------------------------------------------------------------------------
# sync_transcript
# ---------------------------------------------------------------------------

def test_sync_fresh_full_copy(tmp_path):
    src = tmp_path / "src.jsonl"
    _write_lines(src, [{"i": 0}, {"i": 1}, {"i": 2}])
    archive = tmp_path / "arch" / "dst.jsonl"  # parent does not exist yet

    n = hook.sync_transcript(src, archive)

    assert n == 3
    assert archive.exists()
    assert archive.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_sync_incremental_append(tmp_path):
    src = tmp_path / "src.jsonl"
    dicts = [{"i": 0}, {"i": 1}, {"i": 2}, {"i": 3}]
    _write_lines(src, dicts)
    archive = tmp_path / "dst.jsonl"
    _write_lines(archive, dicts[:2])  # archive already has the first two lines

    n = hook.sync_transcript(src, archive)

    assert n == 2  # only the new tail is written
    assert archive.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_sync_idempotent_noop_when_sizes_equal(tmp_path):
    src = tmp_path / "src.jsonl"
    _write_lines(src, [{"i": 0}, {"i": 1}, {"i": 2}])
    archive = tmp_path / "dst.jsonl"
    _write_lines(archive, [{"i": 0}, {"i": 1}, {"i": 2}])
    before = archive.read_text(encoding="utf-8")

    assert hook.sync_transcript(src, archive) == 0
    assert archive.read_text(encoding="utf-8") == before


def test_sync_shortcut_when_archive_larger(tmp_path):
    # Pins the cheap shortcut's `>=` (not `==`): an archive strictly larger than
    # the source short-circuits to 0 with no read/append. Regressing to `==`
    # would make every already-complete file re-read on every Stop.
    src = tmp_path / "src.jsonl"
    _write_lines(src, [{"i": 0}])
    archive = tmp_path / "dst.jsonl"
    _write_lines(archive, [{"i": 0}, {"i": 1}, {"i": 2}])
    before = archive.read_text(encoding="utf-8")

    assert hook.sync_transcript(src, archive) == 0
    assert archive.read_text(encoding="utf-8") == before


def test_sync_torn_trailing_line_deferred(tmp_path):
    src = tmp_path / "src.jsonl"
    src.write_bytes(b'{"a": 1}\n{"b": 2}\n{"torn": ')  # last line has no newline
    archive = tmp_path / "dst.jsonl"

    n = hook.sync_transcript(src, archive)

    assert n == 2  # only the two complete lines
    text = archive.read_text(encoding="utf-8")
    assert text == '{"a": 1}\n{"b": 2}\n'
    assert "torn" not in text  # the partial line is deferred, never written


def test_sync_skips_blank_and_corrupt_lines_with_warning(tmp_path, capsys):
    src = tmp_path / "src.jsonl"
    src.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n', encoding="utf-8")
    archive = tmp_path / "dst.jsonl"

    n = hook.sync_transcript(src, archive)

    assert n == 2  # blank line and corrupt line both skipped
    assert archive.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'
    err = capsys.readouterr().err
    # Assert the stable prefix + line number, not the line[:80]!r repr.
    assert "Warning: skipping corrupt JSONL at line 3" in err


def test_sync_truncation_detected_logs_anomaly(tmp_path, capsys, monkeypatch):
    anomaly_log = tmp_path / "anomalies.log"
    monkeypatch.setattr(hook, "ANOMALY_LOG", anomaly_log)

    # Archive has MORE lines than the source (source shrank = truncation).
    # The lines are sized so the archive is SMALLER in bytes than the source,
    # otherwise the `archive size >= source size` shortcut returns before
    # truncation is ever checked.
    archive = tmp_path / "dst.jsonl"
    archive.write_text("{}\n" * 5, encoding="utf-8")  # 5 short lines, 15 bytes
    src = tmp_path / "src.jsonl"
    _write_lines(src, [{"x": "a" * 60}] * 3)  # 3 long lines, > 15 bytes

    n = hook.sync_transcript(src, archive)

    assert n == 0  # nothing appended (source is a prefix subset by count)
    assert archive.read_text(encoding="utf-8") == "{}\n" * 5  # archive untouched
    logged = anomaly_log.read_text(encoding="utf-8")
    assert "TRUNCATION DETECTED" in logged
    assert "archive has 5 lines, source has 3 complete lines" in logged
    assert "ANOMALY:" in capsys.readouterr().err


def test_sync_nonexistent_source_returns_zero(tmp_path):
    archive = tmp_path / "dst.jsonl"
    assert hook.sync_transcript(tmp_path / "missing.jsonl", archive) == 0
    assert not archive.exists()


# ---------------------------------------------------------------------------
# get_archive_path
# ---------------------------------------------------------------------------

def test_archive_path_mirrors_projects_subtree(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "CLAUDE_DIR", tmp_path / "claude")
    slug = "-home-testuser-projects-my-app"
    transcript = tmp_path / "claude" / "projects" / slug / "cc-test-session-001.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    archive_dir = tmp_path / "archive"

    result = hook.get_archive_path(transcript, archive_dir)

    expected = (archive_dir / slug / "cc-test-session-001.jsonl").resolve()
    assert result == expected


def test_archive_path_no_projects_component_raises(tmp_path):
    transcript = tmp_path / "random" / "foo.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    with pytest.raises(ValueError, match="no 'projects' component"):
        hook.get_archive_path(transcript, tmp_path / "archive")


# NOTE: the escape branch (`not result.is_relative_to(archive_dir)`) is
# effectively unreachable through get_archive_path's own contract: the source
# path is `.resolve()`d before `parts.index("projects")`, so the post-projects
# remainder is already normalized (no `..`), and joining it under archive_dir
# can never escape. It is defensive and is intentionally left uncharacterized
# rather than pinned with a contrived input.


# ---------------------------------------------------------------------------
# validate_source_path
# ---------------------------------------------------------------------------

def test_validate_source_under_claude_dir_ok(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    monkeypatch.setattr(hook, "CLAUDE_DIR", claude_dir)
    inside = claude_dir / "projects" / "p" / "s.jsonl"
    inside.parent.mkdir(parents=True)
    inside.write_text("{}\n")
    assert hook.validate_source_path(inside) is None  # no raise


def test_validate_source_outside_claude_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "CLAUDE_DIR", tmp_path / "claude")
    outside = tmp_path / "elsewhere" / "s.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}\n")
    with pytest.raises(ValueError, match="Refusing to archive file outside"):
        hook.validate_source_path(outside)


# ---------------------------------------------------------------------------
# sync_directory
# ---------------------------------------------------------------------------

def test_sync_directory_archives_all_and_reports(tmp_path, capsys, monkeypatch):
    claude_dir = tmp_path / "claude"
    monkeypatch.setattr(hook, "CLAUDE_DIR", claude_dir)
    projects = claude_dir / "projects"
    _write_lines(projects / "proj-a" / "s1.jsonl", [{"i": 0}, {"i": 1}])
    _write_lines(projects / "proj-b" / "s2.jsonl", [{"i": 0}])
    archive_dir = tmp_path / "archive"

    hook.sync_directory(projects, archive_dir, "Stop")

    assert (archive_dir / "proj-a" / "s1.jsonl").read_text().count("\n") == 2
    assert (archive_dir / "proj-b" / "s2.jsonl").read_text().count("\n") == 1
    err = capsys.readouterr().err
    assert "[Stop] Archived 3 new lines across 2 file(s)" in err


def test_sync_directory_silent_when_nothing_new(tmp_path, capsys, monkeypatch):
    claude_dir = tmp_path / "claude"
    monkeypatch.setattr(hook, "CLAUDE_DIR", claude_dir)
    projects = claude_dir / "projects"
    _write_lines(projects / "proj" / "s.jsonl", [{"i": 0}, {"i": 1}])
    archive_dir = tmp_path / "archive"

    hook.sync_directory(projects, archive_dir, "Stop")  # first pass: archives
    capsys.readouterr()  # clear
    hook.sync_directory(projects, archive_dir, "Stop")  # second pass: no change

    assert "Archived" not in capsys.readouterr().err


def test_sync_directory_skips_invalid_paths(tmp_path, capsys, monkeypatch):
    # Scan CLAUDE_DIR itself so a stray jsonl with no `projects` component is
    # encountered: it passes validate_source_path (under CLAUDE_DIR) but fails
    # get_archive_path → "Skipping" printed, loop continues, valid file archived.
    claude_dir = tmp_path / "claude"
    monkeypatch.setattr(hook, "CLAUDE_DIR", claude_dir)
    claude_dir.mkdir()
    (claude_dir / "orphan.jsonl").write_text('{"i": 0}\n')
    _write_lines(claude_dir / "projects" / "proj" / "s.jsonl", [{"i": 0}])
    archive_dir = tmp_path / "archive"

    hook.sync_directory(claude_dir, archive_dir, "SessionEnd")

    assert (archive_dir / "proj" / "s.jsonl").exists()
    assert not (archive_dir / "orphan.jsonl").exists()
    assert "Skipping" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _log_anomaly
# ---------------------------------------------------------------------------

def test_log_anomaly_writes_timestamped_entry(tmp_path, capsys, monkeypatch):
    log = tmp_path / "anomalies.log"
    monkeypatch.setattr(hook, "ANOMALY_LOG", log)

    hook._log_anomaly("hello world")

    content = log.read_text(encoding="utf-8")
    assert re.match(r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\] hello world\n$", content)
    assert "ANOMALY: hello world" in capsys.readouterr().err


def test_log_anomaly_falls_back_to_stderr_on_oserror(tmp_path, capsys, monkeypatch):
    # Parent dir does not exist → open(..., "a") raises OSError; the message
    # still reaches stderr and no exception propagates.
    monkeypatch.setattr(hook, "ANOMALY_LOG", tmp_path / "nope" / "anomalies.log")
    hook._log_anomaly("disk gone")
    err = capsys.readouterr().err
    assert "ANOMALY: disk gone" in err
    assert "could not write" in err
