"""
Unit tests for claude_code_parser.py.
"""
import json
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.parsers import claude_code as ccp


@pytest.fixture
def sample_jsonl_path():
    """Path to the sample Claude Code JSONL fixture."""
    return Path(__file__).parent / "fixtures" / "sample_claude_code_session.jsonl"


@pytest.fixture
def sample_lines(sample_jsonl_path):
    """Parsed lines from the sample JSONL fixture."""
    return ccp.parse_jsonl(sample_jsonl_path)


class TestParseJsonl:
    def test_parses_all_lines(self, sample_jsonl_path):
        lines = ccp.parse_jsonl(sample_jsonl_path)
        assert len(lines) == 9

    def test_each_line_is_dict(self, sample_lines):
        for line in sample_lines:
            assert isinstance(line, dict)

    def test_skips_malformed_lines(self, tmp_path):
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"type":"user"}\nNOT JSON\n{"type":"assistant"}\n')
        lines = ccp.parse_jsonl(bad_file)
        assert len(lines) == 2

    def test_skips_empty_lines(self, tmp_path):
        f = tmp_path / "empty_lines.jsonl"
        f.write_text('{"type":"user"}\n\n\n{"type":"assistant"}\n')
        lines = ccp.parse_jsonl(f)
        assert len(lines) == 2


class TestExtractSessionMetadata:
    def test_session_id(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert meta["session_id"] == "cc-test-session-001"

    def test_cwd(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert meta["cwd"] == "/home/testuser/projects/my-app"

    def test_git_branch(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert meta["git_branch"] == "main"

    def test_created_at(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert meta["created_at"] == "2026-04-10T14:00:00.000Z"

    def test_updated_at(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert meta["updated_at"] == "2026-04-10T14:01:05.000Z"

    def test_name_from_first_prompt(self, sample_lines):
        meta = ccp.extract_session_metadata(sample_lines)
        assert "virtual environment" in meta["name"]


class TestExtractSearchableText:
    def test_includes_user_prompts(self, sample_lines):
        texts = ccp.extract_searchable_text(sample_lines)
        assert any("virtual environment" in t for t in texts)
        assert any("conftest.py" in t for t in texts)

    def test_includes_assistant_text(self, sample_lines):
        texts = ccp.extract_searchable_text(sample_lines)
        assert any("python -m venv" in t for t in texts)

    def test_excludes_thinking(self, sample_lines):
        texts = ccp.extract_searchable_text(sample_lines)
        assert not any("Let me explain virtual environments" in t for t in texts)

    def test_excludes_tool_results(self, sample_lines):
        texts = ccp.extract_searchable_text(sample_lines)
        # tool_result user lines should not appear
        assert not any("tool_result" in t for t in texts)

    def test_returns_list_of_strings(self, sample_lines):
        texts = ccp.extract_searchable_text(sample_lines)
        assert isinstance(texts, list)
        for t in texts:
            assert isinstance(t, str)


class TestExtractConversationTurns:
    def test_has_correct_number_of_turns(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        # 2 user prompts + their assistant responses = should have user/assistant pairs
        user_turns = [t for t in turns if t["role"] == "user"]
        assert len(user_turns) == 2

    def test_user_turns_have_content(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        user_turns = [t for t in turns if t["role"] == "user"]
        assert "virtual environment" in user_turns[0]["content"]
        assert "conftest.py" in user_turns[1]["content"]

    def test_assistant_turns_merge_consecutive(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        # First assistant turn should merge: thinking(skipped) + text + tool_use
        # Then after tool_result (skipped), another text block
        assistant_turns = [t for t in turns if t["role"] == "assistant"]
        assert len(assistant_turns) >= 1
        # First assistant turn should have text from the text block
        first_asst = assistant_turns[0]
        assert "python -m venv" in first_asst["content"]

    def test_tool_uses_collected(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        assistant_turns = [t for t in turns if t["role"] == "assistant"]
        # One of the assistant turns should have Bash in tool_uses
        all_tools = []
        for t in assistant_turns:
            all_tools.extend(t["tool_uses"])
        assert "Bash" in all_tools

    def test_thinking_excluded_from_content(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        for turn in turns:
            assert "Let me explain virtual environments" not in turn["content"]

    def test_turns_have_timestamps(self, sample_lines):
        turns = ccp.extract_conversation_turns(sample_lines)
        for turn in turns:
            assert turn["timestamp"], f"Turn missing timestamp: {turn}"


class TestFindSessionFile:
    def test_finds_existing_session(self, tmp_path):
        # Create directory structure
        project_dir = tmp_path / "-home-user-project"
        project_dir.mkdir()
        session_file = project_dir / "abc-123.jsonl"
        session_file.write_text('{"type":"permission-mode"}\n')

        result = ccp.find_session_file(tmp_path, "abc-123")
        assert result == session_file

    def test_returns_none_for_missing(self, tmp_path):
        project_dir = tmp_path / "-home-user-project"
        project_dir.mkdir()
        result = ccp.find_session_file(tmp_path, "nonexistent")
        assert result is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        result = ccp.find_session_file(tmp_path / "nope", "anything")
        assert result is None

    def test_searches_across_project_dirs(self, tmp_path):
        # Create multiple project dirs, session in the second
        (tmp_path / "project-a").mkdir()
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        session_file = project_b / "target-session.jsonl"
        session_file.write_text('{"type":"permission-mode"}\n')

        result = ccp.find_session_file(tmp_path, "target-session")
        assert result == session_file


class TestDeriveConversationName:
    def test_truncates_long_names(self):
        lines = [
            {"type": "user", "message": {"content": "x" * 200}, "timestamp": "t"}
        ]
        name = ccp.derive_conversation_name(lines, max_length=80)
        assert len(name) <= 80
        assert name.endswith("\u2026")

    def test_short_prompt_not_truncated(self):
        lines = [
            {"type": "user", "message": {"content": "Hello world"}, "timestamp": "t"}
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "Hello world"

    def test_untitled_when_no_prompts(self):
        lines = [{"type": "permission-mode"}]
        name = ccp.derive_conversation_name(lines)
        assert name == "(untitled)"

    def test_uses_first_line_only(self):
        lines = [
            {"type": "user", "message": {"content": "First line\nSecond line\nThird"}, "timestamp": "t"}
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "First line"
        assert "Second" not in name

    def test_skips_slash_command_boilerplate(self):
        lines = [
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "<local-command-caveat>Caveat: The messages below were generated by the user while running local commands.</local-command-caveat>"},
                "timestamp": "t1",
            },
            {
                "type": "user",
                "message": {"content": "<command-name>/usage</command-name>\n            <command-message>usage</command-message>\n            <command-args></command-args>"},
                "timestamp": "t2",
            },
            {"type": "user", "message": {"content": "Real prompt here"}, "timestamp": "t3"},
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "Real prompt here"

    def test_skips_meta_lines(self):
        lines = [
            {"type": "user", "isMeta": True, "message": {"content": "injected context"}, "timestamp": "t1"},
            {"type": "user", "message": {"content": "Actual prompt"}, "timestamp": "t2"},
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "Actual prompt"

    def test_command_only_session_named_after_command(self):
        lines = [
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "<local-command-caveat>Caveat: ...</local-command-caveat>"},
                "timestamp": "t1",
            },
            {
                "type": "user",
                "message": {"content": "<command-name>/loop</command-name>\n            <command-message>loop</command-message>\n            <command-args>5m /foo</command-args>"},
                "timestamp": "t2",
            },
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "/loop 5m /foo"

    def test_system_command_only_session_named_after_command(self):
        lines = [
            {
                "type": "system",
                "subtype": "local_command",
                "content": "<command-name>/usage</command-name>\n            <command-message>usage</command-message>\n            <command-args></command-args>",
                "timestamp": "t1",
            },
        ]
        name = ccp.derive_conversation_name(lines)
        assert name == "/usage"


class TestCountToolUses:
    def test_counts_each_invocation(self):
        lines = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "hi"},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read"},
            ]}},
        ]
        counts = ccp.count_tool_uses(lines)
        assert counts["Bash"] == 2  # not de-duped within a turn
        assert counts["Read"] == 1

    def test_ignores_non_assistant_and_non_tool_blocks(self):
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "text": "..."},
                {"type": "text", "text": "ok"},
            ]}},
        ]
        assert ccp.count_tool_uses(lines) == {}

    def test_empty(self):
        assert ccp.count_tool_uses([]) == {}


class TestExtractModel:
    def test_most_common_assistant_model(self):
        lines = [
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
            {"type": "assistant", "message": {"model": "claude-sonnet-4-6"}},
        ]
        assert ccp.extract_model(lines) == "claude-opus-4-8"

    def test_ignores_synthetic_placeholder(self):
        lines = [
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
        ]
        assert ccp.extract_model(lines) == "claude-opus-4-8"

    def test_ignores_user_lines(self):
        lines = [{"type": "user", "message": {"content": "hi", "model": "x"}}]
        assert ccp.extract_model(lines) == ""

    def test_empty(self):
        assert ccp.extract_model([]) == ""
        assert ccp.extract_model(
            [{"type": "assistant", "message": {"model": "<synthetic>"}}]
        ) == ""
