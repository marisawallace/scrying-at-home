"""Regression: recency_boost must not silently return 0 for a naive timestamp.

The old inline parse produced a naive datetime and subtracted it from an aware
``now``, raising TypeError that a bare ``except`` swallowed -> 0.0 boost. The
shared parse_iso coerces naive to UTC, so a recent naive timestamp earns a boost.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.search import engine as fts


def test_recent_naive_timestamp_earns_a_boost():
    recent_naive = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    assert fts.recency_boost(recent_naive) > 0


def test_recent_utc_timestamp_earns_a_boost():
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fts.recency_boost(recent) > 0


def test_garbage_timestamp_is_zero():
    assert fts.recency_boost("") == 0.0
    assert fts.recency_boost("not-a-date") == 0.0
