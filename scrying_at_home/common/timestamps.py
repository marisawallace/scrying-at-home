"""ISO-8601 timestamp handling — the single home for how the project reads and
writes conversation timestamps.

Storage convention: every stored timestamp is UTC ISO-8601 (historically
suffixed ``Z``). Readers must therefore treat a *naive* timestamp — one with no
offset — as UTC, never as local time. Otherwise a subtraction against an aware
``now`` raises (and a bare ``except`` silently swallows the recency signal), or a
comparison is quietly off by the machine's UTC offset. ``parse_iso`` is the one
parser that enforces this; anything reading a timestamp should go through it
rather than re-deriving the ``replace('Z', '+00:00')`` / ``fromisoformat`` dance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a tz-aware datetime (UTC if naive).

    Returns None for empty/malformed values so callers can skip them. Accepts a
    bare date (``2026-01-02``) as midnight UTC and tolerates a trailing ``Z``. A
    naive result is coerced to UTC — the storage convention — which is what lets
    recency math subtract it against an aware ``now`` without raising.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to a bare date prefix (handles odd trailing suffixes).
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(dt: datetime) -> datetime:
    """Convert a tz-aware datetime to the machine's local timezone."""
    return dt.astimezone()


def to_utc_iso(epoch: float) -> str:
    """A Unix epoch-seconds value as a UTC ISO-8601 string suffixed ``Z``.

    Always UTC regardless of the machine's local timezone. ``datetime.from-
    timestamp(epoch)`` with no tz yields local wall-clock; labeling *that* ``Z``
    stores a timestamp wrong by the local offset on any non-UTC machine. Passing
    ``tz=timezone.utc`` is the fix; the ``Z`` suffix matches the archive's other
    stored timestamps.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def derive_updated_at(data: dict, item_type: str) -> str:
    """The effective ``updated_at`` for a Claude/ChatGPT export item.

    Top-level ``updated_at`` (falling back to ``created_at``), except for a
    conversation with messages, where the last message's ``created_at`` wins.
    Defined once so the live-search scan path and the indexer cannot derive a
    different recency basis for the same item. ``data['created_at']`` must exist
    — every caller already guarantees it.
    """
    updated_at = data.get("updated_at", data["created_at"])
    if item_type == "conversation":
        messages = data.get("chat_messages", [])
        if messages:
            last_msg_date = messages[-1].get("created_at", "")
            if last_msg_date:
                updated_at = last_msg_date
    return updated_at
