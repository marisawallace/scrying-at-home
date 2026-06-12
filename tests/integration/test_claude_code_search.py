"""
Integration tests for Claude Code search functionality.
"""
import json
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def claude_code_workspace(isolated_workspace):
    """Set up a workspace with Claude Code JSONL data and a workspace-local .env."""
    # Create Claude Code data directory
    cc_dir = isolated_workspace / "claude_code_data"
    project_dir = cc_dir / "-home-testuser-projects-my-app"
    project_dir.mkdir(parents=True)

    # Copy sample JSONL fixture
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_claude_code_session.jsonl"
    shutil.copy(fixture, project_dir / "cc-test-session-001.jsonl")

    env_content = (
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
    )
    (isolated_workspace / ".env").write_text(env_content)

    return isolated_workspace


@pytest.mark.integration
def test_search_claude_code_only(claude_code_workspace, run_cli):
    """Search with --source claude-code returns results from JSONL archives."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment", "-s", "claude-code",
        config=claude_code_workspace / ".env",
    )

    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    assert result.returncode == 0
    assert "Found" in result.stdout
    assert "cc-test-session-001" in result.stdout


@pytest.mark.integration
def test_search_claude_code_json_output(claude_code_workspace, run_cli):
    """Search with JSON output includes provider and extra fields."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment", "-s", "claude-code", "-j",
        config=claude_code_workspace / ".env",
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert len(data) > 0

    entry = data[0]
    assert entry["provider"] == "claude-code"
    assert "cwd" in entry.get("extra", {})
    assert entry["extra"].get("host") == "testhost"
    assert "claude -r" in entry["url"]
    assert "[testhost]" in entry["url"]


@pytest.mark.integration
def test_search_claude_code_resume_command(claude_code_workspace, run_cli):
    """Search results show resume command for Claude Code sessions."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment", "-s", "claude-code",
        config=claude_code_workspace / ".env",
    )

    assert result.returncode == 0
    assert "pushd /home/testuser/projects/my-app && claude -r cc-test-session-001" in result.stdout
    assert "testhost" in result.stdout


@pytest.mark.integration
def test_search_source_all(claude_code_workspace, run_cli):
    """Search with --source all (default) includes Claude Code results."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment",
        config=claude_code_workspace / ".env",
    )

    assert result.returncode == 0
    assert "cc-test-session-001" in result.stdout


@pytest.mark.integration
def test_search_source_llm_excludes_claude_code(claude_code_workspace, run_cli):
    """Search with --source llm does not include Claude Code results."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment", "-s", "llm",
        config=claude_code_workspace / ".env",
    )

    assert result.returncode == 0
    assert "cc-test-session-001" not in result.stdout


@pytest.mark.integration
def test_search_no_claude_code_dir_configured(isolated_workspace, run_cli):
    """Search with --source claude-code fails gracefully when sources not configured."""
    # A workspace-local .env missing CLAUDE_CODE_SOURCES
    env_path = isolated_workspace / ".env"
    env_path.write_text(f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n")

    result = run_cli(
        "full_text_search_chats_archive.py", "test", "-s", "claude-code",
        config=env_path,
    )

    assert result.returncode != 0
    assert "not configured" in result.stderr


@pytest.fixture
def multi_host_claude_code_workspace(isolated_workspace):
    """Set up a workspace with two host-labeled Claude Code source dirs.

    Both hosts have a session at the same cwd (project slug) — exercises
    the disambiguation guarantee that hostname comes from the synced root,
    not from JSONL contents.
    """
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_claude_code_session.jsonl"

    # Two host roots, each with a JSONL under the same project slug
    laptop_dir = isolated_workspace / "cc_sync" / "laptop"
    desktop_dir = isolated_workspace / "cc_sync" / "desktop"
    project_slug = "-home-testuser-projects-my-app"

    (laptop_dir / project_slug).mkdir(parents=True)
    (desktop_dir / project_slug).mkdir(parents=True)
    shutil.copy(fixture, laptop_dir / project_slug / "cc-laptop-session.jsonl")
    shutil.copy(fixture, desktop_dir / project_slug / "cc-desktop-session.jsonl")

    env_content = (
        f"CLAUDE_CODE_SOURCES=laptop={laptop_dir},desktop={desktop_dir}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
    )
    (isolated_workspace / ".env").write_text(env_content)

    return isolated_workspace


def _write_cc_session(path: Path, session_id: str, cwd: str, timestamp: str, text: str):
    """Write a minimal two-line Claude Code JSONL session."""
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


@pytest.mark.integration
def test_search_here_no_query_newest_first(isolated_workspace, run_cli, tmp_path):
    """--here with no query lists this dir's sessions, newest first."""
    cc_dir = isolated_workspace / "claude_code_data"
    # Run from a real directory so session cwd == current cwd passes the filter.
    run_dir = tmp_path / "workdir"
    run_dir.mkdir()
    project_dir = cc_dir / "-tmp-workdir"
    project_dir.mkdir(parents=True)

    _write_cc_session(project_dir / "old.jsonl", "cc-old", str(run_dir),
                      "2026-01-01T10:00:00.000Z", "older session about widgets")
    _write_cc_session(project_dir / "new.jsonl", "cc-new", str(run_dir),
                      "2026-05-01T10:00:00.000Z", "newer session about gadgets")

    env_path = isolated_workspace / ".env"
    env_path.write_text(
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"CLAUDE_CODE_HOST=testhost\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
    )

    result = run_cli(
        "full_text_search_chats_archive.py", "--here", "-j",
        config=env_path, cwd=run_dir,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    uuids = [entry["uuid"] for entry in data]
    assert uuids == ["cc-new", "cc-old"], "Browse mode should list newest first"
    assert all(entry["match_count"] == 1 for entry in data)
    assert all(entry["matches"][0]["score"] == 0.0 for entry in data)


@pytest.mark.integration
def test_search_claude_code_multi_source(multi_host_claude_code_workspace, run_cli):
    """Both host sources surface in results, each tagged with its hostname."""
    result = run_cli(
        "full_text_search_chats_archive.py", "virtual environment", "-s", "claude-code", "-j",
        config=multi_host_claude_code_workspace / ".env",
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    # Each host's JSONL is a copy of the same fixture (same sessionId baked
    # in), so we expect two distinct results disambiguated by host even
    # though their cwd and uuid match — that's the whole point of this test.
    assert len(data) == 2

    hosts = sorted(entry["extra"]["host"] for entry in data)
    assert hosts == ["desktop", "laptop"]

    by_host = {entry["extra"]["host"]: entry for entry in data}
    assert "[laptop]" in by_host["laptop"]["url"]
    assert "[desktop]" in by_host["desktop"]["url"]
    # Filepaths must come from each host's own synced root
    assert "/cc_sync/laptop/" in by_host["laptop"]["filepath"]
    assert "/cc_sync/desktop/" in by_host["desktop"]["filepath"]
