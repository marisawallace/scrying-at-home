#!/usr/bin/env python3
"""
Full-text search for chat archives.

Searches across all conversations and projects for the specified query,
with colorful terminal output and optional JSON export.
Supports multiple LLM providers (Claude, ChatGPT, etc.).
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from paths import (
    CLAUDE_CODE_HOST_ENV_KEY,
    CLAUDE_CODE_SOURCES_ENV_KEY,
    load_env_file,
    parse_claude_code_sources,
    resolve_data_dir,
    resolve_local_views_dir,
    resolve_host_name,
    resolve_search_index_path,
)


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright variants
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    ORANGE = "\033[38;5;208m"


@dataclass
class Match:
    """Represents a single match within a conversation/project."""
    text: str
    score: float  # Relevance score


@dataclass
class SearchResult:
    """Represents search results for a single conversation or project."""
    type: str  # "conversation" or "project"
    uuid: str
    name: str
    created_at: str
    updated_at: str
    email: str
    provider: str  # "claude", "chatgpt", "claude-code"
    filepath: Path
    matches: List[Match]
    total_score: float
    extra: Optional[dict] = None  # Provider-specific metadata

    def get_provider_url(self) -> str:
        """Generate provider URL or resume command for this item."""
        if self.provider == "claude":
            if self.type == "conversation":
                return f"https://claude.ai/chat/{self.uuid}"
            else:  # project
                return f"https://claude.ai/project/{self.uuid}"
        elif self.provider == "chatgpt":
            # ChatGPT only has conversations, no projects
            return f"https://chatgpt.com/c/{self.uuid}"
        elif self.provider == "claude-code":
            extra = self.extra or {}
            cwd = extra.get("cwd", "~")
            host = extra.get("host", "")
            prefix = f"[{host}] " if host else ""
            return f"{prefix}pushd {shlex.quote(cwd)} && claude -r {self.uuid}"
        else:
            return f"Unknown provider: {self.provider}"


def score_match(match_text: str, query: str) -> float:
    """
    Calculate relevance score for a match.

    Scoring criteria:
    - Exact phrase match: +10
    - All words present: +5
    - Whole word match (per word): +2
    - Partial word match (per word): +1
    - Match in title/name: +5 (handled in search_item)
    """
    match_lower = match_text.lower()
    query_lower = query.lower()

    score = 0.0

    # Exact phrase match
    if query_lower in match_lower:
        score += 10

    # Check individual words
    query_words = query_lower.split()
    words_found = 0

    for word in query_words:
        # Whole word match
        if re.search(r'\b' + re.escape(word) + r'\b', match_lower):
            score += 2
            words_found += 1
        # Partial match
        elif word in match_lower:
            score += 1
            words_found += 1

    # Bonus if all query words are present
    if words_found == len(query_words) and len(query_words) > 1:
        score += 5

    return score


def recency_boost(updated_at: str) -> float:
    """
    Calculate a score boost based on how recently the conversation was updated.

    Returns up to 5 points for conversations updated today, linearly decaying
    to 0 for conversations last updated a year ago or longer.
    """
    try:
        # Parse ISO 8601 timestamp (handle both Z and +00:00 suffixes)
        ts = updated_at.replace("Z", "+00:00")
        updated = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        days_ago = (now - updated).total_seconds() / 86400
        return max(0.0, 5.0 * (1.0 - days_ago / 365.0))
    except (ValueError, TypeError):
        return 0.0


def extract_text_from_conversation(data: dict) -> List[str]:
    """Extract all text content from a conversation."""
    texts = []

    # Add name and summary
    if data.get("name"):
        texts.append(data["name"])
    if data.get("summary"):
        texts.append(data["summary"])

    # Extract from chat messages
    for msg in data.get("chat_messages", []):
        # Add message text
        if msg.get("text"):
            texts.append(msg["text"])

        # Add content blocks
        for content in msg.get("content", []):
            if content.get("text"):
                texts.append(content["text"])

    return texts


def extract_text_from_chatgpt_conversation(data: dict) -> List[str]:
    """Extract all text content from a ChatGPT conversation (mapping format)."""
    texts = []

    # Top-level title (canonical) and name (added by our sync normalizer)
    if data.get("title"):
        texts.append(data["title"])
    if data.get("name") and data.get("name") != data.get("title"):
        texts.append(data["name"])
    if data.get("summary"):
        texts.append(data["summary"])

    mapping = data.get("mapping", {})
    if not isinstance(mapping, dict):
        return texts

    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        # text and multimodal_text both use a `parts` list. parts entries can be
        # strings (text) or dicts (e.g. image references) — keep only strings.
        if content.get("content_type") in ("text", "multimodal_text"):
            for part in content.get("parts", []) or []:
                if isinstance(part, str) and part:
                    texts.append(part)

    return texts


def extract_text_from_project(data: dict) -> List[str]:
    """Extract all text content from a project."""
    texts = []

    # Add name and description
    if data.get("name"):
        texts.append(data["name"])
    if data.get("description"):
        texts.append(data["description"])
    if data.get("prompt_template"):
        texts.append(data["prompt_template"])

    # Extract from docs
    for doc in data.get("docs", []):
        if doc.get("filename"):
            texts.append(doc["filename"])
        if doc.get("content"):
            texts.append(doc["content"])

    return texts


def find_matches_in_texts(texts: List[str], query: str, exact: bool = False) -> List[Match]:
    """
    Search a list of text strings for query matches.

    Returns list of Match objects with context snippets and scores.
    Shared by search_item() and search_claude_code_archive().
    """
    matches: List[Match] = []
    query_lower = query.lower()

    # Browse mode: with no query, every item "matches". Return a single preview
    # snippet from the first non-empty text rather than walking the full text.
    if not query.strip():
        for text in texts:
            if not text:
                continue
            preview = text.replace("\n", " ").strip()
            if len(preview) > 200:
                preview = preview[:200] + "..."
            return [Match(text=preview, score=0.0)]
        return []

    for text in texts:
        if not text:
            continue

        text_lower = text.lower()

        # Check if query matches
        query_words = query_lower.split()
        if exact:
            matches_text = query_lower in text_lower
        else:
            matches_text = all(word in text_lower for word in query_words)

        if matches_text:
            score = score_match(text, query)

            # Extract context around matches (up to 200 chars)
            if exact:
                patterns = [re.compile(re.escape(query_lower), re.IGNORECASE)]
            else:
                patterns = [re.compile(re.escape(word), re.IGNORECASE) for word in query_words]

            for pattern in patterns:
                for match in pattern.finditer(text):
                    start = max(0, match.start() - 100)
                    end = min(len(text), match.end() + 100)
                    context = text[start:end]

                    # Clean up context
                    context = context.replace("\n", " ").strip()
                    if start > 0:
                        context = "..." + context
                    if end < len(text):
                        context = context + "..."

                    matches.append(Match(text=context, score=score))

    return matches


def search_item(filepath: Path, query: str, item_type: str, email: str, provider: str, exact: bool = False) -> Optional[SearchResult]:
    """
    Search a single conversation or project file.

    Returns SearchResult if matches found, None otherwise.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return None

    # Extract text based on type and provider
    texts = extract_llm_texts(data, item_type, provider)

    matches = find_matches_in_texts(texts, query, exact=exact)

    if not matches:
        return None

    # Calculate total score
    total_score = sum(m.score for m in matches)

    # Bonus score if match in name
    name = data.get("name", "")
    name_lower = name.lower() if name else ""
    query_lower = query.lower()
    if exact:
        name_matches = query_lower in name_lower
    else:
        name_matches = all(w in name_lower for w in query_lower.split())
    if query.strip() and name and name_matches:
        total_score += 5

    # Determine updated_at from last message (conversations) or top-level field
    updated_at = data.get("updated_at", data["created_at"])
    if item_type == "conversation":
        messages = data.get("chat_messages", [])
        if messages:
            last_msg_date = messages[-1].get("created_at", "")
            if last_msg_date:
                updated_at = last_msg_date

    return SearchResult(
        type=item_type,
        uuid=data["uuid"],
        name=name if name else "(untitled)",
        created_at=data["created_at"],
        updated_at=updated_at,
        email=email,
        provider=provider,
        filepath=filepath,
        matches=matches,
        total_score=total_score
    )


def extract_llm_texts(data: dict, item_type: str, provider: str) -> List[str]:
    """Dispatch to the provider-specific extractor. Shared with search_index,
    which receives it as a callable so it never has to import this module."""
    if item_type == "conversation":
        if provider == "chatgpt":
            return extract_text_from_chatgpt_conversation(data)
        return extract_text_from_conversation(data)
    return extract_text_from_project(data)


def search_archive(data_dir: Path, query: str, apply_recency_boost: bool = True, exact: bool = False, candidates: Optional[set] = None) -> List[SearchResult]:
    """
    Search all conversations and projects in the archive.

    `candidates` (paths as str) narrows the walk to files the search index
    pre-matched; it is always a superset of the true matches, so skipping
    the rest cannot drop results. None means no index: scan everything.
    """
    results: List[SearchResult] = []

    # Search each user directory
    if not data_dir.exists():
        print(f"Error: Data directory not found: {data_dir}")
        return results

    # Search in both claude/ and chatgpt/ subdirectories
    for provider in ["claude", "chatgpt"]:
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue

        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue

            email = user_dir.name

            # Search conversations
            conversations_dir = user_dir / "conversations"
            if conversations_dir.exists():
                for conv_file in conversations_dir.glob("*.json"):
                    if candidates is not None and str(conv_file) not in candidates:
                        continue
                    result = search_item(conv_file, query, "conversation", email, provider, exact=exact)
                    if result:
                        results.append(result)

            # Search projects
            projects_dir = user_dir / "projects"
            if projects_dir.exists():
                for proj_file in projects_dir.glob("*.json"):
                    if candidates is not None and str(proj_file) not in candidates:
                        continue
                    result = search_item(proj_file, query, "project", email, provider, exact=exact)
                    if result:
                        results.append(result)

    # Apply recency boost to scores
    if apply_recency_boost:
        for result in results:
            result.total_score += recency_boost(result.updated_at)

    # The most relevant results should display at the bottom of the list, right
    # above the new terminal prompt.
    results.sort(key=lambda r: -r.total_score)

    return results


def search_claude_code_archive(sources: List[Tuple[str, Path]], query: str, apply_recency_boost: bool = True, exact: bool = False, candidates: Optional[set] = None) -> List[SearchResult]:
    """
    Search Claude Code JSONL conversation archives across one or more
    host-labeled source directories.

    `candidates` narrows the walk to index-pre-matched files (see
    search_archive); None scans everything.
    """
    import claude_code_parser as ccp

    results: List[SearchResult] = []

    for host, cc_data_dir in sources:
        if not cc_data_dir.exists():
            print(f"Warning: Claude Code data directory not found: {cc_data_dir} (host {host})", file=sys.stderr)
            continue

        for project_dir in cc_data_dir.iterdir():
            if not project_dir.is_dir():
                continue

            project_slug = project_dir.name

            for jsonl_file in project_dir.glob("*.jsonl"):
                if candidates is not None and str(jsonl_file) not in candidates:
                    continue
                try:
                    lines = ccp.parse_jsonl(jsonl_file)
                except Exception as e:
                    print(f"Warning: Could not read {jsonl_file}: {e}", file=sys.stderr)
                    continue

                texts = ccp.extract_searchable_text(lines)
                matches = find_matches_in_texts(texts, query, exact=exact)

                if not matches:
                    continue

                metadata = ccp.extract_session_metadata(lines)
                total_score = sum(m.score for m in matches)

                # Bonus score if match in name
                name = metadata["name"]
                name_lower = name.lower()
                query_lower = query.lower()
                if exact:
                    name_hit = query_lower in name_lower
                else:
                    name_hit = all(w in name_lower for w in query_lower.split())
                if query.strip() and name_hit:
                    total_score += 5

                result = SearchResult(
                    type="conversation",
                    uuid=metadata["session_id"],
                    name=name,
                    created_at=metadata["created_at"],
                    updated_at=metadata["updated_at"],
                    email=project_slug,
                    provider="claude-code",
                    filepath=jsonl_file,
                    matches=matches,
                    total_score=total_score,
                    extra={
                        "cwd": metadata["cwd"],
                        "git_branch": metadata["git_branch"],
                        "host": host,
                    },
                )
                results.append(result)

    # Apply recency boost to scores
    if apply_recency_boost:
        for result in results:
            result.total_score += recency_boost(result.updated_at)

    results.sort(key=lambda r: -r.total_score)
    return results


def results_from_index_items(rows: List[dict], apply_recency_boost: bool = True) -> List[SearchResult]:
    """Reconstruct browse-mode SearchResults from index metadata rows,
    mirroring what search_item()/search_claude_code_archive() produce for an
    empty query: one zero-score preview match, recency boost as the score."""
    results: List[SearchResult] = []
    for row in rows:
        extra = None
        if row["provider"] == "claude-code":
            extra = {
                "cwd": row["cwd"],
                "git_branch": row["git_branch"],
                "host": row["host"],
            }
        score = recency_boost(row["updated_at"]) if apply_recency_boost else 0.0
        results.append(SearchResult(
            type=row["item_type"],
            uuid=row["uuid"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            email=row["email"],
            provider=row["provider"],
            filepath=Path(row["path"]),
            matches=[Match(text=row["preview"], score=0.0)],
            total_score=score,
            extra=extra,
        ))
    return results


def gather_cc_tool_counts(sources: List[Tuple[str, Path]]):
    """Sum Claude Code tool_use invocations across every JSONL in the sources.

    Imperative shell around the pure claude_code_parser helpers; used by --stats.
    """
    from collections import Counter
    import claude_code_parser as ccp

    counts: Counter = Counter()
    for _host, cc_data_dir in sources:
        if not cc_data_dir.exists():
            continue
        for jsonl_file in cc_data_dir.rglob("*.jsonl"):
            try:
                lines = ccp.parse_jsonl(jsonl_file)
            except Exception as e:
                print(f"Warning: Could not read {jsonl_file}: {e}", file=sys.stderr)
                continue
            counts.update(ccp.count_tool_uses(lines))
    return counts


def filter_to_here(results: List[SearchResult], cwd: Path, host: str) -> List[SearchResult]:
    """Keep only claude-code results on `host` whose session cwd is `cwd` or a subdir of it.

    Filtering on the recorded session cwd (rather than reconstructing the Claude Code
    project slug from a directory name) is robust to slug-encoding details. Note:
    extract_session_metadata() records a single cwd per session (its first user line),
    so a session that cd's into `cwd` mid-run will not match.
    """
    cwd = Path(cwd).resolve()

    def under(p: str) -> bool:
        try:
            return Path(p).resolve().is_relative_to(cwd)
        except (ValueError, OSError):
            return False

    return [
        r for r in results
        if r.provider == "claude-code"
        and r.extra.get("host") == host
        and under(r.extra.get("cwd", ""))
    ]


def here_miss_hint(
    pre_filter: List[SearchResult], cwd: Path, host: str, host_is_explicit: bool
) -> str:
    """Build a dim, multi-line diagnostic explaining why --here matched nothing.

    Prints both sides of the host equality test plus the current directory so the
    user can eyeball a mismatch. `pre_filter` is the claude-code result set before
    --here was applied (non-empty); call only when the post-filter set is empty.

    Two failure modes are distinguished:
      - host mismatch: this machine's host name isn't among the result hosts at all
        (common when CLAUDE_CODE_HOST is unset and the hostname doesn't match the
        label in CLAUDE_CODE_SOURCES).
      - directory miss: the host matches but no session was recorded under `cwd`.
    """
    hosts_present = sorted({(r.extra or {}).get("host", "") for r in pre_filter})
    host_source = CLAUDE_CODE_HOST_ENV_KEY if host_is_explicit else "system hostname"
    host_match = host in hosts_present

    lines = [
        f"--here matched none of the {len(pre_filter)} Claude Code result(s) for this query.",
        f"  this host ({host_source}): {host!r}"
        + ("" if host_match else "   ← not among the result hosts below"),
        f"  hosts in results:  {', '.join(repr(h) for h in hosts_present)}",
        f"  current directory: {str(cwd)!r}"
        + ("   ← no session was recorded here" if host_match else ""),
    ]
    return "\n".join(f"{Colors.DIM}{line}{Colors.RESET}" for line in lines)


def highlight_query(text: str, query: str, exact: bool = False) -> str:
    """Highlight query matches in text with color."""
    terms = [query] if exact else query.split()
    for term in terms:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        text = pattern.sub(
            lambda m: f"{Colors.BRIGHT_YELLOW}{Colors.BOLD}{m.group()}{Colors.RESET}",
            text
        )
    return text


def print_results(results: List[SearchResult], query: str, exact: bool = False, current_host: str = ""):
    """Print search results with colorful formatting."""
    if not results:
        print(f"{Colors.RED}No results found.{Colors.RESET}")
        return

    print(f"\n{Colors.BOLD}{Colors.GREEN}Found {len(results)} result(s){Colors.RESET}\n")

    # Reverse to show best results last (most visible at bottom of terminal)
    results.reverse()
    for i, result in enumerate(results, 1):
        # Header
        type_label = result.type.upper()
        type_color = Colors.BRIGHT_CYAN if result.type == "conversation" else Colors.BRIGHT_MAGENTA

        if result.provider == "claude-code":
            type_label = "CLAUDE CODE"
            type_color = Colors.ORANGE

        if result.provider == "chatgpt":
            type_label = "CHATGPT"

        if result.provider == "claude":
            type_label = "CLAUDE.AI"

        if result.provider == "gemini":
            type_label = "GEMINI"

        print(f"{Colors.BOLD}{type_color}[{type_label}]{Colors.RESET} {Colors.BOLD}{result.name}{Colors.RESET}")
        # Skip the UUID line for claude-code results: the UUID is already visible
        # (and easy to copy) in the `claude -r <uuid>` resume command printed below.
        if result.provider != "claude-code":
            print(f"{Colors.DIM}UUID: {result.uuid}{Colors.RESET}")
        if result.provider == "claude-code":
            extra = result.extra or {}
            cwd = extra.get("cwd", "~")
            host = extra.get("host", "")
            host_suffix = f" | {host}" if host else ""
            print(f"{Colors.DIM}Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]}{host_suffix}{Colors.RESET}")
            # Dim the resume command if the result is from a different host —
            # `claude -r` won't find the session locally, so it's not actionable here.
            resume_color = Colors.ORANGE
            if current_host and host and host != current_host:
                resume_color = Colors.DIM
            print(f"{resume_color}pushd {shlex.quote(cwd)} && claude -r {result.uuid}{Colors.RESET}")
        else:
            print(f"{Colors.DIM}Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]} | {result.email}{Colors.RESET}")
            print(f"{Colors.BLUE}{result.get_provider_url()}{Colors.RESET}")
        print(f"{Colors.DIM}Score: {result.total_score:.1f} | Matches: {len(result.matches)}{Colors.RESET}")

        # Show matches (up to 3)
        ceil_matches_to_show = 2
        print()
        for j, match in enumerate(result.matches[:ceil_matches_to_show], 1):
            highlighted = highlight_query(match.text, query, exact=exact)
            print(f"  {Colors.DIM}{j}.{Colors.RESET} {highlighted}")

        if len(result.matches) > ceil_matches_to_show:
            remaining = len(result.matches) - ceil_matches_to_show
            print(f"  {Colors.DIM}... and {remaining} more match(es){Colors.RESET}")

        print()

        # Separator
        if i < len(results):
            print(f"{Colors.DIM}{'─' * 80}{Colors.RESET}\n")


def print_json(results: List[SearchResult]):
    """Print results as JSON."""
    output = []
    for result in results:
        entry = {
            "type": result.type,
            "uuid": result.uuid,
            "name": result.name,
            "created_at": result.created_at,
            "updated_at": result.updated_at,
            "email": result.email,
            "provider": result.provider,
            "url": result.get_provider_url(),
            "filepath": str(result.filepath),
            "total_score": result.total_score,
            "match_count": len(result.matches),
            "matches": [{"text": m.text, "score": m.score} for m in result.matches],
        }
        if result.extra:
            entry["extra"] = result.extra
        output.append(entry)

    print(json.dumps(output, indent=2, ensure_ascii=False))


def open_in_editor(results: List[SearchResult], count: int, config: dict):
    """Open top N results in $EDITOR as markdown files."""
    editor = os.environ.get("EDITOR", "vim")

    if count > len(results):
        count = len(results)

    if count == 0:
        print("No results to open.")
        return

    # Import view_conversation functions
    script_dir = Path(__file__).parent.resolve()
    sys.path.insert(0, str(script_dir))
    try:
        from view_conversation import conversation_to_markdown, claude_code_to_markdown, get_output_path
    except ImportError as e:
        print(f"Error: Could not import view_conversation: {e}", file=sys.stderr)
        sys.exit(1)

    local_views_dir = resolve_local_views_dir(script_dir, config)
    local_views_dir.mkdir(parents=True, exist_ok=True)

    # Generate markdown files for each result
    # Take the last N results (highest scoring) since print_results() reverses the list
    markdown_files = []
    for result in results[-count:][::-1]:
        # Get output path for markdown file
        md_path = get_output_path(local_views_dir, result.uuid, result.provider, "markdown")

        # Check if markdown file already exists
        if md_path.exists():
            print(f"Using existing markdown: {md_path.name}")
            markdown_files.append(str(md_path))
            continue

        # Load conversation data and convert to markdown
        try:
            if result.provider == "claude-code":
                markdown_content = claude_code_to_markdown(result.filepath)
            else:
                with open(result.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                markdown_content = conversation_to_markdown(data)

            # Write markdown file
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(markdown_content)

            print(f"Generated markdown: {md_path.name}")
            markdown_files.append(str(md_path))

        except Exception as e:
            print(f"Warning: Could not convert {result.filepath.name} to markdown: {e}", file=sys.stderr)
            # Fall back to opening the original file
            markdown_files.append(str(result.filepath))

    if not markdown_files:
        print("No files to open.")
        return

    # Open markdown files in editor
    print(f"Opening {len(markdown_files)} file(s) in {editor}...")
    try:
        subprocess.run([editor] + markdown_files)
    except FileNotFoundError:
        print(f"Error: Editor '{editor}' not found. Set $EDITOR to your preferred editor.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error opening editor: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Full-text search for chat archives (Claude, ChatGPT, Claude Code).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                 # browse all results, newest first
  %(prog)s "machine learning"              # find convos containing both words
  %(prog)s -e "machine learning"           # find exact phrase "machine learning"
  %(prog)s "python code" -j > results.json
  %(prog)s "API design" -o 3
  %(prog)s "deployment" -t
  %(prog)s "deployment" -R
  %(prog)s "archive" -s claude-code        # search only Claude Code sessions
  %(prog)s "bugfix" --here                  # Claude Code sessions from this dir on this host
  %(prog)s --here                           # this dir's Claude Code sessions, newest first
        """
    )

    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Search query (case-insensitive). Omit to browse all results, newest first."
    )

    parser.add_argument(
        "-e", "--exact",
        action="store_true",
        help="Search for exact phrase (default: match all words individually)"
    )

    parser.add_argument(
        "-j", "--json",
        action="store_true",
        help="Output results as JSON"
    )

    parser.add_argument(
        "-R", "--no-recency",
        action="store_true",
        help="Disable recency boost (score based on text relevance only)"
    )

    parser.add_argument(
        "-t", "--time-sort",
        action="store_true",
        help="Sort results by updated date then score (most recent at bottom)"
    )

    parser.add_argument(
        "-o", "--open",
        type=int,
        metavar="N",
        help="Open top N results in $EDITOR"
    )

    parser.add_argument(
        "-n", "--no-interactive",
        dest="no_interactive",
        action="store_true",
        help="Print results as a static list instead of the arrow-key picker"
    )

    parser.add_argument(
        "-s", "--source",
        choices=["all", "llm", "claude-code"],
        default="all",
        help="Filter by source: all (default), llm (claude.ai/chatgpt), claude-code"
    )

    parser.add_argument(
        "--here",
        action="store_true",
        help="Only Claude Code sessions run from the current directory (and subdirs) on this host"
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show analytics over the archive (counts, timeline, activity) instead of searching"
    )

    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Bypass the search index and scan every archive file (slow; results are identical)"
    )

    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Discard and rebuild the search index from scratch before searching (use if you suspect it is stale)"
    )

    args = parser.parse_args()

    # --stats reports over the whole archive, so it ignores any query and
    # browses every item across the selected source(s).
    if args.stats:
        args.query = None

    # An absent or blank query switches to browse mode: match everything and
    # order strictly by recency.
    query = (args.query or "").strip()
    no_query = not query

    # --here implies the claude-code source and is incompatible with an explicit non-cc source.
    if args.here:
        if args.source == "llm":
            print("Error: --here cannot be combined with -s llm", file=sys.stderr)
            sys.exit(1)
        args.source = "claude-code"

    # Get data directory
    script_dir = Path(__file__).parent.resolve()

    # Load configuration from .env (shared parser also handles inline comments
    # and quoted values, unlike the previous inline split).
    config = load_env_file(script_dir / ".env")

    data_dir = resolve_data_dir(script_dir, config)

    # Search index: brought up to date on every run (files can arrive via
    # cloud sync with no local process running), then used to narrow query
    # scans to candidate files and to serve browse metadata. Every index
    # failure mode degrades to the full scan with identical results.
    index_conn = None
    if not args.no_index:
        import search_index
        index_path = resolve_search_index_path(config)
        if args.reindex:
            search_index.drop_index(index_path)
        index_conn = search_index.open_index(index_path)
        if index_conn is not None:
            ok = search_index.refresh(
                index_conn, data_dir, parse_claude_code_sources(config), extract_llm_texts
            )
            if not ok:
                try:
                    index_conn.close()
                except Exception:
                    pass
                index_conn = None

    candidates_llm = candidates_cc = None
    if index_conn is not None and query:
        fts_q = search_index.build_fts_query(query, args.exact)
        if fts_q is not None:
            candidates_llm = search_index.candidate_paths(index_conn, fts_q, search_index.SOURCE_LLM)
            candidates_cc = search_index.candidate_paths(index_conn, fts_q, search_index.SOURCE_CC)
    index_browse = index_conn is not None and no_query

    # Perform search across requested sources
    results: List[SearchResult] = []
    recency = not args.no_recency

    current_host = resolve_host_name(config)

    if args.source in ("all", "llm"):
        rows = search_index.browse_items(index_conn, search_index.SOURCE_LLM) if index_browse else None
        if rows is not None:
            results.extend(results_from_index_items(rows, apply_recency_boost=recency))
        else:
            results.extend(search_archive(data_dir, query, apply_recency_boost=recency, exact=args.exact, candidates=candidates_llm))

    if args.source in ("all", "claude-code"):
        cc_sources = parse_claude_code_sources(config)
        if cc_sources:
            rows = search_index.browse_items(index_conn, search_index.SOURCE_CC) if index_browse else None
            if rows is not None:
                cc_results = results_from_index_items(rows, apply_recency_boost=recency)
            else:
                cc_results = search_claude_code_archive(cc_sources, query, apply_recency_boost=recency, exact=args.exact, candidates=candidates_cc)
            if args.here:
                pre_filter = cc_results
                cc_results = filter_to_here(pre_filter, Path.cwd(), current_host)
                if pre_filter and not cc_results:
                    host_is_explicit = bool(config.get(CLAUDE_CODE_HOST_ENV_KEY, "").strip())
                    print(here_miss_hint(pre_filter, Path.cwd(), current_host, host_is_explicit), file=sys.stderr)
            results.extend(cc_results)
        elif args.source == "claude-code":
            print(f"Error: {CLAUDE_CODE_SOURCES_ENV_KEY} not configured in .env", file=sys.stderr)
            sys.exit(1)

    # Re-sort combined results by score
    results.sort(key=lambda r: -r.total_score)

    # Re-sort by updated date then score if requested (most recent at bottom).
    # Browse mode (no query) always orders by recency; scores are all ~0 there.
    if no_query:
        results.sort(key=lambda r: r.updated_at, reverse=True)
    elif args.time_sort:
        results.sort(key=lambda r: (r.updated_at, r.total_score), reverse=True)

    import demo_mode
    results = demo_mode.maybe_apply(results, config) # No-op unless DEMO_* env vars are set.

    # --stats: render the analytics report over the gathered items and exit
    # before any picker/list output.
    if args.stats:
        import analytics
        tool_counts = None
        if args.source in ("all", "claude-code"):
            if index_conn is not None:
                tool_counts = search_index.tool_counts(index_conn)
            if tool_counts is None:
                tool_counts = gather_cc_tool_counts(parse_claude_code_sources(config))
        print(analytics.format_report(results, tool_counts=tool_counts))
        return

    # Interactive picker is the default. Auto-fall-back to the static list when
    # the user asks for JSON, asks to open in $EDITOR, explicitly opts out, or
    # when stdout/stdin isn't a TTY (e.g. piped to `less`, redirected to file).
    use_interactive = not (
        args.no_interactive
        or args.json
        or args.open
        or not sys.stdout.isatty()
        or not sys.stdin.isatty()
    )

    # Output results
    if args.json:
        print_json(results)
    elif use_interactive:
        if not results:
            print(f"{Colors.RED}No results found.{Colors.RESET}")
        else:
            import interactive_picker
            # Browse mode: newest first. Otherwise best result first so the
            # cursor starts on the strongest match.
            if no_query:
                picker_results = sorted(results, key=lambda r: r.updated_at, reverse=True)
            else:
                picker_results = sorted(results, key=lambda r: -r.total_score)
            demo = bool(config.get("DEMO_HOSTNAMES", "").strip())
            sys.exit(interactive_picker.pick_and_act(picker_results, query, args.exact, current_host, demo))
    else:
        print_results(results, query, exact=args.exact, current_host=current_host)

    # Open in editor if requested
    if args.open:
        if args.json:
            print("Warning: Cannot use -o/--open with -j/--json", file=sys.stderr)
        else:
            open_in_editor(results, args.open, config)


if __name__ == "__main__":
    main()
