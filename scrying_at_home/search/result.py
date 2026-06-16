"""SearchResult assembly, result-list finalization, and the canonical
match-span segmenter.

Extracted from engine.py so the SearchResult shape and the few operations that
were spelled out in parallel across the scan paths and the index paths live in
one place:

  - ``make_result`` builds a SearchResult and assembles the local-CLI
    ``extra={cwd,git_branch,host}`` dict ONCE (it was hand-written in four
    construction sites);
  - ``finalize_results`` applies the per-source recency boost + best-first sort
    tail (copied across four search functions);
  - ``highlight_spans`` is the single segmentation algorithm both the ANSI
    (engine.highlight_query) and prompt_toolkit (picker._highlight_query)
    highlighters map onto, replacing two divergent implementations.

Leaf discipline: this module imports only the stdlib, the providers registry,
and the timestamps leaf. It MUST NOT import engine — engine imports this and
re-exports ``SearchResult``/``Match``/``recency_boost`` so existing
``from ...search.engine import SearchResult`` call sites keep working.

Because the name bonus, recency boost, and SearchResult fields it owns now feed
the scan path's scoring, this module is listed in
search_index.SCHEMA_SOURCE_FILES — an edit here invalidates the FTS index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from scrying_at_home import providers
from scrying_at_home.common.timestamps import parse_iso


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


def make_result(
    *,
    type: str,
    uuid: str,
    name: str,
    created_at: str,
    updated_at: str,
    email: str,
    provider: str,
    filepath: Path,
    matches: List[Match],
    total_score: float,
    model: str = "",
    cwd: str = "",
    git_branch: str = "",
    host: str = "",
) -> SearchResult:
    """Build a SearchResult, assembling the local-CLI ``extra`` dict once.

    For local-CLI providers (claude-code, codex) ``extra`` is
    ``{cwd,git_branch,host}``; for web providers (claude, chatgpt) it stays
    None. The cwd/git_branch/host args are ignored for web providers, so an
    index caller can pass the row's columns unconditionally (they default to
    "" there) and get the same None ``extra`` the scan path produces.
    """
    extra = None
    if providers.is_local_cli(provider):
        extra = {"cwd": cwd, "git_branch": git_branch, "host": host}
    return SearchResult(
        type=type,
        uuid=uuid,
        name=name,
        created_at=created_at,
        updated_at=updated_at,
        email=email,
        provider=provider,
        filepath=filepath,
        matches=matches,
        total_score=total_score,
        extra=extra,
        model=model,
    )


def recency_boost(updated_at: str) -> float:
    """
    Calculate a score boost based on how recently the conversation was updated.

    Returns up to 5 points for conversations updated today, linearly decaying
    to 0 for conversations last updated a year ago or longer.
    """
    # parse_iso coerces a naive timestamp to UTC, so the subtraction below never
    # raises on a stored value that lacks an offset (the old inline parse did,
    # and the bare except silently returned 0.0 — dropping the recency signal).
    updated = parse_iso(updated_at)
    if updated is None:
        return 0.0
    now = datetime.now(timezone.utc)
    days_ago = (now - updated).total_seconds() / 86400
    return max(0.0, 5.0 * (1.0 - days_ago / 365.0))


def finalize_results(results: List[SearchResult], apply_recency_boost: bool) -> List[SearchResult]:
    """Apply the per-source recency boost (when enabled) then sort best-first.

    The boost+sort tail that was copied across search_archive,
    search_claude_code_archive, search_codex_archive, and
    results_from_index_rows. Mutates total_score in place and sorts descending
    by total_score — the most relevant result sorts first (print_results and
    the picker reverse for display so it lands at the bottom of the terminal).

    apply_recency_boost is gated by the caller: browse mode passes False, where
    the score is already the pure recency value set per row and must not be
    boosted a second time. search_item is excluded entirely — it returns a
    single result and has no such tail.
    """
    if apply_recency_boost:
        for result in results:
            result.total_score += recency_boost(result.updated_at)
    results.sort(key=lambda r: -r.total_score)
    return results


def highlight_spans(text: str, query: str, exact: bool) -> List[Tuple[bool, str]]:
    """Segment ``text`` into ``(is_match, chunk)`` spans, query terms flagged.

    The single canonical highlighting algorithm, adopted from the picker: a
    SINGLE alternation regex over all terms, scanned once. This is why it is
    correct where engine's old N-sequential-``re.sub`` passes were not — a
    later pass could re-match markup the earlier pass injected (the escape
    codes contain digits and letters), and overlapping terms could double-wrap.
    A single left-to-right pass over the original text has neither hazard.

    The two highlighters map the spans onto their own markup: engine wraps
    matched chunks in ANSI bright-yellow bold, the picker in the prompt_toolkit
    "fg:ansiyellow bold" style. With no terms (empty/whitespace query) the whole
    text comes back as one non-matching span.
    """
    terms = [query] if exact else [w for w in query.split() if w]
    if not terms:
        return [(False, text)]

    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    spans: List[Tuple[bool, str]] = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            spans.append((False, text[last:m.start()]))
        spans.append((True, m.group()))
        last = m.end()
    if last < len(text):
        spans.append((False, text[last:]))
    return spans
