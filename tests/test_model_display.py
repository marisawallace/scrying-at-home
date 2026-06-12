"""
Unit tests for model extraction and display in full_text_search_chats_archive.py:
the per-conversation model id shown in the search/browse metadata line.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import full_text_search_chats_archive as fts


class TestPrettifyModel:
    def test_claude_family(self):
        assert fts.prettify_model("claude-opus-4-8") == "Opus 4.8"
        assert fts.prettify_model("claude-sonnet-4-6-20250929") == "Sonnet 4.6"
        assert fts.prettify_model("claude-haiku-4-5") == "Haiku 4.5"

    def test_gpt_prefix(self):
        assert fts.prettify_model("gpt-4o") == "GPT-4o"
        assert fts.prettify_model("gpt-5") == "GPT-5"

    def test_unknown_passes_through(self):
        assert fts.prettify_model("o3-mini") == "o3-mini"

    def test_empty(self):
        assert fts.prettify_model("") == ""


class TestExtractModelFromChatgpt:
    def _node(self, role, slug):
        return {"message": {"author": {"role": role},
                            "metadata": {"model_slug": slug} if slug else {}}}

    def test_most_common_assistant_slug(self):
        data = {"mapping": {
            "a": self._node("assistant", "gpt-4o"),
            "b": self._node("assistant", "gpt-4o"),
            "c": self._node("assistant", "gpt-5"),
            "d": self._node("user", None),
        }}
        assert fts.extract_model_from_chatgpt_conversation(data) == "gpt-4o"

    def test_no_slug(self):
        data = {"mapping": {"a": self._node("assistant", None)}}
        assert fts.extract_model_from_chatgpt_conversation(data) == ""

    def test_no_mapping(self):
        assert fts.extract_model_from_chatgpt_conversation({}) == ""


class TestExtractLlmModel:
    def test_projects_have_no_model(self):
        data = {"mapping": {"a": {"message": {"author": {"role": "assistant"},
                                              "metadata": {"model_slug": "gpt-4o"}}}}}
        assert fts.extract_llm_model(data, "project", "chatgpt") == ""

    def test_claude_conversation_without_model(self):
        data = {"chat_messages": [{"sender": "assistant", "text": "hi"}]}
        assert fts.extract_llm_model(data, "conversation", "claude") == ""

    def test_claude_conversation_with_model(self):
        data = {"chat_messages": [{"sender": "assistant", "model": "claude-opus-4-8"}]}
        assert fts.extract_llm_model(data, "conversation", "claude") == "claude-opus-4-8"
