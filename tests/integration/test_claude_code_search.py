"""
Integration tests for Claude Code search functionality.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def claude_code_workspace(isolated_workspace, repo_root):
    """Set up a workspace with Claude Code JSONL data and swap .env."""
    # Create Claude Code data directory
    cc_dir = isolated_workspace / "claude_code_data"
    project_dir = cc_dir / "-home-testuser-projects-my-app"
    project_dir.mkdir(parents=True)

    # Copy sample JSONL fixture
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_claude_code_session.jsonl"
    shutil.copy(fixture, project_dir / "cc-test-session-001.jsonl")

    # Temporarily replace repo .env
    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)

    env_content = (
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
    )
    repo_env.write_text(env_content)

    yield isolated_workspace

    # Restore original .env
    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


@pytest.mark.integration
def test_search_claude_code_only(claude_code_workspace, repo_root):
    """Search with --source claude-code returns results from JSONL archives."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment", "-s", "claude-code"],
        capture_output=True,
        text=True
    )

    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    assert result.returncode == 0
    assert "Found" in result.stdout
    assert "cc-test-session-001" in result.stdout


@pytest.mark.integration
def test_search_claude_code_json_output(claude_code_workspace, repo_root):
    """Search with JSON output includes provider and extra fields."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment", "-s", "claude-code", "-j"],
        capture_output=True,
        text=True
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
def test_search_claude_code_resume_command(claude_code_workspace, repo_root):
    """Search results show resume command for Claude Code sessions."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment", "-s", "claude-code"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0
    assert "cd /home/testuser/projects/my-app && claude -r cc-test-session-001" in result.stdout
    assert "testhost" in result.stdout


@pytest.mark.integration
def test_search_source_all(claude_code_workspace, repo_root):
    """Search with --source all (default) includes Claude Code results."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0
    assert "cc-test-session-001" in result.stdout


@pytest.mark.integration
def test_search_source_llm_excludes_claude_code(claude_code_workspace, repo_root):
    """Search with --source llm does not include Claude Code results."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment", "-s", "llm"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0
    assert "cc-test-session-001" not in result.stdout


@pytest.mark.integration
def test_search_no_claude_code_dir_configured(isolated_workspace, repo_root):
    """Search with --source claude-code fails gracefully when sources not configured."""
    # Temporarily replace repo .env with one missing CLAUDE_CODE_SOURCES
    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)

    repo_env.write_text(f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n")

    try:
        result = subprocess.run(
            [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
             "test", "-s", "claude-code"],
            capture_output=True,
            text=True
        )

        assert result.returncode != 0
        assert "not configured" in result.stderr
    finally:
        if backup_env.exists():
            shutil.move(backup_env, repo_env)
        else:
            repo_env.unlink(missing_ok=True)


@pytest.fixture
def multi_host_claude_code_workspace(isolated_workspace, repo_root):
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

    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)

    env_content = (
        f"CLAUDE_CODE_SOURCES=laptop={laptop_dir},desktop={desktop_dir}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
    )
    repo_env.write_text(env_content)

    yield isolated_workspace

    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


@pytest.mark.integration
def test_search_claude_code_multi_source(multi_host_claude_code_workspace, repo_root):
    """Both host sources surface in results, each tagged with its hostname."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "full_text_search_chats_archive.py"),
         "virtual environment", "-s", "claude-code", "-j"],
        capture_output=True,
        text=True
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
