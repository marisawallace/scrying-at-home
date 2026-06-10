"""
Integration tests for the search index (search_index.py).

The index is a candidate filter: searching with it must produce stdout
byte-identical to a full scan (--no-index). These tests assert that
identity across query modes, then exercise staleness (modified, appended,
deleted, rewritten files) and corruption recovery.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(repo_root, workspace, script, *args):
    return subprocess.run(
        [sys.executable, str(repo_root / script), *args],
        cwd=workspace,
        capture_output=True,
        text=True,
    )


def _search(repo_root, workspace, *args):
    result = _run_cli(repo_root, workspace, "full_text_search_chats_archive.py", *args)
    assert result.returncode == 0, f"Search failed: {result.stderr}"
    return result.stdout


def _pop_scores(stdout):
    entries = json.loads(stdout)
    return [e.pop("total_score") for e in entries], entries


def _assert_index_matches_scan(repo_root, workspace, *args):
    """The core identity check: index-backed output == full-scan output.

    total_score embeds a recency boost computed from datetime.now(), so two
    runs differ in its far decimals; JSON output is compared with a score
    tolerance and everything else byte-exact.
    """
    with_index = _search(repo_root, workspace, *args)
    without_index = _search(repo_root, workspace, *args, "--no-index")
    if "-j" in args:
        index_scores, index_entries = _pop_scores(with_index)
        scan_scores, scan_entries = _pop_scores(without_index)
        assert index_entries == scan_entries, (
            f"index and scan results diverge for args {args!r}"
        )
        assert index_scores == pytest.approx(scan_scores, abs=1e-3), (
            f"index and scan scores diverge for args {args!r}"
        )
    else:
        assert with_index == without_index, (
            f"index and scan output diverge for args {args!r}"
        )
    return with_index


@pytest.fixture
def full_archive_workspace(isolated_workspace, sample_claude_export,
                           sample_chatgpt_export, repo_root):
    """Workspace with claude + chatgpt + claude-code data and an .env whose
    SEARCH_INDEX_DB points inside the workspace."""
    # Claude Code session data
    cc_dir = isolated_workspace / "claude_code_data"
    project_dir = cc_dir / "-home-testuser-projects-my-app"
    project_dir.mkdir(parents=True)
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_claude_code_session.jsonl"
    shutil.copy(fixture, project_dir / "cc-test-session-001.jsonl")

    # Subagent transcript: invisible to search, but its tool_use calls count
    # toward the --stats leaderboard (gather_cc_tool_counts rglobs them).
    subagent_dir = project_dir / "cc-test-session-001" / "subagents"
    subagent_dir.mkdir(parents=True)
    subagent_line = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Inspecting the flobnicator registry"},
            {"type": "tool_use", "name": "Bash", "id": "t1", "input": {}},
            {"type": "tool_use", "name": "Read", "id": "t2", "input": {}},
        ]},
        "uuid": "sa-msg-001", "timestamp": "2026-04-10T15:00:00.000Z",
        "sessionId": "agent-sub-001",
    }
    (subagent_dir / "agent-sub-001.jsonl").write_text(json.dumps(subagent_line) + "\n")

    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)
    repo_env.write_text(
        f"ZIP_SEARCH_DIR={isolated_workspace}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"ARCHIVED_EXPORTS_DIR={isolated_workspace / 'data' / 'archived_exports'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"CHATGPT_EMAIL=chatgpt-test@example.com\n"
    )

    # Sync both providers' export zips into the archive
    shutil.copy(sample_claude_export, isolated_workspace / sample_claude_export.name)
    shutil.copy(sample_chatgpt_export, isolated_workspace / sample_chatgpt_export.name)
    for flag in ("--claude", "--chatgpt"):
        sync = _run_cli(repo_root, isolated_workspace, "sync_local_chats_archive.py", flag)
        assert sync.returncode == 0, f"Setup sync {flag} failed: {sync.stderr}"

    yield isolated_workspace

    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Identity: index output == scan output
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_index_identical_to_scan_across_modes(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    # Multi-word AND query (claude data)
    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "Test Conversation 1" in out
    # Exact phrase
    out = _assert_index_matches_scan(repo_root, ws, "-e", "integration testing", "-j")
    assert "Integration Testing Discussion" in out
    # Cross-provider hit (chatgpt)
    out = _assert_index_matches_scan(repo_root, ws, "ChatGPT", "-j")
    assert "chatgpt-conv-uuid-001" in out
    # Claude Code source filter
    out = _assert_index_matches_scan(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")
    assert "cc-test-session-001" in out
    # All-short-words query: index can't serve it, both runs scan
    _assert_index_matches_scan(repo_root, ws, "a b", "-j")
    # Static list output (colors/highlighting path)
    _assert_index_matches_scan(repo_root, ws, "Python function", "-n")
    # Index db was actually created
    assert (ws / "search_index.db").exists()


@pytest.mark.integration
def test_browse_and_stats_identical_to_scan(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    out = _assert_index_matches_scan(repo_root, ws, "-j")  # browse mode
    entries = json.loads(out)
    # browse covers every item across all three providers
    assert {e["provider"] for e in entries} == {"claude", "chatgpt", "claude-code"}
    assert len(entries) == 5  # 2 claude convs + 1 project + 1 chatgpt + 1 cc

    out = _assert_index_matches_scan(repo_root, ws, "--stats")
    assert "claude-code" in out
    # Subagent transcript tool calls reach the leaderboard...
    assert "Bash" in out

    # ...but subagent transcript text never reaches search results
    out = _assert_index_matches_scan(repo_root, ws, "flobnicator", "-j")
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# Staleness: the index reconciles on every run
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_modified_json_is_reindexed(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    data = json.loads(conv_file.read_text())
    data["chat_messages"][0]["text"] = "Completely fresh xylophone content"
    conv_file.write_text(json.dumps(data, indent=2))

    out = _assert_index_matches_scan(repo_root, ws, "xylophone", "-j")
    assert "conv-uuid-001" in out
    # The replaced text is gone from the index too
    out = _search(repo_root, ws, "-e", "How do I write a Python function", "-j")
    assert "conv-uuid-001" not in out


@pytest.mark.integration
def test_appended_jsonl_line_is_found(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "virtual environment", "-j")  # build index

    session = ws / "claude_code_data/-home-testuser-projects-my-app/cc-test-session-001.jsonl"
    appended = {
        "type": "user",
        "message": {"role": "user", "content": "Now discuss the zanzibar deployment"},
        "uuid": "msg-appended", "timestamp": "2026-04-11T09:00:00.000Z",
        "cwd": "/home/testuser/projects/my-app",
        "sessionId": "cc-test-session-001", "gitBranch": "main",
    }
    with open(session, "a") as f:
        f.write(json.dumps(appended) + "\n")

    out = _assert_index_matches_scan(repo_root, ws, "zanzibar", "-j")
    assert "cc-test-session-001" in out

    # updated_at must track the appended (newer) timestamp in browse mode
    entries = json.loads(_assert_index_matches_scan(repo_root, ws, "-j", "-s", "claude-code"))
    assert entries[0]["updated_at"] == "2026-04-11T09:00:00.000Z"


@pytest.mark.integration
def test_deleted_file_drops_out_of_results(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    out = _search(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out

    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    next(conv_dir.glob("*Test-Conversation-1*")).unlink()

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" not in out


@pytest.mark.integration
def test_rewritten_jsonl_head_change_forces_full_reindex(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "virtual environment", "-j")  # build index

    # Rewrite the session from scratch with a different head and MORE bytes
    # than before, so only the head-hash check can tell it isn't an append.
    session = ws / "claude_code_data/-home-testuser-projects-my-app/cc-test-session-001.jsonl"
    original_size = session.stat().st_size
    lines = [
        {"type": "permission-mode", "permissionMode": "plan", "sessionId": "cc-test-session-001"},
        {"type": "user",
         "message": {"role": "user", "content": "Rebuilt transcript about quasar telemetry"},
         "uuid": "msg-new-001", "timestamp": "2026-05-01T10:00:00.000Z",
         "cwd": "/home/testuser/projects/my-app",
         "sessionId": "cc-test-session-001", "gitBranch": "main",
         "padding": "x" * max(0, original_size)},
    ]
    session.write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert session.stat().st_size > original_size

    out = _assert_index_matches_scan(repo_root, ws, "quasar telemetry", "-j")
    assert "cc-test-session-001" in out
    # Old content must be gone even though the file only ever grew
    out = _assert_index_matches_scan(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")
    assert "cc-test-session-001" not in out


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_corrupt_index_recovers_with_correct_results(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    db = ws / "search_index.db"
    db.write_text("garbage " * 1000)

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out


@pytest.mark.integration
def test_view_uuid_lookup_with_and_without_index(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    result = _run_cli(repo_root, ws, "view_conversation.py", "conv-uuid-001", "--no-open")
    assert result.returncode == 0, result.stderr
    assert "Found:" in result.stdout

    # Without an index the scan fallback must still find it
    (ws / "search_index.db").unlink()
    result = _run_cli(repo_root, ws, "view_conversation.py", "conv-uuid-001", "--no-open")
    assert result.returncode == 0, result.stderr
    assert "Found:" in result.stdout
