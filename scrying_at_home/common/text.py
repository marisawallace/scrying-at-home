"""Text / identity normalization leaf: conversation-name truncation and uuid
normalization. Stdlib only (plus the sibling ``scrying_at_home.common.constants``).
"""
from __future__ import annotations

from scrying_at_home.common.constants import CLAUDE_CHAT_URL_PREFIX


def truncate_name(text: str, max_length: int) -> str:
    """First line of ``text``, whitespace collapsed, capped at ``max_length``
    with a trailing ellipsis. The shared conversation display-name rule for both
    transcript parsers."""
    first_line = " ".join(text.strip().split("\n")[0].split())
    if len(first_line) <= max_length:
        return first_line
    return first_line[:max_length - 1] + "…"


def normalize_uuid(raw: str) -> str:
    """Normalize a conversation/session id for lookup.

    Strips a pasted claude.ai chat URL down to the bare id, trims whitespace, and
    lowercases. UUIDs are case-insensitive but are stored lowercased everywhere,
    so the viewer and search must lowercase user input before comparing —
    otherwise a pasted upper/mixed-case id silently misses. Accepting the full
    chat URL lets either entry point take a copy-pasted link.
    """
    value = raw.strip()
    if value.startswith(CLAUDE_CHAT_URL_PREFIX):
        value = value[len(CLAUDE_CHAT_URL_PREFIX):]
    return value.strip().lower()
