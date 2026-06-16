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
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import providers
from paths import (
    MACHINE_NAME_ENV_KEY,
    CLAUDE_CODE_SOURCES_ENV_KEY,
    CODEX_SOURCES_ENV_KEY,
    explicit_host_name,
    load_env_file,
    open_in_editor,
    parse_claude_code_sources,
    parse_codex_sources,
    resolve_data_dir,
    resolve_env_path,
    resolve_local_views_dir,
    resolve_host_name,
    resolve_search_index_path,
    migrate_legacy_index_cache,
)


# Accepted values for the -s/--source filter, single source of truth (shared
# with export_archive). "all" plus one token per searchable source family; a new
# transcript source (e.g. codex) is added here and consumed everywhere choices
# are offered. Distinct from a provider id: "llm" bundles claude + chatgpt.
SOURCE_CHOICES = ["all", "llm", "claude-code", "codex"]


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
    model: str = ""  # Model that produced the conversation (raw provider id; "" if unknown)

    def get_provider_url(self) -> str:
        """Generate provider URL or resume command for this item.

        Thin shim over the leaf providers.provider_url, passing the primitives
        it needs (the registry can't see SearchResult without an import cycle).
        """
        extra = self.extra or {}
        return providers.provider_url(
            self.provider, self.type, self.uuid,
            cwd=extra.get("cwd", "~"), host=extra.get("host", ""),
        )


def score_match(match_lower: str, query_lower: str) -> float:
    """
    Calculate relevance score for a match. Both arguments must already be
    lowercased by the caller.

    Scoring criteria:
    - Exact phrase match: +10
    - All words present: +5
    - Whole word match (per word): +2
    - Partial word match (per word): +1
    - Match in title/name: +5 (handled in search_item)
    """
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


def extract_model_from_chatgpt_conversation(data: dict) -> str:
    """The model that did the work in a ChatGPT conversation: the most-used
    `model_slug` across assistant messages in the mapping. Empty string when
    no assistant message records one."""
    counts: "Counter[str]" = Counter()
    mapping = data.get("mapping", {})
    if not isinstance(mapping, dict):
        return ""
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        if (message.get("author") or {}).get("role") != "assistant":
            continue
        slug = (message.get("metadata") or {}).get("model_slug")
        if slug:
            counts[slug] += 1
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def extract_model_from_claude_conversation(data: dict) -> str:
    """The model behind a claude.ai conversation. Claude.ai's account export
    does not record a per-message model, so this is best-effort: it reads a
    `model` field if a future/variant export carries one, else "" ."""
    for msg in data.get("chat_messages", []):
        if msg.get("sender") == "assistant" and msg.get("model"):
            return msg["model"]
    return data.get("model", "") or ""


def extract_llm_model(data: dict, item_type: str, provider: str) -> str:
    """Dispatch to the provider-specific model extractor. Projects have no
    model. Returns a raw provider model id, or "" when unknown."""
    if item_type != "conversation":
        return ""
    if provider == "chatgpt":
        return extract_model_from_chatgpt_conversation(data)
    return extract_model_from_claude_conversation(data)


def prettify_model(model: str) -> str:
    """Human-friendly label for a raw provider model id, for search output.

    claude-opus-4-8         -> Opus 4.8
    claude-sonnet-4-5-2025… -> Sonnet 4.5
    claude-fable-5          -> Fable 5
    gpt-4o                  -> GPT-4o
    Unknown shapes pass through unchanged. Empty in, empty out."""
    if not model:
        return ""
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?", model)
    if m:
        family, major, minor = m.group(1), m.group(2), m.group(3)
        version = major if minor is None else f"{major}.{minor}"
        return f"{family.capitalize()} {version}"
    if model.startswith("gpt-"):
        return "GPT-" + model[len("gpt-"):]
    return model


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

    # Patterns depend only on the query: compile once, not per matching text.
    query_words = query_lower.split()
    if exact:
        patterns = [re.compile(re.escape(query_lower), re.IGNORECASE)]
    else:
        patterns = [re.compile(re.escape(word), re.IGNORECASE) for word in query_words]

    for text in texts:
        if not text:
            continue

        text_lower = text.lower()

        # Check if query matches
        if exact:
            matches_text = query_lower in text_lower
        else:
            matches_text = all(word in text_lower for word in query_words)

        if matches_text:
            score = score_match(text_lower, query_lower)

            # Extract context around matches (up to 200 chars)
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

    # Extract text based on type and provider. A file that parses as JSON
    # but has the wrong shape (missing uuid/created_at, non-dict top level)
    # is corrupt: warn and skip it, same as unparseable JSON — the index
    # path (search_index._index_llm_file) concludes identically.
    try:
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
            total_score=total_score,
            model=extract_llm_model(data, item_type, provider),
        )
    except (KeyError, TypeError, AttributeError) as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return None


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
                    model=metadata.get("model", ""),
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
        if providers.is_local_cli(row["provider"]):
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
            model=row.get("model", ""),
        ))
    return results


def name_bonus(name_raw: str, query: str, exact: bool) -> float:
    """The +5 title bonus, replicating search_item()/search_claude_code_archive().

    Keyed off the RAW extracted name (empty when the item had no name), so an
    untitled item — stored display name "(untitled)" — never earns the bonus,
    while an item literally titled "(untitled)" does. Both scan paths require a
    truthy name; cc names are always truthy, so this one guard covers both.
    """
    if not query.strip() or not name_raw:
        return 0.0
    name_lower = name_raw.lower()
    query_lower = query.lower()
    if exact:
        hit = query_lower in name_lower
    else:
        hit = all(w in name_lower for w in query_lower.split())
    return 5.0 if hit else 0.0


def make_host_resolver(cc_sources: List[Tuple[str, Path]]):
    """Map a stored cc file path to the host label its source dir carries in the
    CURRENT config, so a host renamed in .env is reflected without a reindex.

    The scan path always derives host from current config; the index froze it at
    index time and only rewrites rows when the file's mtime/size changes. Reading
    the stale stored host would diverge from the scan path and break --here
    filtering. Paths under no configured source keep their stored host.
    """
    dirs = [(Path(d).resolve(), host) for host, d in cc_sources]
    def resolve(path_str: str, stored_host: str) -> str:
        p = Path(path_str).resolve()
        for d, host in dirs:
            if p == d or d in p.parents:
                return host
        return stored_host
    return resolve


def results_from_index_rows(
    rows: List[dict], query: str, exact: bool, apply_recency_boost: bool,
    host_for_path=None,
) -> Tuple[List[SearchResult], set]:
    """Rescore index rows into SearchResults, replicating the scan path exactly.

    Returns (results, fallback_paths). A row whose stored texts are missing
    (LEFT JOIN gave None), unparseable, or not a JSON array is never scored as
    empty: its path joins the fallback set, and main() re-scans that file from
    disk — the safety valve that keeps stored texts a pure accelerator.

    host_for_path, when given, resolves each cc row's host from current config
    (see make_host_resolver) instead of trusting the value frozen in the index.
    """
    results: List[SearchResult] = []
    fallback: set = set()
    for row in rows:
        raw = row["texts"]
        if raw is None:
            fallback.add(row["path"])
            continue
        try:
            texts = json.loads(raw)
        except (ValueError, TypeError):
            fallback.add(row["path"])
            continue
        if not isinstance(texts, list):
            # Valid JSON but the wrong shape (string/dict/number): iterating it
            # in find_matches_in_texts would silently mis-score over characters
            # or keys. Rescue via the real file instead, like a parse failure.
            fallback.add(row["path"])
            continue

        matches = find_matches_in_texts(texts, query, exact=exact)
        if not matches:
            continue  # FTS false positive: filtered exactly as the scan path does

        total_score = sum(m.score for m in matches) + name_bonus(row["name_raw"], query, exact)

        extra = None
        if providers.is_local_cli(row["provider"]):
            host = host_for_path(row["path"], row["host"]) if host_for_path else row["host"]
            extra = {
                "cwd": row["cwd"],
                "git_branch": row["git_branch"],
                "host": host,
            }
        results.append(SearchResult(
            type=row["item_type"],
            uuid=row["uuid"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            email=row["email"],
            provider=row["provider"],
            filepath=Path(row["path"]),
            matches=matches,
            total_score=total_score,
            extra=extra,
            model=row.get("model", ""),
        ))

    if apply_recency_boost:
        for result in results:
            result.total_score += recency_boost(result.updated_at)

    results.sort(key=lambda r: -r.total_score)
    return results, fallback


def _index_rows_for_query(index_conn, source, query, exact):
    """Fetch rescore rows for `source`: FTS-narrowed when the trigram index can
    serve the query, otherwise every searchable file (short-word queries the
    index can't filter). None propagates a db error → caller scans."""
    import search_index
    fts_q = search_index.build_fts_query(query, exact)
    if fts_q is not None:
        return search_index.candidate_rows(index_conn, fts_q, source)
    return search_index.all_searchable_rows(index_conn, source)


def search_llm_with_index(index_conn, data_dir, query, exact, recency):
    """LLM (claude/chatgpt) query results via the index, falling back to a
    scan for the whole source on a db error and for individual files whose
    stored texts are missing/corrupt."""
    rows = _index_rows_for_query(index_conn, "llm", query, exact)
    if rows is None:
        return search_archive(data_dir, query, apply_recency_boost=recency, exact=exact)
    results, fallback = results_from_index_rows(rows, query, exact, recency)
    if fallback:
        results += search_archive(data_dir, query, apply_recency_boost=recency,
                                  exact=exact, candidates=fallback)
    return results


def search_cc_with_index(index_conn, cc_sources, query, exact, recency):
    """Claude Code query results via the index, with the same per-source and
    per-file scan fallbacks as search_llm_with_index."""
    rows = _index_rows_for_query(index_conn, "claude-code", query, exact)
    if rows is None:
        return search_claude_code_archive(cc_sources, query, apply_recency_boost=recency, exact=exact)
    results, fallback = results_from_index_rows(rows, query, exact, recency,
                                                host_for_path=make_host_resolver(cc_sources))
    if fallback:
        results += search_claude_code_archive(cc_sources, query, apply_recency_boost=recency,
                                              exact=exact, candidates=fallback)
    return results


def search_codex_archive(sources: List[Tuple[str, Path]], query: str, apply_recency_boost: bool = True, exact: bool = False, candidates: Optional[set] = None) -> List[SearchResult]:
    """
    Search OpenAI Codex rollout JSONL archives across one or more host-labeled
    source directories.

    Codex's on-disk layout is sessions/YYYY/MM/DD/rollout-*.jsonl with no
    per-project directory (the project identity is the session cwd), so — unlike
    search_claude_code_archive's iterdir+glob — we rglob the rollout files under
    each source root. `candidates` narrows the walk to index-pre-matched files;
    None scans everything.
    """
    import codex_parser as cxp

    results: List[SearchResult] = []

    for host, codex_data_dir in sources:
        if not codex_data_dir.exists():
            print(f"Warning: Codex data directory not found: {codex_data_dir} (host {host})", file=sys.stderr)
            continue

        for jsonl_file in codex_data_dir.rglob("rollout-*.jsonl"):
            if candidates is not None and str(jsonl_file) not in candidates:
                continue
            try:
                lines = cxp.parse_jsonl(jsonl_file)
            except Exception as e:
                print(f"Warning: Could not read {jsonl_file}: {e}", file=sys.stderr)
                continue

            texts = cxp.extract_searchable_text(lines)
            matches = find_matches_in_texts(texts, query, exact=exact)
            if not matches:
                continue

            metadata = cxp.extract_session_metadata(lines)
            total_score = sum(m.score for m in matches) + name_bonus(metadata["name"], query, exact)

            results.append(SearchResult(
                type="conversation",
                uuid=metadata["session_id"],
                name=metadata["name"],
                created_at=metadata["created_at"],
                updated_at=metadata["updated_at"],
                email="",  # codex carries no account/project-slug dir
                provider="codex",
                filepath=jsonl_file,
                matches=matches,
                total_score=total_score,
                model=metadata.get("model", ""),
                extra={
                    "cwd": metadata["cwd"],
                    "git_branch": metadata["git_branch"],
                    "host": host,
                },
            ))

    if apply_recency_boost:
        for result in results:
            result.total_score += recency_boost(result.updated_at)

    results.sort(key=lambda r: -r.total_score)
    return results


def search_codex_with_index(index_conn, codex_sources, query, exact, recency):
    """Codex query results via the index, with the same per-source and per-file
    scan fallbacks as search_cc_with_index."""
    import search_index
    rows = _index_rows_for_query(index_conn, search_index.SOURCE_CODEX, query, exact)
    if rows is None:
        return search_codex_archive(codex_sources, query, apply_recency_boost=recency, exact=exact)
    results, fallback = results_from_index_rows(rows, query, exact, recency,
                                                host_for_path=make_host_resolver(codex_sources))
    if fallback:
        results += search_codex_archive(codex_sources, query, apply_recency_boost=recency,
                                        exact=exact, candidates=fallback)
    return results


def unreadable_files_banner(paths: List[str]) -> str:
    """Loud stderr summary for archive files that could not be indexed.

    Corrupt archive files should never exist, so they warrant manual
    attention — this prints on every run until they are fixed. (A partial
    cloud sync resolves on its own once the file finishes transferring.)
    """
    lines = [f"⚠ {len(paths)} archive file(s) could not be read and are missing from results:"]
    lines += [f"    {p}" for p in paths]
    lines.append("  Fix or remove them; they will be retried on every search.")
    return "\n".join(lines)


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


def gather_codex_tool_counts(sources: List[Tuple[str, Path]]):
    """Sum OpenAI Codex tool invocations across every rollout in the sources.

    Imperative shell around the pure codex_parser helpers; the --stats scan
    fallback for codex, mirroring gather_cc_tool_counts.
    """
    from collections import Counter
    import codex_parser as cxp

    counts: Counter = Counter()
    for _host, codex_data_dir in sources:
        if not codex_data_dir.exists():
            continue
        for jsonl_file in codex_data_dir.rglob("rollout-*.jsonl"):
            try:
                lines = cxp.parse_jsonl(jsonl_file)
            except Exception as e:
                print(f"Warning: Could not read {jsonl_file}: {e}", file=sys.stderr)
                continue
            counts.update(cxp.count_tool_uses(lines))
    return counts


def filter_to_here(results: List[SearchResult], here_dir: Path) -> List[SearchResult]:
    """Keep only local-CLI results (claude-code, codex) whose session cwd is
    `here_dir` or a subdir of it.

    All hosts are kept: the same project directory synced to another machine is
    still "here". Same-host results are floated to the top of the final ordering
    by float_same_host_first() rather than filtered out here.

    Filtering on the recorded session cwd (rather than reconstructing a project
    slug from a directory name) is robust to slug-encoding details. Note:
    extract_session_metadata() records a single cwd per session, so a session
    that cd's into `here_dir` mid-run will not match.
    """
    here_dir = Path(here_dir).resolve()

    def under(p: str) -> bool:
        try:
            return Path(p).resolve().is_relative_to(here_dir)
        except (ValueError, OSError):
            return False

    return [
        r for r in results
        if providers.is_local_cli(r.provider)
        and under((r.extra or {}).get("cwd", ""))
    ]


def parse_uuid_filter(raw: str) -> List[str]:
    """Parse the --uuid value — one UUID or a comma-separated list — into a
    lowercased list, trimming whitespace and dropping empty entries.

    UUIDs are case-insensitive, so lowercasing here lets a pasted upper/mixed
    case id match the lowercase ids stored across every source.
    """
    return [u.strip().lower() for u in raw.split(",") if u.strip()]


def filter_to_uuids(results: List[SearchResult], uuids: set) -> List[SearchResult]:
    """Keep only results whose uuid is in `uuids` (compared case-insensitively).

    Backs --uuid: direct lookup of one or more known conversations, regardless
    of provider. Order is preserved; the caller's later sort decides final
    ordering. An empty intersection returns [], which every output path renders
    as the normal "No results found." message.
    """
    return [r for r in results if r.uuid.lower() in uuids]


def float_same_host_first(results: List[SearchResult], host: str) -> List[SearchResult]:
    """Stable-partition `results` so sessions recorded on `host` come first,
    preserving the existing order within each group.

    Used by --here to rank sessions from this machine above same-directory
    sessions synced from other hosts, regardless of recency or relevance score.
    """
    return sorted(results, key=lambda r: (r.extra or {}).get("host") != host)


def here_miss_hint(here_dir: Path, host: str, host_is_explicit: bool, source: str) -> str:
    """Build a dim, three-line diagnostic shown when --here matched nothing.

    Names the directory --here scoped to, the host whose sessions would have
    ranked first, and the source block that came up empty, so the user can
    eyeball a wrong path or host. `here_dir` is the resolved filter directory
    (the cwd for a bare --here, or the explicit PATH); `source` is the local-CLI
    source that missed ("claude-code" or "codex"), since each block emits its
    own hint. Call only when local-CLI results existed before --here was applied
    but none fell under `here_dir`.
    """
    host_source = MACHINE_NAME_ENV_KEY if host_is_explicit else "system hostname"
    lines = [
        f"dir:    {here_dir}",
        f"host:   {host} ({host_source})",
        f"source: {source}",
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
        # Header: badge label + colour from the provider registry. The
        # type-derived colour (cyan conversation / magenta project) is the
        # fallback used whenever the provider declares no colour override
        # (ansi_color == "") and for unknown providers.
        type_color = Colors.BRIGHT_CYAN if result.type == "conversation" else Colors.BRIGHT_MAGENTA
        p = providers.get(result.provider)
        if p is not None:
            type_label = p.badge_label
            type_color = p.ansi_color or type_color
        else:
            type_label = result.type.upper()

        print(f"{Colors.BOLD}{type_color}[{type_label}]{Colors.RESET} {Colors.BOLD}{result.name}{Colors.RESET}")
        # Skip the UUID line for local-CLI results: the UUID is already visible
        # (and easy to copy) in the `<cli> ... <uuid>` resume command printed below.
        if not providers.is_local_cli(result.provider):
            print(f"{Colors.DIM}UUID: {result.uuid}{Colors.RESET}")
        if providers.is_local_cli(result.provider):
            extra = result.extra or {}
            cwd = extra.get("cwd", "~")
            host = extra.get("host", "")
            host_suffix = f" | {host}" if host else ""
            model_label = prettify_model(result.model)
            model_segment = f" | {model_label}" if model_label else ""
            print(f"{Colors.DIM}Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]}{model_segment}{host_suffix}{Colors.RESET}")
            # Dim the resume command if the result is from a different host —
            # the resume CLI won't find the session locally, so it's not actionable here.
            resume_color = Colors.ORANGE
            if current_host and host and host != current_host:
                resume_color = Colors.DIM
            print(f"{resume_color}pushd {shlex.quote(cwd)} && {providers.resume_shell(result.provider, result.uuid)}{Colors.RESET}")
        else:
            model_label = prettify_model(result.model)
            model_segment = f"{model_label} | " if model_label else ""
            print(f"{Colors.DIM}Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]} | {model_segment}{result.email}{Colors.RESET}")
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


def result_to_entry(result: SearchResult) -> dict:
    """The JSON-serializable dict for one result. Shared by print_json and the
    --verify canonicalizer so both compare the exact published shape."""
    entry = {
        "type": result.type,
        "uuid": result.uuid,
        "name": result.name,
        "created_at": result.created_at,
        "updated_at": result.updated_at,
        "email": result.email,
        "provider": result.provider,
        "model": result.model,
        "url": result.get_provider_url(),
        "filepath": str(result.filepath),
        "total_score": result.total_score,
        "match_count": len(result.matches),
        "matches": [{"text": m.text, "score": m.score} for m in result.matches],
    }
    if result.extra:
        entry["extra"] = result.extra
    return entry


def print_json(results: List[SearchResult]):
    """Print results as JSON."""
    print(json.dumps([result_to_entry(r) for r in results], indent=2, ensure_ascii=False))


def canonical_entries(results: List[SearchResult]) -> List[dict]:
    """Results as serialized entries, ordered by (-total_score, filepath) — the
    one canonical order both pipelines can agree on (filesystem readdir order,
    which breaks score ties on the scan path, is not reconstructable from the
    index). Recency boost must be disabled by the caller so wall-clock skew
    between the two runs can't create float diffs."""
    ordered = sorted(results, key=lambda r: (-r.total_score, str(r.filepath)))
    return [result_to_entry(r) for r in ordered]


def verify_diff(index_entries: List[dict], scan_entries: List[dict]) -> str:
    """Readable field-level diff between two canonicalized entry lists."""
    lines = ["VERIFY FAILED: index and scan results diverge"]
    idx_by = {e["filepath"]: e for e in index_entries}
    scan_by = {e["filepath"]: e for e in scan_entries}
    for p in sorted(set(idx_by) - set(scan_by)):
        lines.append(f"  only in index: {p}")
    for p in sorted(set(scan_by) - set(idx_by)):
        lines.append(f"  only in scan:  {p}")
    for p in sorted(set(idx_by) & set(scan_by)):
        ie, se = idx_by[p], scan_by[p]
        for key in sorted(set(ie) | set(se)):
            if ie.get(key) != se.get(key):
                lines.append(f"  {p}: {key}: index={ie.get(key)!r} scan={se.get(key)!r}")
    return "\n".join(lines)


def gather_query_results(use_index, index_conn, data_dir, cc_sources, query, exact, source,
                         codex_sources=None):
    """Combined query results for the selected sources, recency boost disabled.
    use_index picks the index-backed rescore path or the full scan; --verify
    runs both and compares.

    Sources are table-driven so a new transcript source is one extra row: each
    entry is (-s token, index-backed gather, full-scan gather). A source whose
    backing store is unconfigured (empty sources list) contributes no row.
    """
    query_sources = [
        ("llm",
         lambda: search_llm_with_index(index_conn, data_dir, query, exact, False),
         lambda: search_archive(data_dir, query, apply_recency_boost=False, exact=exact)),
    ]
    if cc_sources:
        query_sources.append(
            ("claude-code",
             lambda: search_cc_with_index(index_conn, cc_sources, query, exact, False),
             lambda: search_claude_code_archive(cc_sources, query, apply_recency_boost=False, exact=exact)))
    if codex_sources:
        query_sources.append(
            ("codex",
             lambda: search_codex_with_index(index_conn, codex_sources, query, exact, False),
             lambda: search_codex_archive(codex_sources, query, apply_recency_boost=False, exact=exact)))

    results: List[SearchResult] = []
    for key, index_gather, scan_gather in query_sources:
        if source in ("all", key):
            results += index_gather() if use_index else scan_gather()
    return results


def run_verify(index_conn, data_dir, cc_sources, query, exact, source, codex_sources=None) -> int:
    """Run a query through both the index and scan pipelines and diff the
    canonicalized results. Returns a process exit code (0 = identical). This is
    the standing proof that the index is a pure accelerator, not a source of
    divergent answers."""
    import search_index
    # Mirror the normal path: an explicit local-CLI search with no sources
    # configured is an error, not a vacuous "VERIFY OK (0 results)".
    if source == "claude-code" and not cc_sources:
        print(f"Error: {CLAUDE_CODE_SOURCES_ENV_KEY} not configured in .env", file=sys.stderr)
        return 1
    if source == "codex" and not codex_sources:
        print(f"Error: {CODEX_SOURCES_ENV_KEY} not configured in .env", file=sys.stderr)
        return 1
    with search_index.read_snapshot(index_conn):
        index_results = gather_query_results(True, index_conn, data_dir, cc_sources, query, exact, source,
                                             codex_sources=codex_sources)
    scan_results = gather_query_results(False, None, data_dir, cc_sources, query, exact, source,
                                        codex_sources=codex_sources)
    index_entries = canonical_entries(index_results)
    scan_entries = canonical_entries(scan_results)
    if index_entries == scan_entries:
        print(f"VERIFY OK ({len(index_entries)} results)")
        return 0
    print(verify_diff(index_entries, scan_entries), file=sys.stderr)
    return 1


def open_results_in_editor(results: List[SearchResult], count: int, config: dict):
    """Open top N results as markdown files in the user's editor / default app."""
    if count > len(results):
        count = len(results)

    if count == 0:
        print("No results to open.")
        return

    # Import view_conversation functions
    script_dir = Path(__file__).parent.resolve()
    sys.path.insert(0, str(script_dir))
    try:
        from view_conversation import render_conversation, get_output_path
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

        # Render to markdown via the shared per-provider dispatcher (handles
        # claude-code/codex transcripts and web conversation/project JSON).
        try:
            markdown_content = render_conversation(
                result.provider, result.filepath, "markdown", result.type)

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

    open_in_editor(*(Path(f) for f in markdown_files))


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser (pure; no parsing or side effects)."""
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
  %(prog)s "archive" -s codex              # search only OpenAI Codex sessions
  %(prog)s "bugfix" --here                  # local-CLI sessions from this dir, any host (this host first)
  %(prog)s --here                           # this dir's local-CLI sessions, newest first
  %(prog)s "bugfix" --here ~/code/proj      # local-CLI sessions from another dir, any host
  %(prog)s --uuid 0199...                   # look up one conversation by UUID
  %(prog)s --uuid 0199...,5fb4...           # look up several conversations by UUID
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
        choices=SOURCE_CHOICES,
        default="all",
        help="Filter by source: all (default), llm (claude.ai/chatgpt), claude-code, codex"
    )

    parser.add_argument(
        "--here",
        nargs="?",
        const=True,
        default=None,
        metavar="PATH",
        help="Only local-CLI sessions (Claude Code, Codex) run from PATH (and subdirs) on any host, with this host's sessions ranked first; PATH defaults to the current directory"
    )

    parser.add_argument(
        "--uuid",
        metavar="UUID[,UUID...]",
        default=None,
        help="Look up conversation(s) by UUID directly: keep only results whose UUID matches one of the comma-separated values"
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

    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run the query through both the index and a full scan and diff the results; prints VERIFY OK or a field-level diff and exits (proves the index is a pure accelerator)"
    )

    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="Path to the .env config file (default: alongside this script)",
    )

    return parser


def main():
    """Main entry point."""
    args = build_parser().parse_args()

    # --stats reports over the whole archive, so it ignores any query and
    # browses every item across the selected source(s).
    if args.stats:
        args.query = None

    # An absent or blank query switches to browse mode: match everything and
    # order strictly by recency.
    query = (args.query or "").strip()
    no_query = not query

    # --here scopes to local-CLI sessions (claude-code + codex) run from a
    # directory, on any host (sessions from this host rank first; see
    # float_same_host_first below). A bare --here uses the current directory;
    # --here PATH overrides it. here_dir is the resolved target (None when --here
    # was not given) and is the single truthiness signal downstream. It is
    # incompatible with the web-only llm source; otherwise it leaves args.source
    # intact ("all" runs both local-CLI blocks, an explicit -s claude-code/codex
    # narrows to one) and the per-block filter_to_here below does the directory
    # scoping.
    here_dir = None
    if args.here is not None:
        if args.here is True:
            here_dir = Path.cwd().resolve()
        else:
            # No existence check: --here filters on each session's recorded cwd,
            # so a path that has since been moved, renamed, or deleted is still a
            # valid filter. A path that matches nothing falls through to
            # here_miss_hint rather than erroring.
            here_dir = Path(args.here).expanduser().resolve()

    if here_dir is not None and args.source == "llm":
        print("Error: --here cannot be combined with -s llm", file=sys.stderr)
        sys.exit(1)

    # --uuid: direct lookup of one or more known conversations. Parsed up front
    # into a set (None when the flag was not given) and applied as a post-gather
    # filter below, so it composes with every source, --here, and any query.
    # Incompatible with --stats, which reports over the whole archive by design.
    if args.stats and args.uuid is not None:
        print("Error: --uuid cannot be combined with --stats", file=sys.stderr)
        sys.exit(1)
    uuid_filter = set(parse_uuid_filter(args.uuid)) if args.uuid is not None else None

    # Get data directory
    script_dir = Path(__file__).parent.resolve()

    # Load configuration from .env (shared parser also handles inline comments
    # and quoted values, unlike the previous inline split).
    env_path = resolve_env_path(script_dir, args.config)
    if args.config and not env_path.is_file():
        print(f"Error: --config file not found: {env_path}", file=sys.stderr)
        sys.exit(1)
    config = load_env_file(env_path)

    data_dir = resolve_data_dir(script_dir, config)

    # Search index: brought up to date on every run (files can arrive via
    # cloud sync with no local process running), then used to narrow query
    # scans to candidate files and to serve browse metadata. Every index
    # failure mode degrades to the full scan with identical results.
    index_conn = None
    if not args.no_index:
        import search_index
        migrate_legacy_index_cache(config)
        index_path = resolve_search_index_path(config)
        if args.reindex:
            search_index.drop_index(index_path)
        index_conn = search_index.open_index(index_path)
        if index_conn is not None:
            failed_files = search_index.refresh(
                index_conn, data_dir, parse_claude_code_sources(config), extract_llm_texts,
                extract_llm_model,
                codex_sources=parse_codex_sources(config),
            )
            if failed_files is None:
                try:
                    index_conn.close()
                except Exception:
                    pass
                index_conn = None
            elif failed_files:
                print(unreadable_files_banner(failed_files), file=sys.stderr)

    # --verify: prove the index path and the full scan agree for this query,
    # then exit. Needs the index (so --no-index is meaningless here) and a query.
    if args.verify:
        if index_conn is None:
            print("Error: --verify requires the search index (drop --no-index).", file=sys.stderr)
            sys.exit(2)
        if not query:
            print("Error: --verify requires a query.", file=sys.stderr)
            sys.exit(2)
        sys.exit(run_verify(index_conn, data_dir, parse_claude_code_sources(config),
                            query, args.exact, args.source,
                            codex_sources=parse_codex_sources(config)))

    index_browse = index_conn is not None and no_query
    index_query = index_conn is not None and bool(query)

    # Perform search across requested sources
    results: List[SearchResult] = []
    # local-CLI sources (claude-code, codex) that had matches before --here but
    # none under here_dir. Reported only if the whole --here search ends empty:
    # a source coming up empty is not a miss worth flagging when a sibling source
    # did match here.
    here_misses: List[str] = []
    recency = not args.no_recency

    current_host = resolve_host_name(config)

    # One WAL snapshot around every index read in this search, so the llm and
    # cc candidate+texts reads can't straddle a concurrent refresh commit.
    from contextlib import nullcontext
    snapshot = search_index.read_snapshot(index_conn) if index_conn is not None else nullcontext()
    with snapshot:
        # --here is web-incompatible, so skip the llm block when it is set.
        if args.source in ("all", "llm") and here_dir is None:
            if index_browse:
                rows = search_index.browse_items(index_conn, search_index.SOURCE_LLM)
                if rows is not None:
                    results.extend(results_from_index_items(rows, apply_recency_boost=recency))
                else:
                    results.extend(search_archive(data_dir, query, apply_recency_boost=recency, exact=args.exact))
            elif index_query:
                results.extend(search_llm_with_index(index_conn, data_dir, query, args.exact, recency))
            else:
                results.extend(search_archive(data_dir, query, apply_recency_boost=recency, exact=args.exact))

        if args.source in ("all", "claude-code"):
            cc_sources = parse_claude_code_sources(config)
            if cc_sources:
                if index_browse:
                    rows = search_index.browse_items(index_conn, search_index.SOURCE_CC)
                    cc_results = (results_from_index_items(rows, apply_recency_boost=recency)
                                  if rows is not None
                                  else search_claude_code_archive(cc_sources, query, apply_recency_boost=recency, exact=args.exact))
                elif index_query:
                    cc_results = search_cc_with_index(index_conn, cc_sources, query, args.exact, recency)
                else:
                    cc_results = search_claude_code_archive(cc_sources, query, apply_recency_boost=recency, exact=args.exact)
                if here_dir is not None:
                    pre_filter = cc_results
                    cc_results = filter_to_here(pre_filter, here_dir)
                    if pre_filter and not cc_results:
                        here_misses.append("claude-code")
                results.extend(cc_results)
            elif args.source == "claude-code":
                print(f"Error: {CLAUDE_CODE_SOURCES_ENV_KEY} not configured in .env", file=sys.stderr)
                sys.exit(1)

        if args.source in ("all", "codex"):
            codex_sources = parse_codex_sources(config)
            if codex_sources:
                if index_browse:
                    rows = search_index.browse_items(index_conn, search_index.SOURCE_CODEX)
                    codex_results = (results_from_index_items(rows, apply_recency_boost=recency)
                                     if rows is not None
                                     else search_codex_archive(codex_sources, query, apply_recency_boost=recency, exact=args.exact))
                elif index_query:
                    codex_results = search_codex_with_index(index_conn, codex_sources, query, args.exact, recency)
                else:
                    codex_results = search_codex_archive(codex_sources, query, apply_recency_boost=recency, exact=args.exact)
                if here_dir is not None:
                    pre_filter = codex_results
                    codex_results = filter_to_here(pre_filter, here_dir)
                    if pre_filter and not codex_results:
                        here_misses.append("codex")
                results.extend(codex_results)
            elif args.source == "codex":
                print(f"Error: {CODEX_SOURCES_ENV_KEY} not configured in .env", file=sys.stderr)
                sys.exit(1)

    # --uuid: narrow the gathered results to the requested conversation(s). A
    # browse over all sources (the no-query default when --uuid is used alone)
    # supplies the candidates; this keeps only the matching UUIDs. An empty
    # result falls through to the normal "No results found." path everywhere.
    if uuid_filter is not None:
        results = filter_to_uuids(results, uuid_filter)

    # --here diagnostics: only when nothing matched here across every local-CLI
    # source (results holds only here-filtered local-CLI items, since --here skips
    # the llm block). One hint per missed source, mirroring the per-source blocks.
    if here_dir is not None and here_misses and not results:
        host_is_explicit = bool(explicit_host_name(config))
        for source in here_misses:
            print(here_miss_hint(here_dir, current_host, host_is_explicit, source), file=sys.stderr)

    # Re-sort combined results by score
    results.sort(key=lambda r: -r.total_score)

    # Re-sort by updated date then score if requested (most recent at bottom).
    # Browse mode (no query) always orders by recency; scores are all ~0 there.
    if no_query:
        results.sort(key=lambda r: r.updated_at, reverse=True)
    elif args.time_sort:
        results.sort(key=lambda r: (r.updated_at, r.total_score), reverse=True)

    # --here keeps every host's sessions for this dir, but floats the ones from
    # this machine to the top. Done as a stable partition after the score/recency
    # sort above (rather than a score bonus) so it also takes effect in browse and
    # --time-sort modes, which order by date and would ignore any score nudge. This
    # covers the json/static/-o paths; the interactive picker re-sorts from scratch
    # and re-applies the float itself.
    if here_dir is not None:
        results = float_same_host_first(results, current_host)

    import demo_mode
    results = demo_mode.maybe_apply(results, config) # No-op unless DEMO_* env vars are set.

    # --stats: render the analytics report over the gathered items and exit
    # before any picker/list output.
    if args.stats:
        import analytics
        tool_counts = None
        want_cc = args.source in ("all", "claude-code")
        want_codex = args.source in ("all", "codex")
        if want_cc or want_codex:
            # Scope the leaderboard to the requested local-CLI source(s) so the
            # index path matches the per-source scan fallback exactly (the
            # cc_tool_counts table now holds both cc and codex rows).
            if index_conn is not None:
                import search_index
                index_sources = []
                if want_cc:
                    index_sources += [search_index.SOURCE_CC, search_index.SOURCE_CC_TOOLS]
                if want_codex:
                    index_sources.append(search_index.SOURCE_CODEX)
                tool_counts = search_index.tool_counts(index_conn, sources=index_sources)
            if tool_counts is None:
                tool_counts = Counter()
                if want_cc:
                    tool_counts.update(gather_cc_tool_counts(parse_claude_code_sources(config)))
                if want_codex:
                    tool_counts.update(gather_codex_tool_counts(parse_codex_sources(config)))
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
            # The picker re-sorts from scratch (by recency or score), so re-apply
            # the --here host float here too — otherwise this-host-first ranking
            # would only survive in the json/static/-o paths, not the picker.
            if here_dir is not None:
                picker_results = float_same_host_first(picker_results, current_host)
            demo = bool(config.get("DEMO_HOSTNAMES", "").strip())
            sys.exit(interactive_picker.pick_and_act(picker_results, query, args.exact, current_host, demo))
    else:
        print_results(results, query, exact=args.exact, current_host=current_host)

    # Open in editor if requested
    if args.open:
        if args.json:
            print("Warning: Cannot use -o/--open with -j/--json", file=sys.stderr)
        else:
            open_results_in_editor(results, args.open, config)


if __name__ == "__main__":
    main()
