"""
Unit tests for view_conversation.py project rendering — projects carry docs
and a prompt template instead of chat_messages, so they have their own
renderers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import view_conversation as vc

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
