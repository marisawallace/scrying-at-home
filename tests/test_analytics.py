"""
Unit tests for analytics.py — the pure aggregation/rendering core behind
`cs --stats`. Everything here is exercised with fabricated SearchResult objects
(no filesystem), mirroring the functional-core / imperative-shell split.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.search import analytics
from scrying_at_home.search import engine as fts


def _result(provider="claude", created_at="2026-01-15T09:30:00Z", extra=None):
    return fts.SearchResult(
        type="conversation",
        uuid="u",
        name="n",
        created_at=created_at,
        updated_at=created_at,
        email="e",
        provider=provider,
        filepath=Path("/tmp/x"),
        matches=[],
        total_score=0.0,
        extra=extra,
    )


# --- parse_dt ---------------------------------------------------------------

def test_parse_dt_handles_z_suffix_and_bare_date():
    assert analytics.parse_dt("2026-01-15T09:30:00Z") is not None
    assert analytics.parse_dt("2026-01-15") is not None


def test_parse_dt_rejects_garbage():
    assert analytics.parse_dt("") is None
    assert analytics.parse_dt("not-a-date") is None
    assert analytics.parse_dt(None) is None


def test_parse_dt_assumes_utc_when_naive():
    dt = analytics.parse_dt("2026-01-15T09:30:00")
    assert dt.tzinfo is not None


# --- bar --------------------------------------------------------------------

def test_bar_zero_is_empty_and_positive_is_visible():
    assert analytics.bar(0, 10) == ""
    assert analytics.bar(1, 1000, width=24) == "█"  # tiny but nonzero -> 1 block
    assert analytics.bar(10, 10, width=24) == "█" * 24


# --- provider / host counts -------------------------------------------------

def test_provider_counts_uses_friendly_labels():
    counts = analytics.provider_counts([_result("claude"), _result("claude-code"), _result("chatgpt")])
    assert counts["claude.ai"] == 1
    assert counts["claude-code"] == 1
    assert counts["chatgpt"] == 1


def test_host_counts_only_claude_code():
    results = [
        _result("claude-code", extra={"host": "laptop"}),
        _result("claude-code", extra={"host": "laptop"}),
        _result("claude-code", extra={"host": "desktop"}),
        _result("claude", extra={"host": "ignored"}),  # non-cc host must be ignored
    ]
    counts = analytics.host_counts(results)
    assert counts == {"laptop": 2, "desktop": 1}


def test_host_counts_includes_codex():
    # Bucket C guard: codex is local-cli, so its host must be counted alongside
    # claude-code (a `provider == "claude-code"` literal here would drop it).
    results = [
        _result("codex", extra={"host": "laptop"}),
        _result("claude-code", extra={"host": "laptop"}),
        _result("chatgpt", extra={"host": "ignored"}),  # web: no host
    ]
    assert analytics.host_counts(results) == {"laptop": 2}


# --- timeline / histograms --------------------------------------------------

def test_monthly_counts_fills_gaps():
    results = [_result(created_at="2026-01-10T00:00:00Z"), _result(created_at="2026-03-10T00:00:00Z")]
    months = analytics.monthly_counts(results)
    assert months == [("2026-01", 1), ("2026-02", 0), ("2026-03", 1)]


def test_hour_histogram_buckets_local_hour():
    # Force a known local hour by using an offset-aware timestamp matching local tz.
    results = [_result(created_at="2026-01-15T09:30:00Z") for _ in range(3)]
    hist = analytics.hour_histogram(results)
    assert sum(hist) == 3
    assert len(hist) == 24


def test_weekday_histogram_length_and_total():
    results = [_result(created_at="2026-06-08T12:00:00Z")]  # a Monday
    hist = analytics.weekday_histogram(results)
    assert len(hist) == 7
    assert sum(hist) == 1


def test_top_directories_abbreviates_home_and_ranks():
    home = str(Path.home())
    results = [
        _result("claude-code", extra={"cwd": f"{home}/repos/a"}),
        _result("claude-code", extra={"cwd": f"{home}/repos/a"}),
        _result("claude-code", extra={"cwd": "/etc/somewhere"}),
    ]
    dirs = analytics.top_directories(results)
    assert dirs[0] == ("~/repos/a", 2)
    assert ("/etc/somewhere", 1) in dirs


def test_top_directories_includes_codex():
    # Bucket C guard: codex session cwds must appear in the directory breakdown.
    results = [
        _result("codex", extra={"cwd": "/work/repo"}),
        _result("claude-code", extra={"cwd": "/work/repo"}),
        _result("claude", extra={"cwd": "/ignored"}),  # web: no cwd grouping
    ]
    dirs = analytics.top_directories(results)
    assert ("/work/repo", 2) in dirs
    assert all(cwd != "/ignored" for cwd, _ in dirs)


def test_busiest_day():
    results = [
        _result(created_at="2026-05-01T01:00:00Z"),
        _result(created_at="2026-05-01T02:00:00Z"),
        _result(created_at="2026-05-02T01:00:00Z"),
    ]
    day, count = analytics.busiest_day(results)
    assert count == 2


# --- report -----------------------------------------------------------------

def test_format_report_empty():
    assert "No conversations" in analytics.format_report([])


def test_tool_leaderboard_orders_descending():
    from collections import Counter
    counts = Counter({"Bash": 10, "Read": 5, "Edit": 7})
    assert analytics.tool_leaderboard(counts, n=2) == [("Bash", 10), ("Edit", 7)]


def test_format_report_tool_section_only_when_counts_given():
    from collections import Counter
    results = [_result("claude-code", extra={"host": "laptop", "cwd": "/x"})]
    assert "tool usage" not in analytics.format_report(results)
    with_tools = analytics.format_report(results, tool_counts=Counter({"Bash": 3}))
    assert "Local-CLI tool usage" in with_tools
    assert "Bash" in with_tools


def test_format_report_includes_sections():
    results = [
        _result("claude", created_at="2026-01-15T09:30:00Z"),
        _result("claude-code", created_at="2026-02-15T22:30:00Z", extra={"host": "laptop", "cwd": "/x"}),
    ]
    report = analytics.format_report(results)
    assert "LLM Archive Analytics" in report
    assert "By provider" in report
    assert "Local-CLI sessions by host" in report
    assert "Activity by month" in report
    assert "Busiest day" in report
