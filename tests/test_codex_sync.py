"""
Unit tests for codex_sync.py — the Codex append-only archival adapter.

The mirror engine itself (line-count copy, torn-line deferral, truncation
canary) is characterized in test_claude_code_hook.py; both adapters share it.
These tests pin the CODEX-specific behavior: $CODEX_HOME resolution, the
sweep-by-default scan root (the Codex Stop payload carries no transcript_path),
the sessions/-anchored archive mapping, payload parsing, and CODEX_SOURCES
archive resolution. resolve_archive_dir/main read the real .env only via a
monkeypatched ENV_FILE.
"""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.sync import codex_sync


def _write_lines(path: Path, dicts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(d) + "\n" for d in dicts), encoding="utf-8")


# ---------------------------------------------------------------------------
# codex_home — $CODEX_HOME override
# ---------------------------------------------------------------------------

def test_codex_home_defaults_to_dot_codex(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex_sync.codex_home() == Path.home() / ".codex"


def test_codex_home_honors_env_override(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/custom/codex")
    assert codex_sync.codex_home() == Path("/custom/codex")


# ---------------------------------------------------------------------------
# read_hook_payload
# ---------------------------------------------------------------------------

def _stdin(monkeypatch, text, isatty=False):
    stream = io.StringIO(text)
    stream.isatty = lambda: isatty
    monkeypatch.setattr(sys, "stdin", stream)


def test_read_hook_payload_parses_json(monkeypatch):
    _stdin(monkeypatch, '{"hook_event_name": "Stop", "session_id": "s1"}')
    assert codex_sync.read_hook_payload() == {"hook_event_name": "Stop", "session_id": "s1"}


def test_read_hook_payload_empty_is_dict(monkeypatch):
    _stdin(monkeypatch, "")
    assert codex_sync.read_hook_payload() == {}


def test_read_hook_payload_non_json_is_dict(monkeypatch):
    _stdin(monkeypatch, "not json at all")
    assert codex_sync.read_hook_payload() == {}


def test_read_hook_payload_tty_is_dict(monkeypatch):
    _stdin(monkeypatch, "ignored", isatty=True)
    assert codex_sync.read_hook_payload() == {}


# ---------------------------------------------------------------------------
# resolve_scan_root — sweep by default; scope only on a valid transcript_path
# ---------------------------------------------------------------------------

def test_resolve_scan_root_sweeps_without_transcript_path(monkeypatch, tmp_path):
    sessions = tmp_path / "codex" / "sessions"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", tmp_path / "codex")
    monkeypatch.setattr(codex_sync, "SESSIONS_DIR", sessions)
    # The Stop payload has session_id/cwd but no transcript_path → full sweep.
    payload = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/x"}
    assert codex_sync.resolve_scan_root(payload) == sessions


def test_resolve_scan_root_scopes_to_parent_for_valid_transcript(monkeypatch, tmp_path):
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(codex_sync, "SESSIONS_DIR", codex_dir / "sessions")
    rollout = codex_dir / "sessions" / "2026" / "06" / "13" / "rollout-x.jsonl"
    _write_lines(rollout, [{"i": 0}])
    root = codex_sync.resolve_scan_root({"transcript_path": str(rollout)})
    assert root == rollout.parent


def test_resolve_scan_root_falls_back_to_sweep_outside_codex_dir(monkeypatch, tmp_path):
    codex_dir = tmp_path / "codex"
    sessions = codex_dir / "sessions"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(codex_sync, "SESSIONS_DIR", sessions)
    outside = tmp_path / "elsewhere" / "rollout-evil.jsonl"
    _write_lines(outside, [{"i": 0}])
    # A transcript path outside CODEX_DIR must not be scoped to — sweep instead.
    assert codex_sync.resolve_scan_root({"transcript_path": str(outside)}) == sessions


def test_resolve_scan_root_falls_back_to_sweep_for_missing_file(monkeypatch, tmp_path):
    codex_dir = tmp_path / "codex"
    sessions = codex_dir / "sessions"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(codex_sync, "SESSIONS_DIR", sessions)
    assert codex_sync.resolve_scan_root(
        {"transcript_path": str(codex_dir / "sessions" / "nope.jsonl")}) == sessions


# ---------------------------------------------------------------------------
# sync_directory — sessions/ anchored archive mapping
# ---------------------------------------------------------------------------

def test_sync_directory_mirrors_sessions_date_tree(tmp_path, monkeypatch, capsys):
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    sessions = codex_dir / "sessions"
    _write_lines(sessions / "2026" / "06" / "13" / "rollout-a.jsonl", [{"i": 0}, {"i": 1}])
    _write_lines(sessions / "2026" / "05" / "25" / "rollout-b.jsonl", [{"i": 0}])
    archive_dir = tmp_path / "archive"

    codex_sync.sync_directory(sessions, archive_dir, "Stop")

    # The tail AFTER the 'sessions' anchor is reproduced under the archive root.
    assert (archive_dir / "2026" / "06" / "13" / "rollout-a.jsonl").read_text().count("\n") == 2
    assert (archive_dir / "2026" / "05" / "25" / "rollout-b.jsonl").read_text().count("\n") == 1
    assert "[Stop] Archived 3 new lines across 2 file(s)" in capsys.readouterr().err


def test_sync_directory_idempotent(tmp_path, monkeypatch, capsys):
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    sessions = codex_dir / "sessions"
    _write_lines(sessions / "2026" / "06" / "13" / "rollout-a.jsonl", [{"i": 0}])
    archive_dir = tmp_path / "archive"

    codex_sync.sync_directory(sessions, archive_dir, "Stop")
    capsys.readouterr()
    codex_sync.sync_directory(sessions, archive_dir, "Stop")  # nothing new
    assert "Archived" not in capsys.readouterr().err


def test_sync_directory_archives_real_fixture(tmp_path, monkeypatch):
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    sessions = codex_dir / "sessions"
    day = sessions / "2026" / "06" / "13"
    day.mkdir(parents=True)
    fixture = (Path(__file__).parent / "fixtures" / "sample_codex_session_with_tools.jsonl")
    dest = day / "rollout-2026-06-13T20-58-34-719ec1c6.jsonl"
    dest.write_bytes(fixture.read_bytes())
    archive_dir = tmp_path / "archive"

    codex_sync.sync_directory(sessions, archive_dir, "sweep")

    archived = archive_dir / "2026" / "06" / "13" / "rollout-2026-06-13T20-58-34-719ec1c6.jsonl"
    assert archived.exists()
    # complete-line copy: byte-identical to the source (fixture ends with \n)
    assert archived.read_bytes() == fixture.read_bytes()


# ---------------------------------------------------------------------------
# resolve_archive_dir — CODEX_SOURCES → per-host archive path
# ---------------------------------------------------------------------------

def _env(tmp_path, monkeypatch, text):
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    monkeypatch.setattr(codex_sync, "ENV_FILE", env)


def test_resolve_archive_dir_matches_host(tmp_path, monkeypatch):
    archive = tmp_path / "arch" / "boxA"
    _env(tmp_path, monkeypatch,
         f"CODEX_SOURCES=boxA={archive}\nMACHINE_NAME=boxA\n")
    assert codex_sync.resolve_archive_dir() == archive


def test_resolve_archive_dir_honors_legacy_host_key(tmp_path, monkeypatch):
    # Pre-rename .env files still resolve via the CLAUDE_CODE_HOST fallback.
    archive = tmp_path / "arch" / "boxA"
    _env(tmp_path, monkeypatch,
         f"CODEX_SOURCES=boxA={archive}\nCLAUDE_CODE_HOST=boxA\n")
    assert codex_sync.resolve_archive_dir() == archive


def test_resolve_archive_dir_unset_raises(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, "LLM_DATA_DIR=/x\n")
    with pytest.raises(RuntimeError, match="CODEX_SOURCES is not set"):
        codex_sync.resolve_archive_dir()


def test_resolve_archive_dir_no_host_match_raises(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch,
         "CODEX_SOURCES=otherbox=/data/codex\nMACHINE_NAME=thisbox\n")
    with pytest.raises(RuntimeError, match="No entry for host 'thisbox'"):
        codex_sync.resolve_archive_dir()


# ---------------------------------------------------------------------------
# main — sweep wiring (end to end, archive resolved from .env)
# ---------------------------------------------------------------------------

def test_main_sweeps_sessions_into_archive(tmp_path, monkeypatch):
    codex_dir = tmp_path / "codex"
    sessions = codex_dir / "sessions"
    monkeypatch.setattr(codex_sync, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(codex_sync, "SESSIONS_DIR", sessions)
    _write_lines(sessions / "2026" / "06" / "13" / "rollout-a.jsonl", [{"i": 0}, {"i": 1}])
    archive = tmp_path / "arch" / "boxA"
    _env(tmp_path, monkeypatch, f"CODEX_SOURCES=boxA={archive}\nMACHINE_NAME=boxA\n")
    # Stop payload: no transcript_path → main() sweeps SESSIONS_DIR.
    _stdin(monkeypatch, '{"hook_event_name": "Stop", "session_id": "s1"}')

    codex_sync.main()

    assert (archive / "2026" / "06" / "13" / "rollout-a.jsonl").read_text().count("\n") == 2
