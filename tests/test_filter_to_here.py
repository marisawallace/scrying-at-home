"""
Unit tests for filter_to_here() — scoping local-CLI results to a directory (and
subdirs) across all hosts — and float_same_host_first(), which ranks this host's
sessions ahead of the rest.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.search import engine as fts
from scrying_at_home.common.ansi import strip_ansi


def _result(provider="claude-code", host="laptop", cwd="/home/me/proj"):
    return fts.SearchResult(
        type="conversation",
        uuid="u",
        name="n",
        created_at="2026-01-01",
        updated_at="2026-01-01",
        email="slug",
        provider=provider,
        filepath=Path("/tmp/x.jsonl"),
        matches=[],
        total_score=1.0,
        extra={"host": host, "cwd": cwd},
    )


def test_keeps_exact_cwd_match():
    r = _result(cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == [r]


def test_keeps_subdirectory():
    r = _result(cwd="/home/me/proj/src/pkg")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == [r]


def test_excludes_sibling_prefix():
    # /home/me/projector must NOT match /home/me/proj
    r = _result(cwd="/home/me/projector")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == []


def test_keeps_other_host():
    # All hosts are kept now; same-host ranking happens in float_same_host_first.
    r = _result(host="desktop", cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == [r]


def test_excludes_non_local_cli():
    # web providers (claude.ai/chatgpt) have no cwd scoping
    r = _result(provider="claude", cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == []


def test_includes_codex():
    # codex is local-cli, so --here scopes it like claude-code
    r = _result(provider="codex", cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == [r]


def test_excludes_parent_directory():
    r = _result(cwd="/home/me")
    assert fts.filter_to_here([r], Path("/home/me/proj")) == []


def test_handles_missing_cwd_gracefully():
    r = _result()
    r.extra = {"host": "laptop"}
    assert fts.filter_to_here([r], Path("/home/me/proj")) == []


# --- float_same_host_first() --------------------------------------------------


def test_floats_same_host_to_front_preserving_order():
    a = _result(host="laptop", cwd="/p/a")
    b = _result(host="desktop", cwd="/p/b")
    c = _result(host="laptop", cwd="/p/c")
    ordered = fts.float_same_host_first([b, a, c], "laptop")
    assert [r.extra["cwd"] for r in ordered] == ["/p/a", "/p/c", "/p/b"]


def test_float_is_stable_when_all_same_host():
    a = _result(host="laptop", cwd="/p/a")
    b = _result(host="laptop", cwd="/p/b")
    ordered = fts.float_same_host_first([a, b], "laptop")
    assert [r.extra["cwd"] for r in ordered] == ["/p/a", "/p/b"]


# --- here_miss_hint() ---------------------------------------------------------


def test_hint_shows_dir_host_and_source():
    hint = strip_ansi(fts.here_miss_hint(Path("/home/me/proj"), "laptop", False, "claude-code"))
    assert "dir:    /home/me/proj" in hint
    assert "host:   laptop (system hostname)" in hint  # host_is_explicit=False
    assert "source: claude-code" in hint


def test_hint_labels_machine_name_when_explicit():
    hint = strip_ansi(fts.here_miss_hint(Path("/srv/explicit"), "laptop", True, "codex"))
    assert "dir:    /srv/explicit" in hint
    assert "host:   laptop (MACHINE_NAME)" in hint  # host_is_explicit=True
    assert "source: codex" in hint


# --- --here argument parsing --------------------------------------------------


def _here_value(argv):
    """Parse argv with build_parser() and return the raw args.here sentinel."""
    return fts.build_parser().parse_args(argv).here


def test_here_absent_is_none():
    assert _here_value(["query"]) is None


def test_here_bare_is_true():
    # A bare --here means "current directory" — represented by the True sentinel.
    assert _here_value(["query", "--here"]) is True


def test_here_with_path_is_the_path():
    assert _here_value(["query", "--here", "/home/me/proj"]) == "/home/me/proj"


# --- resolve_here() -----------------------------------------------------------
# Disambiguates `sy --here ARG` (no positional query): ARG is a real session
# directory -> keep PATH scoping; ARG names no session directory -> it was the
# query the user meant, so reinterpret and scope --here to the cwd.


def test_resolve_here_keeps_path_when_sessions_live_under_it():
    q, here_dir, note = fts.resolve_here(
        "proj", Path("/home/me/proj"), Path("/home/me"), {"/home/me/proj/src"})
    assert q is None
    assert here_dir == Path("/home/me/proj")
    assert note is None


def test_resolve_here_reinterprets_when_no_sessions_under_path():
    q, here_dir, note = fts.resolve_here(
        "images pdf size", Path("/home/me/images pdf size"), Path("/home/me"),
        {"/home/me/other"})
    assert q == "images pdf size"
    assert here_dir == Path("/home/me")
    assert note is not None and "images pdf size" in note


def test_resolve_here_sibling_prefix_does_not_count_as_under():
    # /home/me/projector must NOT satisfy a path of /home/me/proj.
    q, here_dir, note = fts.resolve_here(
        "proj", Path("/home/me/proj"), Path("/home/me"), {"/home/me/projector"})
    assert q == "proj"
    assert here_dir == Path("/home/me")


def test_resolve_here_collision_populated_dir_scopes_not_searches():
    # The known tradeoff: a single-token arg that IS a populated directory scopes
    # to it rather than searching for it. Documented, and surfaced via the note
    # on the reinterpret branch (not this one).
    q, here_dir, note = fts.resolve_here(
        "auth", Path("/home/me/proj/auth"), Path("/home/me/proj"),
        {"/home/me/proj/auth"})
    assert q is None
    assert note is None


# --- is_explicit_here_path() / unescape_here_path() ---------------------------
# A trailing "/" on a single token forces PATH scoping (bypasses resolve_here).


def test_explicit_path_single_token_trailing_slash():
    assert fts.is_explicit_here_path("src/") is True


def test_explicit_path_rejects_no_trailing_slash():
    assert fts.is_explicit_here_path("src") is False


def test_explicit_path_rejects_unescaped_space():
    # "foo src/" is two words that happen to end in "/", so it stays an ambiguous
    # query candidate rather than a forced path.
    assert fts.is_explicit_here_path("foo src/") is False


def test_explicit_path_accepts_escaped_space():
    # "foo\ src/" is one token (escaped space) naming a dir literally "foo src".
    assert fts.is_explicit_here_path("foo\\ src/") is True


def test_explicit_path_allows_internal_slashes():
    assert fts.is_explicit_here_path("a/b/") is True
    assert fts.is_explicit_here_path("/abs/path/") is True


def test_unescape_here_path_drops_backslashes():
    assert fts.unescape_here_path("foo\\ src/") == "foo src/"
    assert fts.unescape_here_path("src/") == "src/"
