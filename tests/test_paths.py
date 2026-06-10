"""
Unit tests for paths.py path resolution.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import paths


SCRIPT_DIR = Path("/repo")


def test_llm_data_dir_is_honored():
    config = {"LLM_DATA_DIR": "/custom/llm_data"}
    assert paths.resolve_data_dir(SCRIPT_DIR, config) == Path("/custom/llm_data")


def test_defaults_to_data_llm_data_when_unset():
    assert paths.resolve_data_dir(SCRIPT_DIR, {}) == SCRIPT_DIR / "data" / "llm_data"


def test_data_dir_alias_is_honored_with_warning(capsys):
    config = {"DATA_DIR": "/legacy/llm_data"}
    result = paths.resolve_data_dir(SCRIPT_DIR, config)
    assert result == Path("/legacy/llm_data")
    assert "DATA_DIR" in capsys.readouterr().err  # deprecation warning on stderr


def test_llm_data_dir_wins_over_legacy_alias(capsys):
    config = {"LLM_DATA_DIR": "/new/llm_data", "DATA_DIR": "/legacy/llm_data"}
    result = paths.resolve_data_dir(SCRIPT_DIR, config)
    assert result == Path("/new/llm_data")
    assert capsys.readouterr().err == ""  # no warning when new key is set


def test_search_index_db_is_honored():
    config = {"SEARCH_INDEX_DB": "/custom/index.db"}
    assert paths.resolve_search_index_path(config) == Path("/custom/index.db")


def test_search_index_defaults_to_xdg_cache(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg-cache")
    result = paths.resolve_search_index_path({})
    assert result == Path("/xdg-cache/clauding-at-home/index.db")


def test_search_index_defaults_to_home_cache_without_xdg(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    result = paths.resolve_search_index_path({})
    assert result == Path.home() / ".cache" / "clauding-at-home" / "index.db"
