#!/usr/bin/env python3
"""
View a conversation as Markdown or HTML.

Converts a conversation JSON to Markdown or HTML format and opens it.
Supports multiple LLM providers (Claude, ChatGPT, etc.).
"""

import argparse
import functools
import html
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from paths import LLM_DATA_SUBDIR, LOCAL_VIEWS_SUBDIR, parse_claude_code_sources

CLAUDE_CHAT_URL_PREFIX = "https://claude.ai/chat/"


def extract_uuid(value: str) -> str:
    """Extract a UUID from a value that may be a Claude chat URL or a bare UUID."""
    if value.startswith(CLAUDE_CHAT_URL_PREFIX):
        return value[len(CLAUDE_CHAT_URL_PREFIX):]
    return value


def find_conversation_file(data_dir: Path, uuid: str) -> Optional[tuple[Path, str]]:
    """
    Find the JSON file for a given conversation UUID.

    Returns tuple of (file_path, provider) or None if not found.
    """
    # Search in both claude/ and chatgpt/ subdirectories
    for provider in ["claude", "chatgpt"]:
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue

        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue

            # Check conversations
            conversations_dir = user_dir / "conversations"
            if conversations_dir.exists():
                for conv_file in conversations_dir.glob("*.json"):
                    try:
                        with open(conv_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data.get("uuid") == uuid:
                                return conv_file, provider
                    except Exception:
                        continue

            # Check projects
            projects_dir = user_dir / "projects"
            if projects_dir.exists():
                for proj_file in projects_dir.glob("*.json"):
                    try:
                        with open(proj_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data.get("uuid") == uuid:
                                return proj_file, provider
                    except Exception:
                        continue

    return None


def format_timestamp(timestamp_str: str) -> str:
    """Format ISO timestamp to readable format."""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return timestamp_str


def conversation_to_markdown(data: dict) -> str:
    """Convert conversation JSON to Markdown format."""
    lines = []

    # Header with metadata
    name = data.get("name", "(untitled)")
    lines.append(f"# {name}\n")
    lines.append(f"**UUID:** `{data.get('uuid', 'unknown')}`  ")
    lines.append(f"**Created:** {format_timestamp(data.get('created_at', ''))}  ")
    lines.append(f"**Updated:** {format_timestamp(data.get('updated_at', ''))}  ")

    if data.get("summary"):
        lines.append(f"**Summary:** {data['summary']}  ")

    lines.append("\n---\n")

    # Process each message
    for msg in data.get("chat_messages", []):
        sender = msg.get("sender", "unknown")
        timestamp = format_timestamp(msg.get("created_at", ""))

        # Message header
        sender_label = "**User**" if sender == "human" else "**Assistant**"
        lines.append(f"\n## {sender_label}\n")
        lines.append(f"*{timestamp}*\n")

        # Message content
        # Prefer the 'text' field as it's cleaner, but also include content blocks
        text = msg.get("text", "")
        if text:
            lines.append(f"\n{text}\n")

        # Add any additional content from content blocks if different
        for content_block in msg.get("content", []):
            if content_block.get("type") == "text":
                block_text = content_block.get("text", "")
                # Only add if significantly different from main text
                if block_text and block_text != text:
                    lines.append(f"\n{block_text}\n")

        # Note attachments if present
        attachments = msg.get("attachments", [])
        files = msg.get("files", [])
        if attachments:
            lines.append(f"\n*Attachments: {len(attachments)} file(s)*\n")
        if files:
            lines.append(f"\n*Files: {len(files)} file(s)*\n")

        lines.append("\n---\n")

    return "\n".join(lines)


@functools.lru_cache(maxsize=1)
def _markdown_renderer():
    """Build a mistune Markdown renderer with Pygments-highlighted code blocks.

    Memoized: the renderer (and its lexer registry) is built once per process.
    """
    import vendor_loader  # noqa: F401  -- puts vendored mistune/pygments on sys.path
    import mistune
    from mistune.renderers.html import HTMLRenderer
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound

    class _HighlightRenderer(HTMLRenderer):
        """HTML renderer that runs fenced code blocks through Pygments."""

        def block_code(self, code: str, info: str = None) -> str:
            if info:
                try:
                    lexer = get_lexer_by_name(info.strip().split()[0], stripall=True)
                except ClassNotFound:
                    lexer = None
                if lexer is not None:
                    return highlight(code, lexer, HtmlFormatter())
            return "<pre><code>" + mistune.util.escape(code) + "</code></pre>\n"

    return mistune.create_markdown(
        renderer=_HighlightRenderer(),
        plugins=["strikethrough", "table", "url"],
    )


@functools.lru_cache(maxsize=1)
def _pygments_css() -> str:
    """CSS rules for Pygments-highlighted code blocks (the `.highlight` class)."""
    import vendor_loader  # noqa: F401
    from pygments.formatters import HtmlFormatter

    return HtmlFormatter().get_style_defs(".highlight")


def render_markdown(text: str) -> str:
    """Render Markdown source text to an HTML fragment."""
    if not text:
        return ""
    return _markdown_renderer()(text)


def conversation_to_html(data: dict) -> str:
    """Convert conversation JSON to HTML format with styling."""
    name = html.escape(data.get("name", "(untitled)"))
    uuid = html.escape(data.get("uuid", "unknown"))
    created = html.escape(format_timestamp(data.get("created_at", "")))
    updated = html.escape(format_timestamp(data.get("updated_at", "")))
    summary = html.escape(data.get("summary", "")) if data.get("summary") else ""

    # Build HTML
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + name + """</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        .header {
            border-bottom: 3px solid #e0e0e0;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }

        h1 {
            color: #2c3e50;
            margin-bottom: 15px;
            font-size: 2em;
        }

        .metadata {
            color: #666;
            font-size: 0.9em;
            line-height: 1.8;
        }

        .metadata strong {
            color: #444;
        }

        .metadata code {
            background: #f0f0f0;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.85em;
        }

        .message {
            margin-bottom: 30px;
            padding: 20px;
            border-radius: 8px;
            border-left: 4px solid #ddd;
        }

        .message.user {
            background: #f8f9fa;
            border-left-color: #4a90e2;
        }

        .message.assistant {
            background: #fefefe;
            border-left-color: #50c878;
        }

        .message-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #e0e0e0;
        }

        .sender {
            font-weight: 600;
            font-size: 1.1em;
        }

        .sender.user {
            color: #4a90e2;
        }

        .sender.assistant {
            color: #50c878;
        }

        .timestamp {
            color: #999;
            font-size: 0.85em;
        }

        .message-content {
            color: #333;
            word-wrap: break-word;
        }

        /* Spacing for rendered Markdown block elements. */
        .message-content > *:first-child { margin-top: 0; }
        .message-content > *:last-child { margin-bottom: 0; }

        .message-content p { margin: 10px 0; }

        .message-content h1,
        .message-content h2,
        .message-content h3,
        .message-content h4,
        .message-content h5,
        .message-content h6 {
            color: #2c3e50;
            margin: 18px 0 8px;
            line-height: 1.3;
        }

        .message-content h1 { font-size: 1.5em; }
        .message-content h2 { font-size: 1.3em; }
        .message-content h3 { font-size: 1.15em; }

        .message-content ul,
        .message-content ol {
            margin: 10px 0;
            padding-left: 1.6em;
        }

        .message-content li { margin: 4px 0; }

        .message-content blockquote {
            margin: 10px 0;
            padding: 4px 16px;
            border-left: 4px solid #d0d7de;
            color: #57606a;
        }

        .message-content a { color: #4a90e2; }

        .message-content table {
            border-collapse: collapse;
            margin: 12px 0;
        }

        .message-content th,
        .message-content td {
            border: 1px solid #d0d7de;
            padding: 6px 12px;
        }

        .message-content th { background: #f0f0f0; }

        .message-content hr {
            border: none;
            border-top: 1px solid #e0e0e0;
            margin: 20px 0;
        }

        .message-content pre,
        .message-content .highlight {
            background: #f6f8fa;
            padding: 12px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 10px 0;
        }

        .message-content pre { margin: 0; }

        .message-content code {
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
        }

        /* Inline code (not inside a highlighted block). */
        .message-content :not(pre) > code {
            background: #f0f0f0;
            padding: 2px 5px;
            border-radius: 3px;
        }

        .attachments {
            margin-top: 15px;
            padding: 10px;
            background: #fff3cd;
            border-radius: 4px;
            color: #856404;
            font-size: 0.9em;
        }

        /* Pygments syntax-highlighting rules for fenced code blocks. */
""" + _pygments_css() + """
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>""" + name + """</h1>
            <div class="metadata">
                <div><strong>UUID:</strong> <code>""" + uuid + """</code></div>
                <div><strong>Created:</strong> """ + created + """</div>
                <div><strong>Updated:</strong> """ + updated + """</div>""")

    if summary:
        html_parts.append("""
                <div><strong>Summary:</strong> """ + summary + """</div>""")

    html_parts.append("""
            </div>
        </div>

        <div class="messages">""")

    # Process each message
    for msg in data.get("chat_messages", []):
        sender = msg.get("sender", "unknown")
        timestamp = html.escape(format_timestamp(msg.get("created_at", "")))
        sender_class = "user" if sender == "human" else "assistant"
        sender_label = "User" if sender == "human" else "Assistant"

        html_parts.append(f"""
            <div class="message {sender_class}">
                <div class="message-header">
                    <div class="sender {sender_class}">{sender_label}</div>
                    <div class="timestamp">{timestamp}</div>
                </div>
                <div class="message-content">""")

        # Message content. Collect the message text plus any distinct text
        # content blocks, then render the whole thing as Markdown so it reads
        # nicely in the browser (headers, lists, code blocks, links, etc.).
        text = msg.get("text", "")
        segments = [text] if text else []
        for content_block in msg.get("content", []):
            if content_block.get("type") == "text":
                block_text = content_block.get("text", "")
                if block_text and block_text != text:
                    segments.append(block_text)
        html_parts.append(render_markdown("\n\n".join(segments)))

        html_parts.append("""</div>""")

        # Note attachments if present
        attachments = msg.get("attachments", [])
        files = msg.get("files", [])
        if attachments or files:
            attachment_text = []
            if attachments:
                attachment_text.append(f"Attachments: {len(attachments)} file(s)")
            if files:
                attachment_text.append(f"Files: {len(files)} file(s)")
            html_parts.append(f"""
                <div class="attachments">{" | ".join(attachment_text)}</div>""")

        html_parts.append("""
            </div>""")

    html_parts.append("""
        </div>
    </div>
</body>
</html>""")

    return "\n".join(html_parts)


def claude_code_to_markdown(filepath: Path) -> str:
    """Convert a Claude Code JSONL session to Markdown."""
    import claude_code_parser as ccp

    lines = ccp.parse_jsonl(filepath)
    metadata = ccp.extract_session_metadata(lines)
    turns = ccp.extract_conversation_turns(lines)

    parts = []

    # Header
    parts.append(f"# {metadata['name']}\n")
    parts.append(f"**Session:** `{metadata['session_id']}`  ")
    parts.append(f"**Directory:** `{metadata['cwd']}`  ")
    if metadata["git_branch"]:
        parts.append(f"**Branch:** `{metadata['git_branch']}`  ")
    parts.append(f"**Created:** {format_timestamp(metadata['created_at'])}  ")
    parts.append(f"**Updated:** {format_timestamp(metadata['updated_at'])}  ")
    resume_cmd = f"cd {shlex.quote(metadata['cwd'])} && claude -r {metadata['session_id']}"
    parts.append(f"**Resume:** `{resume_cmd}`  ")
    parts.append("\n---\n")

    # Conversation turns
    for turn in turns:
        timestamp = format_timestamp(turn["timestamp"])
        if turn["role"] == "user":
            parts.append(f"\n## User\n")
            parts.append(f"*{timestamp}*\n")
            parts.append(f"\n{turn['content']}\n")
        else:
            parts.append(f"\n## Assistant\n")
            parts.append(f"*{timestamp}*\n")
            if turn["content"]:
                parts.append(f"\n{turn['content']}\n")
            if turn["tool_uses"]:
                tools = ", ".join(turn["tool_uses"])
                parts.append(f"\n*Tools used: {tools}*\n")

        parts.append("\n---\n")

    return "\n".join(parts)


def markdown_body(content: str) -> str:
    """Markdown after the header — everything past the first '\n---\n' separator.

    Returns '' if there is no separator (no body, or not generated markdown).
    """
    parts = content.split("\n---\n", 1)
    return parts[1] if len(parts) == 2 else ""


def classify_refresh(existing: str, fresh: str) -> str:
    """Compare a cached markdown file against freshly generated markdown.

    Returns one of:
      'current'  — bodies match; the cached file is up to date.
      'stale'    — the cached body is an exact prefix of the fresh body, so the
                   conversation only grew and the file has no local edits.
      'diverged' — the file was hand-edited, or earlier messages changed.

    The header is excluded from the comparison: it carries volatile fields
    (Updated:/Summary:) that change on every conversation update.
    """
    old, new = markdown_body(existing), markdown_body(fresh)
    if old == new:
        return "current"
    if old and new.startswith(old):
        return "stale"
    return "diverged"


def render_conversation(provider: str, conv_file: Path, fmt: str) -> str:
    """Render a conversation file to markdown or HTML."""
    if provider == "claude-code":
        return claude_code_to_markdown(conv_file)
    with open(conv_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if fmt == "markdown":
        return conversation_to_markdown(data)
    return conversation_to_html(data)


def get_output_path(local_views_dir: Path, uuid: str, provider: str, format: str = "markdown") -> Path:
    """Get output path for the specified format, namespaced by provider."""
    extension = "md" if format == "markdown" else "html"
    provider_dir = local_views_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    return provider_dir / f"{uuid}.{extension}"


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="View a conversation as Markdown or HTML (Claude, ChatGPT, Claude Code).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 01995039-0cb1-74d4-9c55-ce616a54780e
  %(prog)s 5fb4dbea-d189-4bd9-ae4a-b6925a12471d --format html
  %(prog)s c33c89ef-5e51-49e5-8b05-a24e1c8a30af --no-open
        """
    )

    parser.add_argument(
        "uuid",
        help="UUID of the conversation or Claude Code session ID to view"
    )

    parser.add_argument(
        "--format",
        choices=["markdown", "html"],
        default="markdown",
        help="Output format (default: markdown)"
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open the file, just generate it"
    )

    args = parser.parse_args()
    args.uuid = extract_uuid(args.uuid)

    # Get directories
    script_dir = Path(__file__).parent.resolve()

    # Load configuration from .env
    config = {}
    env_file = script_dir / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()

    # Use configured directories or defaults
    data_dir = Path(config.get("DATA_DIR", script_dir / LLM_DATA_SUBDIR)).expanduser()
    local_views_dir = Path(config.get("LOCAL_VIEWS_DIR", script_dir / LOCAL_VIEWS_SUBDIR)).expanduser()

    # Create local_views directory if it doesn't exist
    local_views_dir.mkdir(parents=True, exist_ok=True)

    # Find conversation file — try LLM providers first, then Claude Code
    print(f"Searching for conversation {args.uuid}...")
    result = find_conversation_file(data_dir, args.uuid)

    if not result:
        # Try Claude Code across each configured host source
        import claude_code_parser as ccp
        for _host, cc_data_dir in parse_claude_code_sources(config):
            cc_file = ccp.find_session_file(cc_data_dir, args.uuid)
            if cc_file:
                result = (cc_file, "claude-code")
                break

    if not result:
        print(f"Error: Conversation with UUID {args.uuid} not found.", file=sys.stderr)
        sys.exit(1)

    conv_file, provider = result
    print(f"Found: {conv_file}")

    # Determine output path
    output_path = get_output_path(local_views_dir, args.uuid, provider, args.format)

    # Generate fresh content from the stored conversation.
    try:
        content = render_conversation(provider, conv_file, args.format)
    except Exception as e:
        print(f"Error converting conversation: {e}", file=sys.stderr)
        sys.exit(1)

    # Decide whether to (re)write the cached file.
    write_file = True
    if output_path.exists():
        if args.format != "markdown":
            # HTML keeps its reuse-if-exists behavior.
            print(f"HTML file already exists: {output_path}")
            print("Opening existing file (skipping regeneration)...")
            write_file = False
        else:
            existing = output_path.read_text(encoding="utf-8")
            status = classify_refresh(existing, content)
            if status == "current":
                print("Markdown is up to date — opening existing file.")
                write_file = False
            elif status == "stale":
                print("Conversation has new messages — refreshed the markdown.")
            else:  # diverged
                print("Note: this conversation has newer messages, but the markdown "
                      "file has local edits — opening your edited file unchanged.")
                write_file = False

    if write_file:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Created: {output_path}")
        except Exception as e:
            print(f"Error writing {args.format} file: {e}", file=sys.stderr)
            sys.exit(1)

    # Open file
    if not args.no_open:
        if args.format == "markdown":
            # Open markdown in editor
            editor = os.environ.get("EDITOR", "vim")
            print(f"Opening in {editor}...")
            try:
                subprocess.run([editor, str(output_path)])
            except FileNotFoundError:
                print(f"Error: Editor '{editor}' not found. Set $EDITOR to your preferred editor.", file=sys.stderr)
                print(f"File saved at: {output_path}")
                sys.exit(1)
            except Exception as e:
                print(f"Error opening editor: {e}", file=sys.stderr)
                print(f"File saved at: {output_path}")
                sys.exit(1)
        else:  # html
            # Open HTML in browser
            print(f"Opening in browser...")
            try:
                # Try xdg-open (Linux), open (macOS), or start (Windows)
                if sys.platform.startswith('linux'):
                    subprocess.run(["xdg-open", str(output_path)])
                elif sys.platform == 'darwin':
                    subprocess.run(["open", str(output_path)])
                elif sys.platform == 'win32':
                    subprocess.run(["start", str(output_path)], shell=True)
                else:
                    print(f"Cannot automatically open browser on this platform.", file=sys.stderr)
                    print(f"File saved at: {output_path}")
            except Exception as e:
                print(f"Error opening browser: {e}", file=sys.stderr)
                print(f"File saved at: {output_path}")


if __name__ == "__main__":
    main()
