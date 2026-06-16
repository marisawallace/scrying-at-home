"""
Parser for OpenAI Codex JSONL conversation transcripts (rollout files).

Codex archives each session as an append-only JSONL "rollout" file under
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl``. Every line is
``{"timestamp", "type", "payload"}`` where ``type`` is one of four kinds, with
``payload.type`` discriminating the event/response streams.

The load-bearing insight this module encodes: Codex emits each turn's text
TWICE. ``event_msg`` is the clean, human-facing stream (``user_message`` /
``agent_message`` carry the typed prompt and rendered reply); ``response_item``
is the raw API stream (``user`` messages wrapped in ``<environment_context>``,
``developer`` permissions boilerplate, and ``reasoning`` blobs that are
encrypted and unreadable). So we read ``event_msg`` for search text, naming, and
turns; ``response_item`` only for tool-call names; ``turn_context`` for the
model; and skip ``reasoning`` entirely.

This module exposes the same top-level functions with the same signatures as
``claude_code_parser`` so it satisfies ``search_index.TranscriptParser`` and the
indexer, viewer, and search CLI consume it unchanged. Like that module it is a
leaf: stdlib plus the stdlib-only ``scrying_at_home.common`` helpers, so anything
(including ``search_index``) may import it.

Shared by full_text_search_chats_archive.py and view_conversation.py.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from scrying_at_home.common.text import truncate_name
from scrying_at_home.common.constants import UNTITLED
from scrying_at_home.parsers.transcript_jsonl import (
    parse_jsonl_lines,
    last_timestamped_line,
    most_common_model,
    build_turns,
)


def parse_jsonl(filepath: Path) -> list[dict]:
    """Read a JSONL file into a list of parsed JSON objects (blank lines skipped,
    malformed lines warned to stderr)."""
    with open(filepath, "r", encoding="utf-8") as f:
        return parse_jsonl_lines(f, filepath)


def _payload(line: dict) -> dict:
    """The line's payload dict, or an empty dict for a malformed line.

    Every Codex record is ``{"timestamp", "type", "payload"}``; tolerating a
    missing/non-dict payload keeps the parser from crashing on format drift
    (older Codex schemas, truncated lines)."""
    payload = line.get("payload")
    return payload if isinstance(payload, dict) else {}


def _session_meta(lines: list[dict]) -> dict:
    """The payload of the once-per-session ``session_meta`` record (line 1),
    or an empty dict if absent."""
    for line in lines:
        if line.get("type") == "session_meta":
            return _payload(line)
    return {}


def _event_text(line: dict, event_type: str) -> Optional[str]:
    """The clean message string for an ``event_msg`` line of ``event_type``
    (``user_message`` / ``agent_message``), or None if the line is something
    else or carries no non-empty text."""
    if line.get("type") != "event_msg":
        return None
    payload = _payload(line)
    if payload.get("type") != event_type:
        return None
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return None


def extract_model(lines: list[dict]) -> str:
    """The model that did the work in a Codex session: the most-used model id
    across ``turn_context`` records (one per turn), which is the only place the
    model id appears — messages don't carry it.

    Empty string when no ``turn_context`` records a model."""
    counts: Counter[str] = Counter()
    for line in lines:
        if line.get("type") != "turn_context":
            continue
        model = _payload(line).get("model")
        if model:
            counts[model] += 1
    return most_common_model(counts)


def extract_session_metadata(lines: list[dict]) -> dict:
    """Extract metadata from parsed JSONL lines.

    Returns dict with keys (matching the claude_code_parser contract exactly so
    make_local_cli_item_meta / the indexer consume it unchanged):
      session_id, cwd, git_branch, created_at, updated_at, name, model

    session_id/cwd/created_at come from the ``session_meta`` record; model from
    ``turn_context``; git_branch is always "" (Codex records no git info)."""
    meta = _session_meta(lines)
    last_line = last_timestamped_line(lines)

    session_id = meta.get("id", "")
    cwd = meta.get("cwd", "")

    # created_at: the session's own recorded start (payload.timestamp), falling
    # back to the record's line timestamp, then to the first timestamped line.
    created_at = meta.get("timestamp", "")
    if not created_at:
        first = next((line for line in lines if line.get("timestamp")), None)
        created_at = first.get("timestamp", "") if first else ""
    updated_at = last_line.get("timestamp", "") if last_line else created_at

    return {
        "session_id": session_id,
        "cwd": cwd,
        "git_branch": "",  # Codex records no git info
        "created_at": created_at,
        "updated_at": updated_at,
        "name": derive_conversation_name(lines),
        "model": extract_model(lines),
    }


def extract_searchable_text(lines: list[dict]) -> list[str]:
    """Extract text suitable for full-text search.

    Includes only the clean ``event_msg`` stream — ``user_message`` (typed human
    prompts) and ``agent_message`` (rendered assistant replies, both commentary
    and final-answer phases).

    Excludes everything else: the raw ``response_item`` message duplicates
    (wrapped in ``<environment_context>`` / ``developer`` boilerplate), encrypted
    ``reasoning`` blobs, tool calls/outputs, and turn-lifecycle telemetry.
    """
    texts = []
    for line in lines:
        text = _event_text(line, "user_message") or _event_text(line, "agent_message")
        if text:
            texts.append(text)
    return texts


def count_tool_uses(lines: list[dict]) -> "Counter[str]":
    """Count every tool invocation by tool name, for the usage leaderboard.

    Codex tool calls live in the ``response_item`` stream: ``function_call``
    (built-in tools, e.g. ``exec_command``) and ``custom_tool_call`` (e.g.
    ``apply_patch``). Each invocation is counted; the paired ``*_output`` records
    are not.
    """
    counts: Counter[str] = Counter()
    for line in lines:
        if line.get("type") != "response_item":
            continue
        payload = _payload(line)
        if payload.get("type") in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            if name:
                counts[name] += 1
    return counts


def _classify_turn_events(line: dict) -> list:
    """Per-line (kind, value, timestamp) events for build_turns (Codex).

    Built from the clean event_msg stream plus response_item tool calls:
    user_message -> a user event; agent_message -> assistant text; a
    function_call / custom_tool_call response_item -> a tool event. Reasoning,
    raw response_item messages, and turn-lifecycle records produce nothing. No
    assistant_open: the assistant turn is pinned to its first agent_message /
    tool call (build_turns' lazy open), matching the original behavior.
    """
    ts = line.get("timestamp", "")
    user_text = _event_text(line, "user_message")
    if user_text:
        return [("user", user_text, ts)]
    agent_text = _event_text(line, "agent_message")
    if agent_text:
        return [("assistant_text", agent_text, ts)]
    if line.get("type") == "response_item":
        payload = _payload(line)
        if payload.get("type") in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            if name:
                return [("tool", name, ts)]
    return []


def extract_conversation_turns(lines: list[dict]) -> list[dict]:
    """Structured conversation turns (markdown/HTML) from the clean event_msg
    stream + response_item tool calls. Merge/flush rules live in build_turns;
    _classify_turn_events supplies the Codex per-line interpretation."""
    return build_turns(lines, _classify_turn_events)


def find_session_file(codex_data_dir: Path, session_id: str) -> Optional[Path]:
    """Find a rollout JSONL file by session id under codex_data_dir.

    The archive mirrors Codex's ``YYYY/MM/DD`` date tree and the session id is
    embedded in the filename (``rollout-<ISO-ts>-<session_id>.jsonl``), so —
    unlike Claude Code's ``{project-slug}/{id}.jsonl`` layout — there is no
    per-project directory to scan; we glob the id across the whole tree.
    """
    if not codex_data_dir.exists():
        return None
    for candidate in codex_data_dir.rglob(f"rollout-*{session_id}*.jsonl"):
        if candidate.is_file():
            return candidate
    return None


def derive_conversation_name(lines: list[dict], max_length: int = 80) -> str:
    """Get conversation name from the first typed human prompt, truncated.

    The first ``event_msg``/``user_message`` is the clean typed prompt (unlike
    Claude Code, Codex has no slash-command boilerplate to skip). Falls back to
    '(untitled)'.
    """
    for line in lines:
        text = _event_text(line, "user_message")
        if text:
            return truncate_name(text, max_length)
    return UNTITLED
