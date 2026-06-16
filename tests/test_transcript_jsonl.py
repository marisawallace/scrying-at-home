"""Unit tests for the shared scrying_at_home.parsers.transcript_jsonl primitives."""
from collections import Counter

from scrying_at_home.parsers import transcript_jsonl as tj


def test_parse_jsonl_lines_skips_blank_and_warns_malformed(capsys):
    out = tj.parse_jsonl_lines(['{"a": 1}', '', '  ', 'not json', '{"b": 2}'], "src")
    assert out == [{"a": 1}, {"b": 2}]
    assert "malformed JSON" in capsys.readouterr().err


def test_last_timestamped_line():
    lines = [{"timestamp": "t1"}, {"x": 1}, {"timestamp": "t2"}, {"y": 2}]
    assert tj.last_timestamped_line(lines) == {"timestamp": "t2"}
    assert tj.last_timestamped_line([{"x": 1}]) is None


def test_most_common_model():
    assert tj.most_common_model(Counter()) == ""
    assert tj.most_common_model(Counter(["a", "a", "b"])) == "a"


def _classify(line):
    return line["events"]


def test_build_turns_merges_assistant_and_dedups_tools():
    lines = [
        {"events": [("user", "hi", "t0")]},
        {"events": [("assistant_text", "part1", "t1")]},
        {"events": [("assistant_text", "part2", "t2"), ("tool", "Read", "t2")]},
        {"events": [("tool", "Read", "t3")]},  # duplicate tool name within the turn
        {"events": [("user", "next", "t4")]},
    ]
    turns = tj.build_turns(lines, _classify)
    assert turns[0] == {"role": "user", "timestamp": "t0", "content": "hi", "tool_uses": []}
    asst = turns[1]
    assert asst["role"] == "assistant"
    assert asst["timestamp"] == "t1"            # pinned to the first assistant event
    assert asst["content"] == "part1\n\npart2"  # \n\n-joined
    assert asst["tool_uses"] == ["Read"]        # de-duped
    assert turns[2]["content"] == "next"


def test_build_turns_assistant_open_pins_timestamp_before_text():
    # assistant_open with no content still sets the turn timestamp even when the
    # text arrives on a later event (the Claude Code thinking-only-first-line case).
    lines = [
        {"events": [("assistant_open", None, "t1")]},                       # thinking-only line
        {"events": [("assistant_open", None, "t2"), ("assistant_text", "hello", "t2")]},
    ]
    assert tj.build_turns(lines, _classify) == [
        {"role": "assistant", "timestamp": "t1", "content": "hello", "tool_uses": []}
    ]


def test_build_turns_drops_empty_assistant_turn():
    lines = [{"events": [("assistant_open", None, "t1")]}]  # opened, never filled
    assert tj.build_turns(lines, _classify) == []
