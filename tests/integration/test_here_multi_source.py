"""
Integration tests for --here when both local-CLI sources are configured.

The miss hint is emitted per source block but suppressed unless the whole --here
search comes up empty — so a source with no match here is silent when a sibling
source did match (the bug this guards against), and both hints fire only when
nothing matched anywhere.
"""
import json
from pathlib import Path

import pytest


def _write_cc_session(path: Path, session_id: str, cwd: str, timestamp: str, text: str):
    """Minimal two-line Claude Code JSONL session recorded under `cwd`."""
    lines = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": session_id},
        {
            "parentUuid": None, "type": "user",
            "message": {"role": "user", "content": text},
            "uuid": f"{session_id}-msg", "timestamp": timestamp,
            "cwd": cwd, "sessionId": session_id, "gitBranch": "main",
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _write_codex_rollout(path: Path, session_id: str, cwd: str, created_at: str, text: str):
    """Minimal Codex rollout (session_meta + turn_context + a turn) recorded under `cwd`."""
    lines = [
        {"timestamp": created_at, "type": "session_meta",
         "payload": {"id": session_id, "timestamp": created_at, "cwd": cwd,
                     "cli_version": "0.133.0"}},
        {"timestamp": created_at, "type": "turn_context",
         "payload": {"model": "gpt-5.5", "cwd": cwd}},
        {"timestamp": created_at, "type": "event_msg",
         "payload": {"type": "user_message", "message": text}},
        {"timestamp": created_at, "type": "event_msg",
         "payload": {"type": "agent_message", "message": "ok", "phase": "final_answer"}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def two_source_workspace(isolated_workspace, tmp_path):
    """A workspace where a claude-code session and a codex session both match the
    query 'alpha', but sit in different directories.

    Returns (env_path, here_dir): `here_dir` holds only the claude-code session.
    """
    here_dir = tmp_path / "here"
    other_dir = tmp_path / "elsewhere"
    here_dir.mkdir()
    other_dir.mkdir()

    cc_dir = isolated_workspace / "claude_code_data"
    cc_project = cc_dir / "-tmp-here"
    cc_project.mkdir(parents=True)
    _write_cc_session(cc_project / "cc-here.jsonl", "cc-here", str(here_dir),
                      "2026-05-01T10:00:00.000Z", "alpha in a claude code session")

    codex_dir = isolated_workspace / "codex_data"
    _write_codex_rollout(
        codex_dir / "2026" / "05" / "01" / "rollout-2026-05-01T10-00-00-codex-elsewhere.jsonl",
        "codex-elsewhere", str(other_dir), "2026-05-01T10:00:00", "alpha in a codex session")

    env_path = isolated_workspace / ".env"
    env_path.write_text(
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"CODEX_SOURCES=testhost={codex_dir}\n"
        f"MACHINE_NAME=testhost\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
    )
    return env_path, here_dir


@pytest.mark.integration
def test_sibling_match_suppresses_other_source_hint(two_source_workspace, run_cli):
    """claude-code matches here, codex does not → codex's miss hint is suppressed."""
    env_path, here_dir = two_source_workspace
    result = run_cli(
        "full_text_search_chats_archive.py", "alpha", "--here",
        config=env_path, cwd=here_dir,
    )

    assert result.returncode == 0, result.stderr
    # The claude-code session is shown; the search did not come up empty.
    assert "cc-here" in result.stdout
    assert "No results found." not in result.stdout
    # Codex came up empty here, but a sibling source matched, so no hint fires.
    assert "source:" not in result.stderr


@pytest.mark.integration
def test_total_miss_reports_every_source(two_source_workspace, run_cli, tmp_path):
    """Nothing matches under the target dir → one hint per missed source."""
    env_path, _here_dir = two_source_workspace
    nowhere = tmp_path / "nowhere"  # matches neither session's cwd (need not exist)
    result = run_cli(
        "full_text_search_chats_archive.py", "alpha", "--here", str(nowhere),
        config=env_path, cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "No results found." in result.stdout
    # Both local-CLI sources had a pre-filter match but none under `nowhere`.
    assert "source: claude-code" in result.stderr
    assert "source: codex" in result.stderr
    assert str(nowhere) in result.stderr
    assert "testhost (MACHINE_NAME)" in result.stderr


@pytest.mark.integration
def test_here_reinterprets_swallowed_query(two_source_workspace, run_cli):
    """`--here "alpha"` with no positional query: --here's nargs="?" binds "alpha"
    as its PATH, but "alpha" names no session directory, so it is reinterpreted as
    the query and --here is scoped to the cwd. The here session matches; the note
    explains the switch (and codex, recorded elsewhere, stays scoped out)."""
    env_path, here_dir = two_source_workspace
    result = run_cli(
        "full_text_search_chats_archive.py", "--here", "alpha",
        config=env_path, cwd=here_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "cc-here" in result.stdout
    assert "No results found." not in result.stdout
    assert 'searching for "alpha"' in result.stderr


@pytest.mark.integration
def test_here_trailing_slash_forces_path_no_reinterpret(two_source_workspace, run_cli):
    """A --here arg ending in "/" is an explicit directory reference: even though
    no session lives under `./newsub`, it is NOT reinterpreted as a query (no
    "searching for" note). It scopes as a path and falls through to the normal
    empty-result miss hint, which names the resolved directory."""
    env_path, here_dir = two_source_workspace
    result = run_cli(
        "full_text_search_chats_archive.py", "--here", "newsub/",
        config=env_path, cwd=here_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "No results found." in result.stdout
    assert "searching for" not in result.stderr        # heuristic suppressed
    assert str(here_dir / "newsub") in result.stderr   # scoped as a path
