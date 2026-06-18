"""Shared transcript-parsing primitives for the local-CLI JSONL parsers
(claude_code, codex) and the indexer.

These are the genuinely format-agnostic pieces — the JSONL tokenizer, the
last-timestamp scan, the most-common-model tie-break, and the turn-flush state
machine — pulled out of the two parsers so the merge/ordering rules live in one
place. The per-line CLASSIFY logic (which record means what) stays in each
parser, since that is where the two formats actually differ. Stdlib only.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Callable, Optional

from scrying_at_home.common.ansi import warning


def parse_jsonl_lines(raw_lines, label) -> list[dict]:
    """Parse an iterable of raw JSONL strings into dicts, skipping blank lines and
    warning to stderr on malformed ones. ``label`` names the source in the warning
    (e.g. the file path). Shared by both parsers' parse_jsonl and the indexer's
    parse_jsonl_texts so the skip rule and warning format live in one place."""
    out = []
    for i, raw in enumerate(raw_lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError as e:
            print(warning(f"Warning: {label}:{i}: malformed JSON: {e}", stream=sys.stderr), file=sys.stderr)
    return out


def last_timestamped_line(lines: list[dict]) -> Optional[dict]:
    """The last line carrying a top-level ``timestamp`` (format-agnostic)."""
    for line in reversed(lines):
        if line.get("timestamp"):
            return line
    return None


def most_common_model(counts: "Counter[str]") -> str:
    """The most-used model id in ``counts``, or "" when empty — the shared
    empty/tie-break convention. Each parser supplies the provider-specific
    counting (which records carry the model id)."""
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def build_turns(lines: list[dict], classify: Callable[[dict], list]) -> list[dict]:
    """Assemble structured conversation turns from a transcript.

    ``classify(line)`` returns a list of ``(kind, value, timestamp)`` events for
    that line — the only provider-specific piece. Recognized kinds:
      - ("user", text, ts):           flush the open assistant turn, emit a user turn
      - ("assistant_open", None, ts): open an assistant turn (setting its timestamp)
                                      if none is open — lets a parser pin the turn
                                      timestamp to the first assistant *line* even
                                      when that line carries no text/tool yet
      - ("assistant_text", text, ts): open if needed, append text (``\\n\\n`` joined)
      - ("tool", name, ts):           open if needed, add a de-duped tool name

    Consecutive assistant events merge into one turn; an assistant turn with no
    content and no tools is dropped. Returns dicts with keys role / timestamp /
    content / tool_uses — the contract the viewer and tool-count paths consume.
    """
    turns: list[dict] = []
    current: Optional[dict] = None

    def flush():
        nonlocal current
        if current and (current["content"] or current["tool_uses"]):
            turns.append(current)
        current = None

    def ensure(ts):
        nonlocal current
        if current is None:
            current = {"role": "assistant", "timestamp": ts, "content": "", "tool_uses": []}

    for line in lines:
        for kind, value, ts in classify(line):
            if kind == "user":
                flush()
                turns.append({"role": "user", "timestamp": ts, "content": value, "tool_uses": []})
            elif kind == "assistant_open":
                ensure(ts)
            elif kind == "assistant_text":
                ensure(ts)
                if current["content"]:
                    current["content"] += "\n\n"
                current["content"] += value
            elif kind == "tool":
                ensure(ts)
                if value not in current["tool_uses"]:
                    current["tool_uses"].append(value)
    flush()
    return turns
