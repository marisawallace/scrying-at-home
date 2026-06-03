"""
Unit tests for filter_to_here() — scoping Claude Code results to the current
directory (and subdirs) on the current host.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import full_text_search_chats_archive as fts


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
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == [r]


def test_keeps_subdirectory():
    r = _result(cwd="/home/me/proj/src/pkg")
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == [r]


def test_excludes_sibling_prefix():
    # /home/me/projector must NOT match /home/me/proj
    r = _result(cwd="/home/me/projector")
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == []


def test_excludes_other_host():
    r = _result(host="desktop", cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == []


def test_excludes_non_claude_code():
    r = _result(provider="claude", cwd="/home/me/proj")
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == []


def test_excludes_parent_directory():
    r = _result(cwd="/home/me")
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == []


def test_handles_missing_cwd_gracefully():
    r = _result()
    r.extra = {"host": "laptop"}
    assert fts.filter_to_here([r], Path("/home/me/proj"), "laptop") == []


# --- here_miss_hint() ---------------------------------------------------------


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_hint_flags_host_mismatch():
    # Results exist on 'desktop', but we're on 'laptop' → host is the culprit.
    results = [_result(host="desktop", cwd="/home/me/proj")]
    hint = _strip_ansi(fts.here_miss_hint(results, Path("/home/me/proj"), "laptop", False))
    assert "'laptop'" in hint
    assert "'desktop'" in hint
    assert "not among the result hosts" in hint
    assert "system hostname" in hint  # host_is_explicit=False
    # The directory line should not claim a directory miss when the host is wrong.
    assert "no session was recorded here" not in hint


def test_hint_flags_directory_miss_when_host_matches():
    # Host matches; the cwd just isn't among recorded sessions.
    results = [_result(host="laptop", cwd="/home/me/other")]
    hint = _strip_ansi(fts.here_miss_hint(results, Path("/home/me/proj"), "laptop", True))
    assert "no session was recorded here" in hint
    assert "not among the result hosts" not in hint
    assert "CLAUDE_CODE_HOST" in hint  # host_is_explicit=True
    assert "/home/me/proj" in hint


def test_hint_reports_count():
    results = [_result(host="desktop"), _result(host="desktop")]
    hint = _strip_ansi(fts.here_miss_hint(results, Path("/home/me/proj"), "laptop", False))
    assert "none of the 2 Claude Code result(s)" in hint
