"""
Integration tests for OpenAI Codex view functionality.

Mirrors test_claude_code_view.py: drives view_conversation.py as a subprocess,
resolving a codex session by id via codex_parser.find_session_file across
CODEX_SOURCES and rendering it to markdown.
"""
import shutil
from pathlib import Path

import pytest

SESSION_ID = "b19ec125-978e-7f30-8b5b-61448a2fc5d7"
ROLLOUT_NAME = f"rollout-2026-06-13T15-48-28-{SESSION_ID}.jsonl"


@pytest.fixture
def codex_workspace(isolated_workspace):
    codex_dir = isolated_workspace / "codex_data"
    day = codex_dir / "2026" / "06" / "13"
    day.mkdir(parents=True)
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_codex_session.jsonl"
    shutil.copy(fixture, day / ROLLOUT_NAME)

    (isolated_workspace / ".env").write_text(
        f"CODEX_SOURCES=testhost={codex_dir}\n"
        f"MACHINE_NAME=testhost\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
    )
    return isolated_workspace


@pytest.mark.integration
def test_view_codex_session(codex_workspace, run_cli):
    result = run_cli(
        "view_conversation.py", SESSION_ID, "--no-open",
        config=codex_workspace / ".env",
    )
    assert result.returncode == 0, result.stderr
    assert "Found" in result.stdout


@pytest.mark.integration
def test_view_output_path(codex_workspace, run_cli):
    result = run_cli(
        "view_conversation.py", SESSION_ID, "--no-open",
        config=codex_workspace / ".env",
    )
    assert result.returncode == 0, result.stderr
    md_path = (codex_workspace / "data" / "local_views" / "codex" / f"{SESSION_ID}.md")
    assert md_path.exists(), f"Expected markdown at {md_path}"
    content = md_path.read_text()
    assert SESSION_ID in content
    assert "PING" in content


@pytest.mark.integration
def test_view_markdown_content(codex_workspace, run_cli):
    run_cli(
        "view_conversation.py", SESSION_ID, "--no-open",
        config=codex_workspace / ".env",
    )
    md_path = (codex_workspace / "data" / "local_views" / "codex" / f"{SESSION_ID}.md")
    content = md_path.read_text()

    assert "Session:" in content
    assert "Directory:" in content
    assert "Resume:" in content
    assert "codex resume" in content
    assert "## User" in content
    assert "## Assistant" in content


@pytest.mark.integration
def test_view_html_format(codex_workspace, run_cli):
    result = run_cli(
        "view_conversation.py", SESSION_ID, "--no-open", "--format", "html",
        config=codex_workspace / ".env",
    )
    assert result.returncode == 0, result.stderr
    html_path = (codex_workspace / "data" / "local_views" / "codex" / f"{SESSION_ID}.html")
    assert html_path.exists()
    assert "OpenAI Codex" in html_path.read_text()


@pytest.mark.integration
def test_view_not_found(codex_workspace, run_cli):
    result = run_cli(
        "view_conversation.py", "nonexistent-session-id", "--no-open",
        config=codex_workspace / ".env",
    )
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()
