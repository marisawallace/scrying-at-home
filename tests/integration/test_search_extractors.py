"""
Unit tests for text-extraction helpers in full_text_search_chats_archive.

These guard against the regression where ChatGPT conversations (which use a
`mapping` of nodes, not a `chat_messages` array) had no message text indexed.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scrying_at_home.search.engine import (  # noqa: E402
    extract_text_from_chatgpt_conversation,
    extract_text_from_conversation,
)


def _chatgpt_conv(mapping, **top):
    base = {"id": "x", "title": "T", "create_time": 0, "mapping": mapping}
    base.update(top)
    return base


def test_extract_chatgpt_pulls_message_parts():
    data = _chatgpt_conv(
        {
            "root": {"id": "root", "message": None, "parent": None, "children": ["a"]},
            "a": {
                "id": "a",
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["hello world"]},
                },
                "parent": "root",
                "children": ["b"],
            },
            "b": {
                "id": "b",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["goodbye moon"]},
                },
                "parent": "a",
                "children": [],
            },
        },
        title="conv title",
    )

    texts = extract_text_from_chatgpt_conversation(data)

    assert "conv title" in texts
    assert "hello world" in texts
    assert "goodbye moon" in texts


def test_extract_chatgpt_skips_null_message_root():
    data = _chatgpt_conv(
        {"root": {"id": "root", "message": None, "parent": None, "children": []}}
    )
    # Should not crash and should return at least the title; nothing from root.
    texts = extract_text_from_chatgpt_conversation(data)
    assert all(t for t in texts)  # no None / empty entries


def test_extract_chatgpt_handles_multimodal_parts():
    data = _chatgpt_conv(
        {
            "a": {
                "id": "a",
                "message": {
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [
                            "describe this image",
                            {"content_type": "image_asset_pointer", "asset_pointer": "file://x"},
                        ],
                    },
                },
                "parent": None,
                "children": [],
            }
        }
    )
    texts = extract_text_from_chatgpt_conversation(data)
    assert "describe this image" in texts
    # The dict part is silently dropped — no string entries should be dicts.
    assert all(isinstance(t, str) for t in texts)


def test_extract_chatgpt_tolerates_missing_mapping():
    data = {"id": "x", "title": "just a title"}
    texts = extract_text_from_chatgpt_conversation(data)
    assert texts == ["just a title"]


def test_extract_claude_unchanged():
    """Regression sanity: Claude extractor still walks chat_messages."""
    data = {
        "name": "claude conv",
        "summary": "summary text",
        "chat_messages": [
            {"text": "msg one text", "content": [{"text": "block one text"}]},
            {"text": "msg two text", "content": []},
        ],
    }
    texts = extract_text_from_conversation(data)
    assert "claude conv" in texts
    assert "summary text" in texts
    assert "msg one text" in texts
    assert "block one text" in texts
    assert "msg two text" in texts
