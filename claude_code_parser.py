"""
Parser for Claude Code JSONL conversation transcripts.

Claude Code archives conversations as append-only JSONL files where each line
is a self-contained JSON object with a `type` field. This module extracts
searchable text, metadata, and structured conversation turns from those files.

Shared by full_text_search_chats_archive.py and view_conversation.py.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Sessions started via a slash command begin with harness-generated user lines
# (<local-command-caveat> boilerplate, then the <command-name> invocation)
# rather than a typed prompt. These must not be mistaken for human prompts
# when deriving a conversation name.
_COMMAND_BOILERPLATE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def parse_jsonl(filepath: Path) -> list[dict]:
    """Read a JSONL file and return list of parsed JSON objects.

    Skips malformed lines with a warning to stderr.
    """
    lines = []
    with open(filepath, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError as e:
                print(f"Warning: {filepath}:{i}: malformed JSON: {e}", file=sys.stderr)
    return lines


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


def _last_timestamped_line(lines: list[dict]) -> Optional[dict]:
    """Return the last line that has a timestamp."""
    for line in reversed(lines):
        if line.get("timestamp"):
            return line
    return None


def extract_session_metadata(lines: list[dict]) -> dict:
    """Extract metadata from parsed JSONL lines.

    Returns dict with keys:
      session_id, cwd, git_branch, created_at, updated_at, name
    """
    first_prompt = _first_user_prompt(lines)
    last_line = _last_timestamped_line(lines)

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
    }


def extract_searchable_text(lines: list[dict]) -> list[str]:
    """Extract text suitable for full-text search.

    Includes:
      - User messages where content is a string (actual human prompts)
      - Assistant message content blocks of type "text"

    Excludes everything else (thinking, tool_use, tool_results, system, etc.).
    """
    texts = []
    for line in lines:
        msg_type = line.get("type")

        if msg_type == "user":
            content = line.get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content)

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


def extract_conversation_turns(lines: list[dict]) -> list[dict]:
    """Extract structured conversation turns for markdown rendering.

    Returns list of dicts with keys:
      - role: "user" | "assistant"
      - timestamp: str
      - content: str (for user: the prompt; for assistant: concatenated text blocks)
      - tool_uses: list[str] (tool names, for assistant turns only)

    Consecutive assistant lines are merged into a single turn.
    User lines that are tool_results (content is a list) are skipped.
    """
    turns = []
    current_assistant = None

    def _flush_assistant():
        nonlocal current_assistant
        if current_assistant and (current_assistant["content"] or current_assistant["tool_uses"]):
            turns.append(current_assistant)
        current_assistant = None

    for line in lines:
        msg_type = line.get("type")

        if msg_type == "user":
            content = line.get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                _flush_assistant()
                turns.append({
                    "role": "user",
                    "timestamp": line.get("timestamp", ""),
                    "content": content,
                    "tool_uses": [],
                })
            # tool_result user lines (content is a list) are skipped

        elif msg_type == "assistant":
            if current_assistant is None:
                current_assistant = {
                    "role": "assistant",
                    "timestamp": line.get("timestamp", ""),
                    "content": "",
                    "tool_uses": [],
                }

            for block in line.get("message", {}).get("content", []):
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    if current_assistant["content"]:
                        current_assistant["content"] += "\n\n"
                    current_assistant["content"] += block["text"]
                elif block_type == "tool_use" and block.get("name"):
                    tool_name = block["name"]
                    if tool_name not in current_assistant["tool_uses"]:
                        current_assistant["tool_uses"].append(tool_name)
                # thinking blocks are skipped

    _flush_assistant()
    return turns


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
    """True if a user line holds a prompt the human actually typed.

    Excludes harness-generated lines: isMeta lines and slash-command
    boilerplate (caveat/command-name/command-output tags).
    """
    if line.get("type") != "user" or line.get("isMeta"):
        return False
    content = line.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    return not content.lstrip().startswith(_COMMAND_BOILERPLATE_PREFIXES)


def _slash_command_invocation(lines: list[dict]) -> str:
    """Reconstruct the first slash-command invocation, e.g. '/loop 5m'.

    Returns '' if the session contains no <command-name> line.
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
        name_match = _COMMAND_NAME_RE.search(content)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        args_match = _COMMAND_ARGS_RE.search(content)
        args = args_match.group(1).strip() if args_match else ""
        return f"{name} {args}".strip()
    return ""


def _truncate_name(text: str, max_length: int) -> str:
    # Take first line, collapse runs of whitespace
    first_line = " ".join(text.strip().split("\n")[0].split())
    if len(first_line) <= max_length:
        return first_line
    return first_line[:max_length - 1] + "\u2026"


def derive_conversation_name(lines: list[dict], max_length: int = 80) -> str:
    """Get conversation name from first human prompt, truncated.

    Sessions started via a slash command and containing no typed prompt are
    named after the command invocation. Falls back to '(untitled)'.
    """
    for line in lines:
        if _is_human_prompt(line):
            return _truncate_name(line["message"]["content"], max_length)

    command = _slash_command_invocation(lines)
    if command:
        return _truncate_name(command, max_length)
    return "(untitled)"
