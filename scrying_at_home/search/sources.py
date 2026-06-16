"""
Source-dispatch registry: the one place that pairs each ``-s/--source`` token
with how to find its data and how to search it.

The search and export CLIs both filter by source (``all``, ``llm``,
``claude-code``, ``codex``). Each token needs the same triple of knowledge
wherever it is consumed: how to resolve its backing store from the config, which
env key configures it (for the "not configured" guard), and which search
functions serve it. Before this module that triple was hand-written in
``engine.gather_query_results``, ``engine.main`` and ``export.gather_results``,
so adding a source meant editing every ladder. Now it is one row here.

Callers iterate ``SOURCE_REGISTRY`` and call the descriptor callable for the
gather mode they need — they do NOT share one gather function, because their
surrounding behavior genuinely differs (main has browse/index/scan modes plus
``--here`` and ``--stats`` gating; export is browse-only; the verify path
disables the recency boost). The registry centralizes only the per-token
pairing and the ``source in ("all", token)`` selection, not the call sites.

``llm`` is the one web source: its "sources" is the llm_data directory (always
present) and it carries no ``*_SOURCES`` env key, so it never hits the
not-configured guard. ``claude-code`` and ``codex`` read host=path source lists
from .env and do — which is also what makes them the local-CLI sources that
``--here`` scopes to (``env_key is not None``).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from scrying_at_home.config.paths import (
    CLAUDE_CODE_SOURCES_ENV_KEY,
    CODEX_SOURCES_ENV_KEY,
    REPO_ROOT,
    parse_claude_code_sources,
    parse_codex_sources,
    resolve_data_dir,
)
from scrying_at_home.search.engine import (
    search_archive,
    search_cc_with_index,
    search_claude_code_archive,
    search_codex_archive,
    search_codex_with_index,
    search_llm_with_index,
)


@dataclass(frozen=True)
class SourceDescriptor:
    """Everything the CLIs need to dispatch one ``-s/--source`` token.

    token          the -s value, e.g. "claude-code" (also an argparse choice).
    sources_getter config -> backing store: the llm_data Path for "llm", a list
                   of (host, Path) for the local-CLI sources. A falsy result
                   (empty list) means the source is unconfigured.
    env_key        the .env key named in the not-configured error, or None for
                   "llm" (which has no env-keyed source and never errors). Its
                   presence also marks the local-CLI sources that ``--here``
                   applies to.
    scan           full-scan search: (sources, query, *, apply_recency_boost,
                   exact, candidates) -> [SearchResult].
    with_index     index-backed search: (index_conn, sources, query, exact,
                   recency) -> [SearchResult].
    browse         index browse-metadata rows for this source: (index_conn) ->
                   rows | None (None signals the caller to fall back to a scan).
    """
    token: str
    sources_getter: Callable[[dict], Any]
    env_key: Optional[str]
    scan: Callable[..., list]
    with_index: Callable[..., list]
    browse: Callable[[Any], Optional[list]]


def _llm_data_dir(config: dict):
    """The llm (claude.ai/chatgpt) source: the resolved llm_data directory. A
    Path is always truthy, so the llm row is never treated as unconfigured."""
    return resolve_data_dir(REPO_ROOT, config)


def _make_browse(source_attr: str) -> Callable[[Any], Optional[list]]:
    """A browse closure that fetches index metadata rows for one index source.

    The search_index import is deferred to call time so importing this module
    (export_archive does, and it never touches the index) does not drag the
    index module in, and so engine can import this module lazily without the
    index loading during engine load. ``source_attr`` names the
    ``search_index.SOURCE_*`` constant rather than copying its value, keeping
    that constant's single source of truth in search_index.
    """
    def browse(index_conn) -> Optional[list]:
        from scrying_at_home.index import search_index
        return search_index.browse_items(index_conn, getattr(search_index, source_attr))
    return browse


# One row per searchable source family. Order is cosmetic — every caller
# re-sorts its combined results — but kept llm, claude-code, codex for reading.
SOURCE_REGISTRY = [
    SourceDescriptor(
        token="llm",
        sources_getter=_llm_data_dir,
        env_key=None,
        scan=search_archive,
        with_index=search_llm_with_index,
        browse=_make_browse("SOURCE_LLM"),
    ),
    SourceDescriptor(
        token="claude-code",
        sources_getter=parse_claude_code_sources,
        env_key=CLAUDE_CODE_SOURCES_ENV_KEY,
        scan=search_claude_code_archive,
        with_index=search_cc_with_index,
        browse=_make_browse("SOURCE_CC"),
    ),
    SourceDescriptor(
        token="codex",
        sources_getter=parse_codex_sources,
        env_key=CODEX_SOURCES_ENV_KEY,
        scan=search_codex_archive,
        with_index=search_codex_with_index,
        browse=_make_browse("SOURCE_CODEX"),
    ),
]


# Accepted values for -s/--source: "all" plus one token per source family.
# Single source of truth shared by the search and export CLIs; derived from the
# registry so a new source row extends the choices automatically. Distinct from
# a provider id: "llm" bundles claude + chatgpt.
SOURCE_CHOICES = ["all"] + [d.token for d in SOURCE_REGISTRY]


def sources_or_error(descriptor: SourceDescriptor, selected: str, sources) -> bool:
    """Whether `descriptor`'s block should run for the chosen ``-s`` value.

    True when `sources` is non-empty. When empty: an explicit single-source
    selection of a guarded source (selected == token and env_key set) prints
    the canonical ``Error: <ENV_KEY> not configured in .env`` and returns False
    — the caller then exits non-zero; an ``all`` sweep (or the unguarded llm
    source) returns False silently. This is the one place that message lives.
    """
    if sources:
        return True
    if selected == descriptor.token and descriptor.env_key:
        print(f"Error: {descriptor.env_key} not configured in .env", file=sys.stderr)
    return False
