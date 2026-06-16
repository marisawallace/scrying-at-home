"""
Unit tests for paths.py path resolution.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import scrying_at_home.config.paths as paths


SCRIPT_DIR = Path("/repo")


def test_codex_sources_parsed_to_host_path_pairs():
    config = {"CODEX_SOURCES": "laptop=/data/codex/laptop,desktop=~/codex/desktop"}
    assert paths.parse_codex_sources(config) == [
        ("laptop", Path("/data/codex/laptop")),
        ("desktop", Path("~/codex/desktop").expanduser()),
    ]


def test_codex_sources_empty_when_unset():
    assert paths.parse_codex_sources({}) == []
    assert paths.parse_codex_sources({"CODEX_SOURCES": ""}) == []


def test_codex_sources_malformed_entry_names_codex_key():
    import pytest

    with pytest.raises(ValueError) as exc:
        paths.parse_codex_sources({"CODEX_SOURCES": "no-equals-sign"})
    assert "CODEX_SOURCES" in str(exc.value)


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
    assert result == Path("/xdg-cache/scrying-at-home/index.db")


def test_search_index_defaults_to_home_cache_without_xdg(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    result = paths.resolve_search_index_path({})
    assert result == Path.home() / ".cache" / "scrying-at-home" / "index.db"


def test_migrate_legacy_index_cache_moves_old_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    old_dir = tmp_path / "clauding-at-home"
    old_dir.mkdir()
    (old_dir / "index.db").write_text("data")

    paths.migrate_legacy_index_cache({})

    new_dir = tmp_path / "scrying-at-home"
    assert not old_dir.exists()
    assert (new_dir / "index.db").read_text() == "data"


def test_migrate_legacy_index_cache_noop_when_new_dir_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    old_dir = tmp_path / "clauding-at-home"
    old_dir.mkdir()
    (old_dir / "index.db").write_text("old")
    new_dir = tmp_path / "scrying-at-home"
    new_dir.mkdir()

    paths.migrate_legacy_index_cache({})

    # Existing new dir is left untouched; old dir is not clobbered.
    assert old_dir.exists()
    assert not (new_dir / "index.db").exists()


def test_migrate_legacy_index_cache_noop_with_custom_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    old_dir = tmp_path / "clauding-at-home"
    old_dir.mkdir()
    (old_dir / "index.db").write_text("old")

    paths.migrate_legacy_index_cache({"SEARCH_INDEX_DB": str(tmp_path / "custom.db")})

    assert old_dir.exists()
    assert not (tmp_path / "scrying-at-home").exists()


def test_resolve_env_path_defaults_to_script_dir_dot_env():
    assert paths.resolve_env_path(SCRIPT_DIR, None) == SCRIPT_DIR / ".env"


def test_resolve_env_path_blank_config_falls_back_to_default():
    # argparse default and an empty string both mean "no explicit config".
    assert paths.resolve_env_path(SCRIPT_DIR, "") == SCRIPT_DIR / ".env"


def test_resolve_env_path_explicit_config_is_used():
    assert paths.resolve_env_path(SCRIPT_DIR, "/custom/profile.env") == Path("/custom/profile.env")


def test_resolve_env_path_expands_user():
    assert paths.resolve_env_path(SCRIPT_DIR, "~/cfg/.env") == Path.home() / "cfg" / ".env"


# --- machine name resolution (MACHINE_NAME, legacy CLAUDE_CODE_HOST) --------

def test_explicit_host_name_prefers_machine_name():
    assert paths.explicit_host_name({"MACHINE_NAME": "laptop"}) == "laptop"


def test_explicit_host_name_falls_back_to_legacy_key():
    assert paths.explicit_host_name({"CLAUDE_CODE_HOST": "legacybox"}) == "legacybox"


def test_explicit_host_name_machine_name_wins_over_legacy():
    config = {"MACHINE_NAME": "new", "CLAUDE_CODE_HOST": "old"}
    assert paths.explicit_host_name(config) == "new"


def test_explicit_host_name_empty_when_unset():
    assert paths.explicit_host_name({}) == ""
    assert paths.explicit_host_name({"MACHINE_NAME": "  "}) == ""


def test_resolve_host_name_uses_explicit_machine_name():
    assert paths.resolve_host_name({"MACHINE_NAME": "laptop"}) == "laptop"


def test_resolve_host_name_falls_back_to_gethostname(monkeypatch):
    monkeypatch.setattr(paths.socket, "gethostname", lambda: "Box.local")
    assert paths.resolve_host_name({}) == "box"  # normalized: lowercased, .local stripped


# --- remove_env_key ---------------------------------------------------------

def test_remove_env_key_drops_active_assignment():
    text = "A=1\nCLAUDE_CODE_HOST=box\nB=2\n"
    assert paths.remove_env_key(text, "CLAUDE_CODE_HOST") == "A=1\nB=2\n"


def test_remove_env_key_preserves_commented_lines():
    text = "# CLAUDE_CODE_HOST=doc\nA=1\n"
    assert paths.remove_env_key(text, "CLAUDE_CODE_HOST") == text


def test_remove_env_key_absent_is_noop():
    text = "A=1\nB=2\n"
    assert paths.remove_env_key(text, "CLAUDE_CODE_HOST") == text


def test_remove_env_key_empties_file_when_only_key():
    assert paths.remove_env_key("CLAUDE_CODE_HOST=box\n", "CLAUDE_CODE_HOST") == ""
