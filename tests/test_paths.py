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


def test_legacy_data_dir_raises():
    import pytest

    config = {"DATA_DIR": "/legacy/llm_data"}
    with pytest.raises(SystemExit) as exc:
        paths.resolve_data_dir(SCRIPT_DIR, config)
    assert "LLM_DATA_DIR" in str(exc.value)


def test_legacy_data_dir_raises_even_with_llm_data_dir_set():
    import pytest

    config = {"LLM_DATA_DIR": "/new/llm_data", "DATA_DIR": "/legacy/llm_data"}
    with pytest.raises(SystemExit):
        paths.resolve_data_dir(SCRIPT_DIR, config)


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


def test_resolve_env_path_defaults_to_script_dir_dot_env():
    assert paths.resolve_env_path(SCRIPT_DIR, None) == SCRIPT_DIR / ".env"


def test_resolve_env_path_blank_config_falls_back_to_default():
    # argparse default and an empty string both mean "no explicit config".
    assert paths.resolve_env_path(SCRIPT_DIR, "") == SCRIPT_DIR / ".env"


def test_resolve_env_path_explicit_config_is_used():
    assert paths.resolve_env_path(SCRIPT_DIR, "/custom/profile.env") == Path("/custom/profile.env")


def test_resolve_env_path_expands_user():
    assert paths.resolve_env_path(SCRIPT_DIR, "~/cfg/.env") == Path.home() / "cfg" / ".env"
