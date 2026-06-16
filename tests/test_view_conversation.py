"""
Unit tests for view_conversation.py project rendering — projects carry docs
and a prompt template instead of chat_messages, so they have their own
renderers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.view import render as vc

PROJECT = {
    "uuid": "p-1",
    "name": "My Project",
    "created_at": "2026-01-02T09:00:00Z",
    "updated_at": "2026-01-03T09:00:00Z",
    "description": "A test project",
    "prompt_template": "Always answer in haiku.",
    "docs": [
        {"filename": "notes.md", "created_at": "2026-01-02T10:00:00Z",
         "content": "Some *markdown* content"},
        {"filename": "empty.txt"},
    ],
}


def test_project_to_markdown_renders_all_sections():
    md = vc.project_to_markdown(PROJECT)
    assert "# My Project" in md
    assert "**Description:** A test project" in md
    assert "## Prompt template" in md
    assert "Always answer in haiku." in md
    assert "## 📄 notes.md" in md
    assert "Some *markdown* content" in md
    assert "## 📄 empty.txt" in md


def test_project_to_markdown_minimal():
    md = vc.project_to_markdown({"uuid": "p", "name": "Bare",
                                 "created_at": "2026-01-02T09:00:00Z"})
    assert "# Bare" in md
    assert "Prompt template" not in md


def test_project_to_html_renders_docs():
    page = vc.project_to_html(PROJECT, "claude")
    assert "<title>My Project</title>" in page
    assert "A test project" in page
    assert "Prompt template" in page
    assert "notes.md" in page
    assert "<em>markdown</em>" in page  # doc content rendered as Markdown


# ---------------------------------------------------------------------------
# Claude Code session renderers (claude_code_to_markdown / claude_code_to_html).
#
# Pinned directly on the real fixture (not via subprocess) so the upcoming
# shared-transcript-renderer refactor can't silently change the metadata block,
# the resume command (the load-bearing string), the turn rendering, or the
# thinking-exclusion. Timestamps are rendered through format_timestamp(), which
# is machine-local-TZ dependent, so we never assert the visible date/time — only
# the raw ISO carried in the HTML <time datetime="..."> attribute.
# ---------------------------------------------------------------------------

CC_FIXTURE = Path(__file__).parent / "fixtures" / "sample_claude_code_session.jsonl"
CC_NAME = "How do I set up a Python virtual environment for testing?"
CC_RESUME = "cd /home/testuser/projects/my-app && claude -r cc-test-session-001"


def test_cc_markdown_metadata_block():
    md = vc.claude_code_to_markdown(CC_FIXTURE)
    assert f"# {CC_NAME}" in md
    assert "**Session:** `cc-test-session-001`" in md
    assert "**Directory:** `/home/testuser/projects/my-app`" in md
    assert "**Branch:** `main`" in md


def test_cc_markdown_resume_command():
    md = vc.claude_code_to_markdown(CC_FIXTURE)
    assert f"**Resume:** `{CC_RESUME}`" in md


def test_cc_markdown_turns_and_tools():
    md = vc.claude_code_to_markdown(CC_FIXTURE)
    assert "## User" in md
    assert "## Assistant" in md
    assert "Now how do I write a conftest.py" in md  # second user prompt
    assert "python -m venv .venv" in md  # assistant text
    assert "*Tools used: Bash*" in md


def test_cc_markdown_excludes_thinking():
    md = vc.claude_code_to_markdown(CC_FIXTURE)
    assert "Let me explain virtual environments" not in md


def test_cc_html_page_shell_and_source_label():
    page = vc.claude_code_to_html(CC_FIXTURE)
    assert f"<title>{CC_NAME}</title>" in page
    assert "Claude Code" in page  # _SOURCE_LABELS["claude-code"]


def test_cc_html_metadata_block():
    page = vc.claude_code_to_html(CC_FIXTURE)
    assert "<strong>Session:</strong> <code>cc-test-session-001</code>" in page
    assert "<strong>Directory:</strong> <code>/home/testuser/projects/my-app</code>" in page
    assert "<strong>Branch:</strong> <code>main</code>" in page
    # The resume command is HTML-escaped: `&&` becomes `&amp;&amp;`. Pinning the
    # escaped form is deliberate — dropping the escape would regress here.
    assert ("<strong>Resume:</strong> <code>cd /home/testuser/projects/my-app "
            "&amp;&amp; claude -r cc-test-session-001</code>") in page


def test_cc_html_turns_and_tool_use():
    page = vc.claude_code_to_html(CC_FIXTURE)
    assert page.count('message-row user') == 2  # two user prompts
    assert page.count('message-row assistant') == 2
    assert "python -m venv .venv" in page
    assert "🔧 Tools used: Bash" in page


def test_cc_html_excludes_thinking_and_pins_iso_timestamp():
    page = vc.claude_code_to_html(CC_FIXTURE)
    assert "Let me explain virtual environments" not in page
    # TZ-independent created-at pin (raw ISO in the <time> datetime attribute).
    assert 'datetime="2026-04-10T14:00:00.000Z"' in page


# ---------------------------------------------------------------------------
# OpenAI Codex session renderers (codex_to_markdown / codex_to_html), mirroring
# the Claude Code renderer tests above.
# ---------------------------------------------------------------------------

CX_FIXTURE = Path(__file__).parent / "fixtures" / "sample_codex_session.jsonl"
CX_SESSION = "b19ec125-978e-7f30-8b5b-61448a2fc5d7"
CX_RESUME = f"cd /tmp/codex-resume-test && codex resume {CX_SESSION}"
CX_TOOLS_FIXTURE = Path(__file__).parent / "fixtures" / "sample_codex_session_with_tools.jsonl"


def test_codex_markdown_metadata_block():
    md = vc.codex_to_markdown(CX_FIXTURE)
    assert f"**Session:** `{CX_SESSION}`" in md
    assert "**Directory:** `/tmp/codex-resume-test`" in md
    # codex records no git branch; the Branch line is omitted (empty git_branch)
    assert "**Branch:**" not in md


def test_codex_markdown_resume_command():
    md = vc.codex_to_markdown(CX_FIXTURE)
    assert f"**Resume:** `{CX_RESUME}`" in md


def test_codex_markdown_turns():
    md = vc.codex_to_markdown(CX_FIXTURE)
    assert "## User" in md
    assert "## Assistant" in md
    assert "PING" in md and "PONG" in md


def test_codex_markdown_tools_and_excludes_reasoning():
    md = vc.codex_to_markdown(CX_TOOLS_FIXTURE)
    assert "*Tools used:" in md
    assert "apply_patch" in md
    # encrypted reasoning blobs must never surface in the rendered transcript
    assert "encrypted" not in md.lower()


def test_codex_html_page_shell_and_source_label():
    page = vc.codex_to_html(CX_FIXTURE)
    assert "OpenAI Codex" in page  # _SOURCE_LABELS["codex"]
    assert ("<strong>Resume:</strong> <code>cd /tmp/codex-resume-test "
            f"&amp;&amp; codex resume {CX_SESSION}</code>") in page


def test_codex_html_turns():
    page = vc.codex_to_html(CX_FIXTURE)
    assert page.count('message-row user') == 2
    assert page.count('message-row assistant') == 2


def test_render_conversation_dispatches_codex():
    md = vc.render_conversation("codex", CX_FIXTURE, "markdown")
    assert f"**Session:** `{CX_SESSION}`" in md
    html = vc.render_conversation("codex", CX_FIXTURE, "html")
    assert "OpenAI Codex" in html
