"""
Unit tests for the source-dispatch registry (scrying_at_home/search/sources.py).

The registry is the single source of truth pairing each ``-s/--source`` token
with its config getter, env-key guard and search functions. These tests pin the
invariants the three call sites (engine.gather_query_results, engine.main,
export.gather_results) rely on.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.config.paths import (
    CLAUDE_CODE_SOURCES_ENV_KEY,
    CODEX_SOURCES_ENV_KEY,
)
from scrying_at_home.search import sources as src


def test_source_choices_is_all_plus_registry_tokens():
    """SOURCE_CHOICES (the argparse choices both CLIs offer) is exactly "all"
    followed by the registry tokens, in registry order."""
    assert src.SOURCE_CHOICES == ["all"] + [d.token for d in src.SOURCE_REGISTRY]
    assert src.SOURCE_CHOICES == ["all", "llm", "claude-code", "codex"]


def test_descriptor_env_keys_match_config_constants():
    """The llm row carries no env key (its source is the always-present data
    dir); the local-CLI rows name their .env keys from config.paths."""
    by_token = {d.token: d for d in src.SOURCE_REGISTRY}
    assert by_token["llm"].env_key is None
    assert by_token["claude-code"].env_key == CLAUDE_CODE_SOURCES_ENV_KEY
    assert by_token["codex"].env_key == CODEX_SOURCES_ENV_KEY


def test_descriptor_callables_present():
    """Every descriptor supplies the three gather callables and a getter."""
    for d in src.SOURCE_REGISTRY:
        assert callable(d.sources_getter)
        assert callable(d.scan)
        assert callable(d.with_index)
        assert callable(d.browse)


def test_sources_getters_resolve_from_config():
    """The llm getter returns the resolved data dir (always truthy); the
    local-CLI getters parse their host=path lists, empty when unset."""
    config = {
        "LLM_DATA_DIR": "/custom/llm_data",
        "CLAUDE_CODE_SOURCES": "laptop=/cc/laptop",
        "CODEX_SOURCES": "laptop=/cx/laptop",
    }
    by_token = {d.token: d for d in src.SOURCE_REGISTRY}
    assert by_token["llm"].sources_getter(config) == Path("/custom/llm_data")
    assert by_token["claude-code"].sources_getter(config) == [("laptop", Path("/cc/laptop"))]
    assert by_token["codex"].sources_getter(config) == [("laptop", Path("/cx/laptop"))]
    # Unconfigured local-CLI sources resolve to an empty (falsy) list.
    assert by_token["claude-code"].sources_getter({}) == []
    assert by_token["codex"].sources_getter({}) == []


def test_browse_passes_matching_index_source(monkeypatch):
    """Each row's browse closure queries the search_index SOURCE_* constant that
    matches its token — the coupling that keeps browse-mode results correct."""
    from scrying_at_home.index import search_index

    seen = []
    monkeypatch.setattr(search_index, "browse_items", lambda conn, source: seen.append(source) or [])
    for d in src.SOURCE_REGISTRY:
        d.browse(object())  # dummy connection; browse_items is stubbed
    assert seen == [search_index.SOURCE_LLM, search_index.SOURCE_CC, search_index.SOURCE_CODEX]


def test_sources_or_error_runs_when_configured():
    """A non-empty sources value means "run this block" — no error, returns True."""
    cc = {d.token: d for d in src.SOURCE_REGISTRY}["claude-code"]
    assert src.sources_or_error(cc, "claude-code", [("laptop", Path("/cc"))]) is True
    assert src.sources_or_error(cc, "all", [("laptop", Path("/cc"))]) is True


def test_sources_or_error_explicit_empty_prints_env_key(capsys):
    """An explicit single-source selection of an unconfigured guarded source
    prints the canonical 'Error: <ENV_KEY> not configured in .env' and skips."""
    by_token = {d.token: d for d in src.SOURCE_REGISTRY}

    assert src.sources_or_error(by_token["claude-code"], "claude-code", []) is False
    err = capsys.readouterr().err
    assert err.strip() == f"Error: {CLAUDE_CODE_SOURCES_ENV_KEY} not configured in .env"

    assert src.sources_or_error(by_token["codex"], "codex", []) is False
    err = capsys.readouterr().err
    assert err.strip() == f"Error: {CODEX_SOURCES_ENV_KEY} not configured in .env"


def test_sources_or_error_all_sweep_empty_is_silent(capsys):
    """Under an 'all' sweep, an unconfigured source is skipped silently (no
    error message) — only an explicit -s of that source is fatal."""
    cc = {d.token: d for d in src.SOURCE_REGISTRY}["claude-code"]
    assert src.sources_or_error(cc, "all", []) is False
    assert capsys.readouterr().err == ""


def test_sources_or_error_llm_never_prints(capsys):
    """The llm row has no env key, so even an explicit empty value (which never
    happens in practice — the data dir is always a Path) prints nothing."""
    llm = {d.token: d for d in src.SOURCE_REGISTRY}["llm"]
    assert src.sources_or_error(llm, "llm", None) is False
    assert capsys.readouterr().err == ""
