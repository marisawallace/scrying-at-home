"""
Integration tests for Claude Code view functionality.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def claude_code_workspace(isolated_workspace, repo_root):
    """Set up a workspace with Claude Code JSONL data and swap .env."""
    cc_dir = isolated_workspace / "claude_code_data"
    project_dir = cc_dir / "-home-testuser-projects-my-app"
    project_dir.mkdir(parents=True)

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
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
    )
    repo_env.write_text(env_content)

    yield isolated_workspace

    # Restore original .env
    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


@pytest.mark.integration
def test_view_claude_code_session(claude_code_workspace, repo_root):
    """View a Claude Code session by session ID generates markdown."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "view_conversation.py"),
         "cc-test-session-001", "--no-open"],
        capture_output=True,
        text=True
    )

    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    assert result.returncode == 0
    assert "Found:" in result.stdout
    assert "Created:" in result.stdout


@pytest.mark.integration
def test_view_output_path(claude_code_workspace, repo_root):
    """View output goes to local_views/claude-code/ directory."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "view_conversation.py"),
         "cc-test-session-001", "--no-open"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0

    # Check the markdown file was created in the right place
    md_path = claude_code_workspace / "data" / "local_views" / "claude-code" / "cc-test-session-001.md"
    assert md_path.exists(), f"Expected markdown at {md_path}"

    content = md_path.read_text()
    assert "cc-test-session-001" in content
    assert "virtual environment" in content.lower()


@pytest.mark.integration
def test_view_markdown_content(claude_code_workspace, repo_root):
    """Generated markdown includes expected sections."""
    subprocess.run(
        [sys.executable, str(repo_root / "view_conversation.py"),
         "cc-test-session-001", "--no-open"],
        capture_output=True,
        text=True
    )

    md_path = claude_code_workspace / "data" / "local_views" / "claude-code" / "cc-test-session-001.md"
    content = md_path.read_text()

    # Check header metadata
    assert "Session:" in content
    assert "Directory:" in content
    assert "Resume:" in content
    assert "claude -r" in content

    # Check conversation turns are present
    assert "## User" in content
    assert "## Assistant" in content

    # Check tool usage summary
    assert "Tools used:" in content
    assert "Bash" in content

    # Check thinking blocks are NOT present
    assert "Let me explain virtual environments" not in content


@pytest.mark.integration
def test_view_not_found(claude_code_workspace, repo_root):
    """View with unknown session ID fails gracefully."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "view_conversation.py"),
         "nonexistent-session-id", "--no-open"],
        capture_output=True,
        text=True
    )

    assert result.returncode != 0
    assert "not found" in result.stderr.lower()
