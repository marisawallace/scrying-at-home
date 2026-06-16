"""
Unit tests for codex_parser.py.

These are the real correctness net for the Codex functional core: the search
suite's --verify / _assert_index_matches_scan checks are DIFFERENTIAL (index
path vs scan path) and both paths share codex_parser, so a parser bug moves both
identically and parity stays green. So pin behavior here, against the real
fixtures.
"""
import sys
from collections import Counter
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.parsers import codex as cxp


@pytest.fixture
def sample_jsonl_path():
    """Path to the minimal two-turn (no-tools) Codex rollout fixture."""
    return Path(__file__).parent / "fixtures" / "sample_codex_session.jsonl"


@pytest.fixture
def sample_lines(sample_jsonl_path):
    return cxp.parse_jsonl(sample_jsonl_path)


@pytest.fixture
def tools_jsonl_path():
    """Path to the rich (tools/reasoning/turn_aborted) Codex rollout fixture."""
    return Path(__file__).parent / "fixtures" / "sample_codex_session_with_tools.jsonl"


@pytest.fixture
def tools_lines(tools_jsonl_path):
    return cxp.parse_jsonl(tools_jsonl_path)


class TestParseJsonl:
    def test_parses_all_lines(self, sample_jsonl_path):
        lines = cxp.parse_jsonl(sample_jsonl_path)
        assert len(lines) == 19

    def test_each_line_is_dict(self, sample_lines):
        for line in sample_lines:
            assert isinstance(line, dict)

    def test_skips_malformed_lines(self, tmp_path):
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"type":"session_meta"}\nNOT JSON\n{"type":"event_msg"}\n')
        assert len(cxp.parse_jsonl(bad_file)) == 2

    def test_skips_empty_lines(self, tmp_path):
        f = tmp_path / "empty_lines.jsonl"
        f.write_text('{"type":"session_meta"}\n\n\n{"type":"event_msg"}\n')
        assert len(cxp.parse_jsonl(f)) == 2


class TestExtractSessionMetadata:
    def test_session_id(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["session_id"] == "b19ec125-978e-7f30-8b5b-61448a2fc5d7"

    def test_cwd(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["cwd"] == "/tmp/codex-resume-test"

    def test_git_branch_always_empty(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["git_branch"] == ""

    def test_created_at_from_session_meta_payload(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["created_at"] == "2026-06-13T15:48:28.687Z"

    def test_updated_at_from_last_timestamped_line(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["updated_at"] == "2026-06-13T15:49:20.649Z"

    def test_name_from_first_user_message(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["name"].startswith("Reply with exactly the word PING")

    def test_model_from_turn_context(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert meta["model"] == "gpt-5.5"

    def test_keys_match_cc_contract(self, sample_lines):
        meta = cxp.extract_session_metadata(sample_lines)
        assert set(meta) == {
            "session_id", "cwd", "git_branch",
            "created_at", "updated_at", "name", "model",
        }

    def test_empty_lines_degrade_gracefully(self):
        meta = cxp.extract_session_metadata([])
        assert meta["session_id"] == ""
        assert meta["cwd"] == ""
        assert meta["model"] == ""
        assert meta["name"] == "(untitled)"


class TestExtractSearchableText:
    def test_includes_user_prompts(self, sample_lines):
        texts = cxp.extract_searchable_text(sample_lines)
        assert any("PING" in t for t in texts)
        assert any("PONG" in t for t in texts)

    def test_includes_assistant_messages(self, sample_lines):
        texts = cxp.extract_searchable_text(sample_lines)
        # The clean agent_message replies "PING" / "PONG" appear as their own texts
        assert texts.count("PING") == 1
        assert texts.count("PONG") == 1

    def test_excludes_response_item_developer_boilerplate(self, sample_lines):
        texts = cxp.extract_searchable_text(sample_lines)
        # The wrapped response_item duplicates (developer/permissions, env context)
        # must never leak into search text.
        assert not any("environment_context" in t for t in texts)
        assert not any("permissions instructions" in t for t in texts)
        assert not any("skills_instructions" in t for t in texts)

    def test_excludes_encrypted_reasoning(self, tools_lines):
        texts = cxp.extract_searchable_text(tools_lines)
        assert not any("encrypted" in t.lower() for t in texts)

    def test_excludes_tool_call_noise(self, tools_lines):
        texts = cxp.extract_searchable_text(tools_lines)
        assert not any("Begin Patch" in t for t in texts)
        assert not any("Process exited with code" in t for t in texts)

    def test_returns_list_of_strings(self, sample_lines):
        texts = cxp.extract_searchable_text(sample_lines)
        assert isinstance(texts, list)
        assert all(isinstance(t, str) for t in texts)


class TestCountToolUses:
    def test_counts_function_and_custom_tool_calls(self, tools_lines):
        counts = cxp.count_tool_uses(tools_lines)
        assert counts["exec_command"] == 40
        assert counts["apply_patch"] == 14

    def test_does_not_count_outputs(self, tools_lines):
        # 40 function_call + 40 function_call_output, but only the calls count.
        counts = cxp.count_tool_uses(tools_lines)
        assert sum(counts.values()) == 54

    def test_no_tools_session_is_empty(self, sample_lines):
        assert cxp.count_tool_uses(sample_lines) == Counter()

    def test_empty(self):
        assert cxp.count_tool_uses([]) == Counter()


class TestExtractConversationTurns:
    def test_user_turn_count(self, sample_lines):
        turns = cxp.extract_conversation_turns(sample_lines)
        user_turns = [t for t in turns if t["role"] == "user"]
        assert len(user_turns) == 2

    def test_user_turns_have_content(self, sample_lines):
        turns = cxp.extract_conversation_turns(sample_lines)
        user_turns = [t for t in turns if t["role"] == "user"]
        assert "PING" in user_turns[0]["content"]
        assert "PONG" in user_turns[1]["content"]

    def test_assistant_turns_have_content(self, sample_lines):
        turns = cxp.extract_conversation_turns(sample_lines)
        assistant_turns = [t for t in turns if t["role"] == "assistant"]
        assert [t["content"] for t in assistant_turns] == ["PING", "PONG"]

    def test_turns_alternate_user_assistant(self, sample_lines):
        turns = cxp.extract_conversation_turns(sample_lines)
        assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]

    def test_turns_have_timestamps(self, sample_lines):
        turns = cxp.extract_conversation_turns(sample_lines)
        for turn in turns:
            assert turn["timestamp"], f"Turn missing timestamp: {turn}"

    def test_tool_names_attached_to_assistant(self, tools_lines):
        turns = cxp.extract_conversation_turns(tools_lines)
        all_tools = [tool for t in turns if t["role"] == "assistant" for tool in t["tool_uses"]]
        assert "exec_command" in all_tools
        assert "apply_patch" in all_tools

    def test_tool_names_deduped_within_turn(self, tools_lines):
        turns = cxp.extract_conversation_turns(tools_lines)
        for t in turns:
            assert len(t["tool_uses"]) == len(set(t["tool_uses"]))

    def test_reasoning_and_raw_messages_excluded_from_content(self, tools_lines):
        turns = cxp.extract_conversation_turns(tools_lines)
        for turn in turns:
            assert "environment_context" not in turn["content"]
            assert "encrypted" not in turn["content"].lower()


class TestFindSessionFile:
    def test_finds_by_session_id_in_date_tree(self, tmp_path):
        day = tmp_path / "2026" / "06" / "13"
        day.mkdir(parents=True)
        rollout = day / "rollout-2026-06-13T15-48-28-abc-123.jsonl"
        rollout.write_text('{"type":"session_meta"}\n')
        assert cxp.find_session_file(tmp_path, "abc-123") == rollout

    def test_returns_none_for_missing(self, tmp_path):
        (tmp_path / "2026" / "06" / "13").mkdir(parents=True)
        assert cxp.find_session_file(tmp_path, "nonexistent") is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        assert cxp.find_session_file(tmp_path / "nope", "anything") is None


class TestDeriveConversationName:
    def test_first_user_message(self, sample_lines):
        assert cxp.derive_conversation_name(sample_lines).startswith(
            "Reply with exactly the word PING"
        )

    def test_truncates_long_names(self):
        lines = [{"type": "event_msg", "timestamp": "t",
                  "payload": {"type": "user_message", "message": "x" * 200}}]
        name = cxp.derive_conversation_name(lines, max_length=80)
        assert len(name) <= 80
        assert name.endswith("…")

    def test_uses_first_line_only(self):
        lines = [{"type": "event_msg", "timestamp": "t",
                  "payload": {"type": "user_message", "message": "First line\nSecond line"}}]
        assert cxp.derive_conversation_name(lines) == "First line"

    def test_untitled_when_no_user_message(self):
        lines = [{"type": "turn_context", "payload": {"model": "gpt-5.5"}}]
        assert cxp.derive_conversation_name(lines) == "(untitled)"

    def test_ignores_response_item_user_duplicates(self):
        # A raw response_item 'user' message (wrapped env context) precedes the
        # clean event_msg user_message; the name must come from the clean one.
        lines = [
            {"type": "response_item", "timestamp": "t1",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "<environment_context>noise</environment_context>"}]}},
            {"type": "event_msg", "timestamp": "t2",
             "payload": {"type": "user_message", "message": "Clean typed prompt"}},
        ]
        assert cxp.derive_conversation_name(lines) == "Clean typed prompt"


class TestExtractModel:
    def test_from_turn_context(self):
        lines = [
            {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
            {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        ]
        assert cxp.extract_model(lines) == "gpt-5.5"

    def test_ignores_non_turn_context(self):
        lines = [{"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}]
        assert cxp.extract_model(lines) == ""

    def test_empty(self):
        assert cxp.extract_model([]) == ""


class TestFormatDriftTolerance:
    def test_missing_payload_does_not_crash(self):
        lines = [{"type": "event_msg"}, {"type": "response_item", "payload": None}]
        assert cxp.extract_searchable_text(lines) == []
        assert cxp.count_tool_uses(lines) == Counter()
        assert cxp.extract_conversation_turns(lines) == []

    def test_unknown_payload_types_skipped(self):
        lines = [{"type": "event_msg", "timestamp": "t",
                  "payload": {"type": "some_future_event", "message": "x"}}]
        assert cxp.extract_searchable_text(lines) == []
        assert cxp.extract_conversation_turns(lines) == []
