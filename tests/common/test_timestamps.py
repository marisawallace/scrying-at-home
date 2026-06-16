"""Unit tests for scrying_at_home.common.timestamps — the single ISO/epoch timestamp leaf."""
import os
import time

from scrying_at_home.common import timestamps as ts


def _with_tz(tz, fn):
    """Run fn() with TZ pinned to a fixed (non-UTC) zone, restoring afterwards."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


# --- parse_iso --------------------------------------------------------------

def test_parse_iso_handles_z_suffix_and_bare_date():
    assert ts.parse_iso("2026-01-15T09:30:00Z") is not None
    assert ts.parse_iso("2026-01-15") is not None


def test_parse_iso_rejects_garbage():
    assert ts.parse_iso("") is None
    assert ts.parse_iso("not-a-date") is None
    assert ts.parse_iso(None) is None


def test_parse_iso_coerces_naive_to_utc():
    dt = ts.parse_iso("2026-01-15T09:30:00")  # no offset
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


# --- to_utc_iso (regression for the ChatGPT 'Z'-label offset bug) -----------

def test_to_utc_iso_is_utc_regardless_of_local_tz():
    # epoch 0 == 1970-01-01T00:00:00Z. The old code (fromtimestamp without a tz,
    # then + 'Z') labeled local wall-clock as UTC; under a negative-offset zone
    # that yields 1969-12-31T19:00:00Z. The fixed converter must stay UTC.
    assert _with_tz("America/New_York", lambda: ts.to_utc_iso(0)) == "1970-01-01T00:00:00Z"


def test_to_utc_iso_known_instant():
    assert ts.to_utc_iso(1704103200) == "2024-01-01T10:00:00Z"


# --- derive_updated_at ------------------------------------------------------

def test_derive_updated_at_prefers_last_message_for_conversations():
    data = {
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "chat_messages": [{"created_at": "2024-01-03T00:00:00Z"}],
    }
    assert ts.derive_updated_at(data, "conversation") == "2024-01-03T00:00:00Z"


def test_derive_updated_at_falls_back_to_updated_then_created():
    assert ts.derive_updated_at({"created_at": "c", "updated_at": "u"}, "project") == "u"
    assert ts.derive_updated_at({"created_at": "c"}, "project") == "c"


def test_derive_updated_at_ignores_empty_last_message_date():
    data = {"created_at": "c", "updated_at": "u", "chat_messages": [{"created_at": ""}]}
    assert ts.derive_updated_at(data, "conversation") == "u"
