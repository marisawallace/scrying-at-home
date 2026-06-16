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
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from scrying_at_home import providers
from scrying_at_home.config.paths import REPO_ROOT, add_config_arg, load_env_or_exit, WEB_EXPORT_SUBDIRS, resolve_data_dir, resolve_local_views_dir, parse_claude_code_sources, parse_codex_sources, open_in_editor
from scrying_at_home.common.timestamps import parse_iso
from scrying_at_home.common.text import normalize_uuid
from scrying_at_home.common.constants import UNTITLED


def find_conversation_file_via_index(config: dict, uuid: str) -> Optional[tuple[Path, str]]:
    """O(1) uuid lookup via the search index, or None to fall back to the
    full-archive scan.

    The index is opened read-only — viewing never builds or refreshes it —
    so the hit is verified against the live file before being trusted.
    """
    import sqlite3

    from scrying_at_home.config.paths import resolve_search_index_path, migrate_legacy_index_cache
    from scrying_at_home.index import search_index

    migrate_legacy_index_cache(config)
    db_path = resolve_search_index_path(config)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        hit = search_index.lookup_uuid(conn, uuid)
    finally:
        conn.close()
    if not hit:
        return None
    path, provider = hit
    try:
        with open(path, "r", encoding="utf-8") as f:
            if json.load(f).get("uuid") == uuid:
                return path, provider
    except Exception:
        pass
    return None


def find_conversation_file(data_dir: Path, uuid: str) -> Optional[tuple[Path, str]]:
    """
    Find the JSON file for a given conversation UUID.

    Returns tuple of (file_path, provider) or None if not found.
    """
    # Walk the web-export tree: data_dir/<provider>/<email>/<subdir>/*.json
    for provider in providers.ingest_dir_providers():
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue

        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue

            for subdir, _item_type in WEB_EXPORT_SUBDIRS:
                item_dir = user_dir / subdir
                if not item_dir.exists():
                    continue
                for item_file in item_dir.glob("*.json"):
                    try:
                        with open(item_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data.get("uuid") == uuid:
                                return item_file, provider
                    except Exception:
                        continue

    return None


def format_timestamp(timestamp_str: str) -> str:
    """Format an ISO timestamp as 'YYYY-MM-DD HH:MM' in local time."""
    dt = parse_iso(timestamp_str)
    if dt is None:
        return timestamp_str
    return dt.astimezone().strftime('%Y-%m-%d %H:%M')


def time_tag(iso: str, cls: str) -> str:
    """An ISO timestamp as a <time> element.

    The raw ISO value is kept in the `datetime` attribute; a small script in
    the generated page reformats the visible text to the viewer's local time.
    The Python-formatted fallback shows if scripting is disabled.
    """
    return (f'<time class="{cls}" datetime="{html.escape(iso or "")}">'
            f'{html.escape(format_timestamp(iso))}</time>')


def attachment_summary(msg: dict) -> Optional[str]:
    """Describe files uploaded to a message as a single non-redundant line.

    Claude's export records uploads twice: ``files`` lists every uploaded item
    (images, documents, pasted text) by uuid, while ``attachments`` holds only
    the subset whose text could be extracted. Counting both double-counts any
    document, so we report one total and surface any real filenames the export
    provides (often blank, e.g. for pasted text).
    """
    attachments = msg.get("attachments") or []
    files = msg.get("files") or []
    count = max(len(attachments), len(files))
    if count == 0:
        return None

    names = sorted({
        item["file_name"]
        for item in (*attachments, *files)
        if item.get("file_name")
    })
    noun = "file" if count == 1 else "files"
    if names:
        return f"{count} {noun}: {', '.join(names)}"
    return f"{count} {noun}"


def conversation_to_markdown(data: dict) -> str:
    """Convert conversation JSON to Markdown format."""
    lines = []

    # Header with metadata
    name = data.get("name") or UNTITLED
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
        summary = attachment_summary(msg)
        if summary:
            lines.append(f"\n*📎 {summary}*\n")

        lines.append("\n---\n")

    return "\n".join(lines)


def project_to_markdown(data: dict) -> str:
    """Convert project JSON (description, prompt template, docs) to Markdown.

    Projects have no chat_messages; their content is the prompt template and
    the uploaded knowledge docs, each rendered as its own section.
    """
    lines = []

    lines.append(f"# {data.get('name') or UNTITLED}\n")
    lines.append(f"**UUID:** `{data.get('uuid', 'unknown')}`  ")
    lines.append(f"**Created:** {format_timestamp(data.get('created_at', ''))}  ")
    lines.append(f"**Updated:** {format_timestamp(data.get('updated_at', ''))}  ")

    if data.get("description"):
        lines.append(f"**Description:** {data['description']}  ")

    lines.append("\n---\n")

    if data.get("prompt_template"):
        lines.append("\n## Prompt template\n")
        lines.append(f"\n{data['prompt_template']}\n")
        lines.append("\n---\n")

    for doc in data.get("docs", []):
        lines.append(f"\n## 📄 {doc.get('filename', '(unnamed doc)')}\n")
        if doc.get("created_at"):
            lines.append(f"*{format_timestamp(doc['created_at'])}*\n")
        if doc.get("content"):
            lines.append(f"\n{doc['content']}\n")
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
    """CSS rules for Pygments-highlighted code blocks.

    Emits a Monokai scheme for the default (dark) theme and a light scheme
    scoped to `[data-theme="light"]`, so highlighting tracks the page theme.
    """
    import vendor_loader  # noqa: F401
    from pygments.formatters import HtmlFormatter

    dark = HtmlFormatter(style="monokai").get_style_defs(".highlight")
    light = HtmlFormatter(style="default").get_style_defs(
        '[data-theme="light"] .highlight')
    return dark + "\n" + light


def ensure_stylesheet(local_views_dir: Path) -> Path:
    """Deploy the shared conversation stylesheet next to the generated pages.

    Combines the repo's source stylesheet with the Pygments highlighting rules
    and writes the result to `<local_views_dir>/assets/conversation.css`, which
    every generated page links. Rewritten on each render so restyling the
    source file propagates to all cached pages without regenerating them.
    """
    source = REPO_ROOT / "assets" / "conversation.css"
    combined = source.read_text(encoding="utf-8") + "\n" + _pygments_css() + "\n"

    assets_dir = local_views_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / "conversation.css"
    target.write_text(combined, encoding="utf-8")
    return target


def render_markdown(text: str) -> str:
    """Render Markdown source text to an HTML fragment."""
    if not text:
        return ""
    return _markdown_renderer()(text)


# Human-facing label for the conversation's source provider, derived from the
# provider registry. Lookups keep a .get(provider, provider) passthrough for
# any provider not in the registry.
_SOURCE_LABELS = {pid: p.source_label for pid, p in providers.all_providers().items()}


def _html_page(title: str, source: str, metadata_html: str,
               messages_html: str) -> str:
    """Assemble a full conversation HTML page from inner fragments.

    `metadata_html` and `messages_html` are HTML fragments placed inside the
    metadata panel and message list. The surrounding shell — head, theme
    bootstrap, topbar, stylesheet link, and the local-time `<time>` reformat
    script — is shared by every provider. `title` and `source` must already
    be HTML-escaped by the caller.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + title + """</title>
    <script>
        // Apply the saved theme before first paint to avoid a flash.
        try {
            document.documentElement.dataset.theme =
                localStorage.getItem("conversation-theme") || "dark";
        } catch (e) {
            document.documentElement.dataset.theme = "dark";
        }
    </script>
    <link rel="stylesheet" href="../assets/conversation.css">
</head>
<body>
    <div class="topbar">
        <div class="topbar-left">
            <span class="topbar-title">""" + title + """</span>
            <span class="topbar-source">""" + source + """</span>
        </div>
        <button class="theme-toggle" onclick="(function(){var d=document.documentElement;var t=d.dataset.theme==='light'?'dark':'light';d.dataset.theme=t;try{localStorage.setItem('conversation-theme',t)}catch(e){}})()">&#9680; theme</button>
    </div>
    <div class="container">
        <div class="metadata">""" + metadata_html + """
        </div>

        <div class="messages">""" + messages_html + """
        </div>
    </div>
    <script>
        // Reformat every <time> element to the viewer's local timezone.
        (function () {
            function pad(n) { return String(n).padStart(2, "0"); }
            function fmt(iso) {
                var d = new Date(iso);
                if (isNaN(d.getTime())) return null;
                return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
                    pad(d.getDate()) + " " + pad(d.getHours()) + ":" +
                    pad(d.getMinutes());
            }
            document.querySelectorAll("time[datetime]").forEach(function (t) {
                var s = fmt(t.getAttribute("datetime"));
                if (s) t.textContent = s;
            });
        })();
    </script>
</body>
</html>"""


def _message_row(role: str, timestamp_iso: str, body_html: str,
                 extra_html: str = "") -> str:
    """One conversation message as a `message-row` block.

    `body_html` is the rendered message content; `extra_html`, if given, is
    appended after the content (used for the attachments / tool-use line).
    """
    timestamp = time_tag(timestamp_iso, "timestamp")
    return f"""
            <div class="message-row {role}">
                <div class="gutter">{timestamp}</div>
                <div class="message">
                    <div class="message-content">{body_html}</div>{extra_html}
                </div>
            </div>"""


def conversation_to_html(data: dict, provider: str = "") -> str:
    """Convert conversation JSON to HTML format with styling."""
    name = html.escape(data.get("name") or UNTITLED)
    source = html.escape(_SOURCE_LABELS.get(provider, provider))
    uuid = html.escape(data.get("uuid", "unknown"))
    created = time_tag(data.get("created_at", ""), "localtime")
    updated = time_tag(data.get("updated_at", ""), "localtime")
    summary = html.escape(data.get("summary", "")) if data.get("summary") else ""

    metadata_parts = [
        f"""
            <div><strong>UUID:</strong> <code>{uuid}</code></div>
            <div><strong>Created:</strong> {created}</div>
            <div><strong>Updated:</strong> {updated}</div>""",
    ]
    if summary:
        metadata_parts.append(f"""
            <div><strong>Summary:</strong> {summary}</div>""")

    messages_parts = []
    for msg in data.get("chat_messages", []):
        sender = msg.get("sender", "unknown")
        role = "user" if sender == "human" else "assistant"

        # Collect the message text plus any distinct text content blocks, then
        # render the whole thing as Markdown so it reads nicely in the browser
        # (headers, lists, code blocks, links, etc.).
        text = msg.get("text", "")
        segments = [text] if text else []
        for content_block in msg.get("content", []):
            if content_block.get("type") == "text":
                block_text = content_block.get("text", "")
                if block_text and block_text != text:
                    segments.append(block_text)

        attachment = attachment_summary(msg)
        extra = ""
        if attachment:
            extra = f"""
                    <div class="attachments">📎 {html.escape(attachment)}</div>"""

        messages_parts.append(_message_row(
            role, msg.get("created_at", ""),
            render_markdown("\n\n".join(segments)), extra))

    return _html_page(name, source, "".join(metadata_parts),
                       "".join(messages_parts))


def project_to_html(data: dict, provider: str = "") -> str:
    """Convert project JSON to HTML format with styling.

    Reuses the conversation page shell: description in the metadata panel,
    then the prompt template and each knowledge doc as its own block.
    """
    name = html.escape(data.get("name") or UNTITLED)
    source = html.escape(_SOURCE_LABELS.get(provider, provider))
    uuid = html.escape(data.get("uuid", "unknown"))
    created = time_tag(data.get("created_at", ""), "localtime")
    updated = time_tag(data.get("updated_at", ""), "localtime")

    metadata_parts = [
        f"""
            <div><strong>UUID:</strong> <code>{uuid}</code></div>
            <div><strong>Created:</strong> {created}</div>
            <div><strong>Updated:</strong> {updated}</div>""",
    ]
    if data.get("description"):
        metadata_parts.append(f"""
            <div><strong>Description:</strong> {html.escape(data['description'])}</div>""")

    doc_parts = []
    if data.get("prompt_template"):
        doc_parts.append(_message_row(
            "user", data.get("created_at", ""),
            "<h2>Prompt template</h2>" + render_markdown(data["prompt_template"])))
    for doc in data.get("docs", []):
        filename = html.escape(doc.get("filename", "(unnamed doc)"))
        doc_parts.append(_message_row(
            "user", doc.get("created_at", ""),
            f"<h2>📄 {filename}</h2>" + render_markdown(doc.get("content", ""))))

    return _html_page(name, source, "".join(metadata_parts), "".join(doc_parts))


def transcript_to_markdown(metadata: dict, turns: list, resume_shell: str) -> str:
    """Render a parsed local-CLI transcript (Claude Code, Codex, …) to Markdown.

    Provider-agnostic: takes the parser output (`metadata` from
    extract_session_metadata, `turns` from extract_conversation_turns) plus the
    bare resume command (e.g. "claude -r <id>"), which is wrapped here in the
    shared `cd <cwd> && …` form. The thin per-provider wrappers below supply
    those by parsing the JSONL with their own parser module.
    """
    parts = []

    # Header
    parts.append(f"# {metadata['name']}\n")
    parts.append(f"**Session:** `{metadata['session_id']}`  ")
    parts.append(f"**Directory:** `{metadata['cwd']}`  ")
    if metadata["git_branch"]:
        parts.append(f"**Branch:** `{metadata['git_branch']}`  ")
    parts.append(f"**Created:** {format_timestamp(metadata['created_at'])}  ")
    parts.append(f"**Updated:** {format_timestamp(metadata['updated_at'])}  ")
    resume_cmd = f"cd {shlex.quote(metadata['cwd'])} && {resume_shell}"
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


def transcript_to_html(metadata: dict, turns: list, source_label: str,
                       resume_shell: str) -> str:
    """Render a parsed local-CLI transcript to HTML (see transcript_to_markdown).

    `source_label` is the raw provider label (escaped here); `resume_shell` is
    the bare resume command, wrapped in the shared `cd <cwd> && …` form.
    """
    name = html.escape(metadata["name"])
    source = html.escape(source_label)
    session = html.escape(metadata["session_id"])
    cwd = html.escape(metadata["cwd"])
    created = time_tag(metadata["created_at"], "localtime")
    updated = time_tag(metadata["updated_at"], "localtime")
    resume_cmd = f"cd {shlex.quote(metadata['cwd'])} && {resume_shell}"

    metadata_parts = [f"""
            <div><strong>Session:</strong> <code>{session}</code></div>
            <div><strong>Directory:</strong> <code>{cwd}</code></div>"""]
    if metadata["git_branch"]:
        branch = html.escape(metadata["git_branch"])
        metadata_parts.append(f"""
            <div><strong>Branch:</strong> <code>{branch}</code></div>""")
    metadata_parts.append(f"""
            <div><strong>Created:</strong> {created}</div>
            <div><strong>Updated:</strong> {updated}</div>
            <div><strong>Resume:</strong> <code>{html.escape(resume_cmd)}</code></div>""")

    messages_parts = []
    for turn in turns:
        extra = ""
        if turn["role"] == "assistant" and turn["tool_uses"]:
            tools = html.escape(", ".join(turn["tool_uses"]))
            extra = f"""
                    <div class="attachments">🔧 Tools used: {tools}</div>"""
        messages_parts.append(_message_row(
            turn["role"], turn["timestamp"],
            render_markdown(turn["content"]), extra))

    return _html_page(name, source, "".join(metadata_parts),
                       "".join(messages_parts))


def claude_code_to_markdown(filepath: Path) -> str:
    """Parse a Claude Code JSONL session and render it to Markdown."""
    from scrying_at_home.parsers import claude_code as ccp

    lines = ccp.parse_jsonl(filepath)
    metadata = ccp.extract_session_metadata(lines)
    turns = ccp.extract_conversation_turns(lines)
    return transcript_to_markdown(
        metadata, turns,
        providers.resume_shell("claude-code", metadata["session_id"]))


def claude_code_to_html(filepath: Path) -> str:
    """Parse a Claude Code JSONL session and render it to HTML."""
    from scrying_at_home.parsers import claude_code as ccp

    lines = ccp.parse_jsonl(filepath)
    metadata = ccp.extract_session_metadata(lines)
    turns = ccp.extract_conversation_turns(lines)
    return transcript_to_html(
        metadata, turns, _SOURCE_LABELS["claude-code"],
        providers.resume_shell("claude-code", metadata["session_id"]))


def codex_to_markdown(filepath: Path) -> str:
    """Parse an OpenAI Codex rollout JSONL session and render it to Markdown."""
    from scrying_at_home.parsers import codex as cxp

    lines = cxp.parse_jsonl(filepath)
    metadata = cxp.extract_session_metadata(lines)
    turns = cxp.extract_conversation_turns(lines)
    return transcript_to_markdown(
        metadata, turns,
        providers.resume_shell("codex", metadata["session_id"]))


def codex_to_html(filepath: Path) -> str:
    """Parse an OpenAI Codex rollout JSONL session and render it to HTML."""
    from scrying_at_home.parsers import codex as cxp

    lines = cxp.parse_jsonl(filepath)
    metadata = cxp.extract_session_metadata(lines)
    turns = cxp.extract_conversation_turns(lines)
    return transcript_to_html(
        metadata, turns, _SOURCE_LABELS["codex"],
        providers.resume_shell("codex", metadata["session_id"]))


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


# Per-provider transcript renderers for the local-CLI sources, as
# (markdown_fn, html_fn). A new local-cli provider plugs in with one row; web
# providers (claude/chatgpt) fall through to the JSON conversation/project path.
_TRANSCRIPT_RENDERERS = {
    "claude-code": (claude_code_to_markdown, claude_code_to_html),
    "codex": (codex_to_markdown, codex_to_html),
}


def render_conversation(provider: str, conv_file: Path, fmt: str,
                        item_type: str = "conversation") -> str:
    """Render a conversation (or claude.ai project) file to markdown or HTML."""
    renderers = _TRANSCRIPT_RENDERERS.get(provider)
    if renderers is not None:
        md_fn, html_fn = renderers
        return html_fn(conv_file) if fmt == "html" else md_fn(conv_file)
    with open(conv_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if item_type == "project":
        if fmt == "markdown":
            return project_to_markdown(data)
        return project_to_html(data, provider)
    if fmt == "markdown":
        return conversation_to_markdown(data)
    return conversation_to_html(data, provider)


def get_output_path(local_views_dir: Path, uuid: str, provider: str, format: str = "markdown") -> Path:
    """Get output path for the specified format, namespaced by provider."""
    extension = "md" if format == "markdown" else "html"
    provider_dir = local_views_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    return provider_dir / f"{uuid}.{extension}"


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="View a conversation as Markdown or HTML (Claude, ChatGPT, Claude Code, OpenAI Codex).",
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
        help="UUID of the conversation or Claude Code / Codex session ID to view"
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

    add_config_arg(parser)

    args = parser.parse_args()
    args.uuid = normalize_uuid(args.uuid)

    # Get directories
    script_dir = REPO_ROOT

    # Load configuration from .env (shared parser handles inline comments and
    # quoted values, unlike the previous inline split).
    config = load_env_or_exit(script_dir, args.config)

    data_dir = resolve_data_dir(script_dir, config)
    local_views_dir = resolve_local_views_dir(script_dir, config)

    # Create local_views directory if it doesn't exist
    local_views_dir.mkdir(parents=True, exist_ok=True)

    # Find conversation file — index lookup first, then LLM provider scan,
    # then Claude Code
    print(f"Searching for conversation {args.uuid}...")
    result = find_conversation_file_via_index(config, args.uuid)
    if not result:
        result = find_conversation_file(data_dir, args.uuid)

    if not result:
        # Try Claude Code across each configured host source
        from scrying_at_home.parsers import claude_code as ccp
        for _host, cc_data_dir in parse_claude_code_sources(config):
            cc_file = ccp.find_session_file(cc_data_dir, args.uuid)
            if cc_file:
                result = (cc_file, "claude-code")
                break

    if not result:
        # Try OpenAI Codex across each configured host source
        from scrying_at_home.parsers import codex as cxp
        for _host, codex_data_dir in parse_codex_sources(config):
            codex_file = cxp.find_session_file(codex_data_dir, args.uuid)
            if codex_file:
                result = (codex_file, "codex")
                break

    if not result:
        print(f"Error: Conversation with UUID {args.uuid} not found.", file=sys.stderr)
        sys.exit(1)

    conv_file, provider = result
    print(f"Found: {conv_file}")

    # Archive layout puts projects under <user>/projects/; everything else
    # (conversations/, Claude Code JSONL) renders as a conversation.
    item_type = "project" if conv_file.parent.name == "projects" else "conversation"

    # Determine output path
    output_path = get_output_path(local_views_dir, args.uuid, provider, args.format)

    # HTML pages link a shared stylesheet — (re)deploy it so restyling the
    # source propagates even to cached pages that aren't regenerated.
    if args.format == "html":
        ensure_stylesheet(local_views_dir)

    # Generate fresh content from the stored conversation.
    try:
        content = render_conversation(provider, conv_file, args.format, item_type)
    except Exception as e:
        print(f"Error converting conversation: {e}", file=sys.stderr)
        sys.exit(1)

    # Decide whether to (re)write the cached file.
    write_file = True
    if output_path.exists():
        if args.format != "markdown":
            # HTML pages aren't hand-edited, so a plain equality check is
            # enough: rewrite when the conversation has changed, otherwise
            # reuse the cached page.
            existing = output_path.read_text(encoding="utf-8")
            if existing == content:
                print("HTML is up to date — opening existing file.")
                write_file = False
            else:
                print("Conversation has changed — refreshed the HTML.")
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
            open_in_editor(output_path)
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
