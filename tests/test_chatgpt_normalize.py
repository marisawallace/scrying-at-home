"""
Unit tests for ChatGPTProvider._normalize_conversation() — mapping ChatGPT's
export fields (id/title/create_time) to the internal schema, robustly.

validate() only inspects the first conversation in an export, so the normalizer
itself must tolerate later conversations with missing/null fields rather than
crashing with a raw KeyError/TypeError.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.sync import local_chats as sync


def _provider():
    return sync.ChatGPTProvider(Path("/tmp"), {})


def test_maps_fields_to_internal_schema():
    conv = {"id": "abc", "title": "Hello", "create_time": 1_700_000_000}
    out = _provider()._normalize_conversation(conv)
    assert out["uuid"] == "abc"
    assert out["name"] == "Hello"
    assert out["created_at"].endswith("Z")


def test_blank_title_yields_empty_name():
    # sanitize_name() later turns "" into "untitled"; the normalizer just must
    # not propagate None.
    conv = {"id": "abc", "title": None, "create_time": 1_700_000_000}
    assert _provider()._normalize_conversation(conv)["name"] == ""


def test_falls_back_to_update_time_when_create_time_null():
    conv = {"id": "abc", "title": "t", "create_time": None, "update_time": 1_700_000_000}
    out = _provider()._normalize_conversation(conv)
    assert out["created_at"].endswith("Z")


def test_missing_id_exits_cleanly_not_keyerror():
    # A conversation past the first one could lack 'id' entirely.
    with pytest.raises(SystemExit):
        _provider()._normalize_conversation({"title": "t", "create_time": 1})


def test_no_usable_timestamp_exits_cleanly_not_typeerror():
    with pytest.raises(SystemExit):
        _provider()._normalize_conversation({"id": "abc", "title": "t", "create_time": None})


def test_created_at_is_utc_not_local_wall_clock():
    # Regression: create_time (epoch) must store a UTC instant, not local
    # wall-clock mislabeled 'Z'. 1704103200 == 2024-01-01T10:00:00Z; under a
    # non-UTC zone the old code stored the offset-shifted wall-clock.
    import os
    import time
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        out = _provider()._normalize_conversation({"id": "abc", "title": "t", "create_time": 1704103200})
        assert out["created_at"] == "2024-01-01T10:00:00Z"
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()
