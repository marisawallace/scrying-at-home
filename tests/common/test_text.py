"""Unit tests for scrying_at_home.common.text."""
from scrying_at_home.common import text


def test_truncate_name_collapses_whitespace_and_takes_first_line():
    assert text.truncate_name("  hello   world \n second line", 80) == "hello world"


def test_truncate_name_caps_with_ellipsis():
    out = text.truncate_name("x" * 100, 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_normalize_uuid_lowercases_and_trims():
    # Regression for `view <UPPERCASE-UUID>` silently missing: the id is stored
    # lowercased, so user input must be lowercased before lookup.
    assert text.normalize_uuid("  ABCD-EF  ") == "abcd-ef"


def test_normalize_uuid_strips_claude_chat_url():
    assert text.normalize_uuid("https://claude.ai/chat/ABCD-1234") == "abcd-1234"
