"""
Parser for Claude Code JSONL conversation transcripts.

Claude Code archives conversations as append-only JSONL files where each line
is a self-contained JSON object with a `type` field. This module extracts
searchable text, metadata, and structured conversation turns from those files.

Shared by full_text_search_chats_archive.py and view_conversation.py.
"""

from __future__ import annotations

import re
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

# A slash command typed into the prompt is recorded by the harness as a user
# line carrying <command-name>/<command-args> tags (plus a <command-message>
# echo). Tag order varies and <command-args> is omitted when there were none,
# e.g.:  <command-message>code-review</command-message>
#        <command-name>/code-review</command-name>
# We reconstruct the typed form ('/code-review') rather than show the raw tags,
# since the invocation is human input. By contrast the lines below are pure
# machine boilerplate the harness injects as user turns — the caveat blurb, a
# command's captured stdout/stderr, and background-task-completion notices —
# which the human did not type, so they are dropped from naming/search/view.
# (Extend this tuple as new harness-injected user-line wrappers appear.)
_MACHINE_BOILERPLATE_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<task-notification>",
)

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _reconstruct_command(content: str) -> Optional[str]:
    """Reconstruct the slash command a human typed from its harness encoding.

    '<command-name>/code-review</command-name>' becomes '/code-review';
    a '/loop' name with '<command-args>5m /foo</command-args>' becomes
    '/loop 5m /foo'. Tag order is irrelevant and <command-args> is optional.

    Returns None when `content` holds no <command-name> tag.
    """
    name_match = _COMMAND_NAME_RE.search(content)
    if not name_match:
        return None
    name = name_match.group(1).strip()
    args_match = _COMMAND_ARGS_RE.search(content)
    args = args_match.group(1).strip() if args_match else ""
    return f"{name} {args}".strip()


def _user_line_text(line: dict) -> Optional[str]:
    """The text a user line contributes to naming, search, and rendering.

    Returns a typed prompt verbatim, or — for a slash-command turn — the
    reconstructed invocation ('/code-review'). Returns None for lines the human
    did not type: non-user / tool_result lines (non-string content), isMeta
    context, and machine boilerplate (caveat blurb, command stdout/stderr). The
    raw archive keeps every line verbatim regardless.
    """
    if line.get("type") != "user" or line.get("isMeta"):
        return None
    content = line.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if content.lstrip().startswith(_MACHINE_BOILERPLATE_PREFIXES):
        return None
    command = _reconstruct_command(content)
    return command if command is not None else content


def parse_jsonl(filepath: Path) -> list[dict]:
    """Read a JSONL file into a list of parsed JSON objects (blank lines skipped,
    malformed lines warned to stderr)."""
    with open(filepath, "r", encoding="utf-8") as f:
        return parse_jsonl_lines(f, filepath)


def _first_user_prompt(lines: list[dict]) -> Optional[dict]:
    """Return the first user line with string content (used for created_at).

    Includes slash-command boilerplate lines, which carry the session-start
    timestamp; use _is_human_prompt to filter those out when naming.
    """
    for line in lines:
        if line.get("type") == "user":
            content = line.get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                return line
    return None


def extract_model(lines: list[dict]) -> str:
    """The model that did the work in a Claude Code session: the most-used
    model id across assistant lines, ignoring the "<synthetic>" placeholder
    Claude Code stamps on harness-generated assistant turns.

    Empty string when no real model is recorded (e.g. a session with only
    synthetic lines)."""
    counts: Counter[str] = Counter()
    for line in lines:
        if line.get("type") != "assistant":
            continue
        model = line.get("message", {}).get("model")
        if model and model != "<synthetic>":
            counts[model] += 1
    return most_common_model(counts)


def extract_session_metadata(lines: list[dict]) -> dict:
    """Extract metadata from parsed JSONL lines.

    Returns dict with keys:
      session_id, cwd, git_branch, created_at, updated_at, name, model
    """
    first_prompt = _first_user_prompt(lines)
    last_line = last_timestamped_line(lines)

    session_id = ""
    cwd = ""
    git_branch = ""

    # Get session_id, cwd, and gitBranch from the first line that has each.
    # gitBranch is optional (empty for non-repo cwds), so we stop as soon as
    # session_id and cwd are populated rather than waiting for a branch that
    # may never appear.
    for line in lines:
        if not session_id and line.get("sessionId"):
            session_id = line["sessionId"]
        if not cwd and line.get("cwd"):
            cwd = line["cwd"]
        if not git_branch and line.get("gitBranch"):
            git_branch = line["gitBranch"]
        if session_id and cwd:
            break

    created_at = first_prompt.get("timestamp", "") if first_prompt else ""
    updated_at = last_line.get("timestamp", "") if last_line else created_at

    name = derive_conversation_name(lines)

    return {
        "session_id": session_id,
        "cwd": cwd,
        "git_branch": git_branch,
        "created_at": created_at,
        "updated_at": updated_at,
        "name": name,
        "model": extract_model(lines),
    }


def extract_searchable_text(lines: list[dict]) -> list[str]:
    """Extract text suitable for full-text search.

    Includes:
      - Text a human typed: prompts verbatim, and slash commands reconstructed
        to their typed form ('/code-review'); see _user_line_text
      - Assistant message content blocks of type "text"

    Excludes everything else: thinking, tool_use, tool_results, system, and
    machine boilerplate (isMeta context, the <local-command-caveat> blurb, and
    command stdout/stderr). The raw archive retains those lines verbatim; they
    are simply not indexed as searchable text.
    """
    texts = []
    for line in lines:
        msg_type = line.get("type")

        if msg_type == "user":
            text = _user_line_text(line)
            if text:
                texts.append(text)

        elif msg_type == "assistant":
            for block in line.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])

    return texts


def count_tool_uses(lines: list[dict]) -> "Counter[str]":
    """Count every assistant tool_use block by tool name.

    Unlike extract_conversation_turns (which de-dups tool names within a turn
    for display), this counts each invocation, suitable for a usage leaderboard.
    """
    counts: Counter[str] = Counter()
    for line in lines:
        if line.get("type") != "assistant":
            continue
        for block in line.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name"):
                counts[block["name"]] += 1
    return counts


def _classify_turn_events(line: dict) -> list:
    """Per-line (kind, value, timestamp) events for build_turns (Claude Code).

    A user line a human typed (prompt, or reconstructed slash command — see
    _user_line_text) is one user event; an assistant line opens the turn
    (pinning its timestamp to this first assistant line) and contributes one
    event per text / tool_use block. Tool_result user lines (list content),
    machine boilerplate (isMeta, caveat blurb, command stdout/stderr), and
    thinking blocks produce nothing.
    """
    ts = line.get("timestamp", "")
    msg_type = line.get("type")
    if msg_type == "user":
        text = _user_line_text(line)
        if text:
            return [("user", text, ts)]
        return []
    if msg_type == "assistant":
        events = [("assistant_open", None, ts)]
        for block in line.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                events.append(("assistant_text", block["text"], ts))
            elif block_type == "tool_use" and block.get("name"):
                events.append(("tool", block["name"], ts))
        return events
    return []


def extract_conversation_turns(lines: list[dict]) -> list[dict]:
    """Structured conversation turns for markdown rendering: consecutive
    assistant lines merge into one turn, tool_result user lines are skipped. The
    merge/flush rules live in build_turns; _classify_turn_events supplies the
    Claude Code per-line interpretation."""
    return build_turns(lines, _classify_turn_events)


def find_session_file(cc_data_dir: Path, session_id: str) -> Optional[Path]:
    """Find a JSONL file by session ID across all project-slug directories.

    Searches cc_data_dir/{any-project-slug}/{session_id}.jsonl
    """
    if not cc_data_dir.exists():
        return None

    target = f"{session_id}.jsonl"
    for project_dir in cc_data_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / target
        if candidate.exists():
            return candidate
    return None


def _is_human_prompt(line: dict) -> bool:
    """True if a user line is descriptive prose the human typed — not a slash
    command and not machine boilerplate. Naming prefers such prose over a bare
    command invocation; see _user_line_text for the shared filtering.
    """
    if _user_line_text(line) is None:
        return False
    return _reconstruct_command(line["message"]["content"]) is None


def _slash_command_invocation(lines: list[dict]) -> str:
    """Reconstruct the first slash-command invocation, e.g. '/loop 5m'.

    Checks user and system lines. Returns '' if the session contains no
    <command-name> line.
    """
    for line in lines:
        if line.get("type") == "user":
            content = line.get("message", {}).get("content")
        elif line.get("type") == "system":
            content = line.get("content")
        else:
            continue
        if not isinstance(content, str):
            continue
        command = _reconstruct_command(content)
        if command:
            return command
    return ""


def derive_conversation_name(lines: list[dict], max_length: int = 80) -> str:
    """Get conversation name from first human prompt, truncated.

    Sessions started via a slash command and containing no typed prompt are
    named after the command invocation. Falls back to '(untitled)'.
    """
    for line in lines:
        if _is_human_prompt(line):
            return truncate_name(line["message"]["content"], max_length)

    command = _slash_command_invocation(lines)
    if command:
        return truncate_name(command, max_length)
    return UNTITLED
