"""
Analytics over a chat archive.

Given the flat list of items the search layer already produces in browse mode
(one SearchResult per conversation/project, across every provider and host),
this module aggregates simple, delightful insights: how many conversations you
have, how they split across providers and machines, when you tend to work, and
which Claude Code directories you live in.

Design: a functional core. Every aggregation and the whole report renderer are
pure functions over a list of SearchResult-shaped objects (they only read
.provider, .type, .created_at, .updated_at, .email and .extra). The imperative
shell lives in full_text_search_chats_archive.py, which gathers the items and
prints format_report().
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Sequence, Tuple

import providers

if TYPE_CHECKING:  # avoid a circular import at runtime
    from full_text_search_chats_archive import SearchResult


# Friendly per-provider labels, derived from the provider registry so this
# layer never hand-maintains its own parallel map. count_by keeps the
# .get(provider, provider) passthrough for any provider not in the registry.
PROVIDER_LABELS = {pid: p.analytics_label for pid, p in providers.all_providers().items()}

# Monday-first, to match datetime.weekday() (Monday == 0).
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def parse_dt(value: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into a tz-aware datetime (UTC if naive).

    Returns None for empty/malformed values so callers can skip them. Accepts a
    bare date ("2026-01-02") as midnight UTC, and tolerates the trailing 'Z'.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to a bare date prefix (handles odd suffixes).
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(dt: datetime) -> datetime:
    """Convert a tz-aware datetime to the machine's local timezone."""
    return dt.astimezone()


def bar(value: int, max_value: int, width: int = 24) -> str:
    """Render a horizontal block bar proportional to value/max_value.

    Always shows at least one block for a positive value so small-but-nonzero
    buckets stay visible. Empty for value <= 0.
    """
    if value <= 0 or max_value <= 0:
        return ""
    filled = max(1, round(width * value / max_value))
    return "█" * filled


def abbreviate_home(path: str) -> str:
    """Replace a leading home directory with ~ for compact display."""
    if not path:
        return path
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


# ---------------------------------------------------------------------------
# Aggregations (pure)
# ---------------------------------------------------------------------------

def count_by(results: Sequence["SearchResult"], key: Callable[["SearchResult"], Optional[str]]) -> Counter:
    """Count results bucketed by key(result), skipping None/empty keys."""
    counter: Counter = Counter()
    for r in results:
        k = key(r)
        if k:
            counter[k] += 1
    return counter


def provider_counts(results: Sequence["SearchResult"]) -> Counter:
    return count_by(results, lambda r: PROVIDER_LABELS.get(r.provider, r.provider))


def host_counts(results: Sequence["SearchResult"]) -> Counter:
    """Conversation counts per Claude Code host (other providers have no host)."""
    return count_by(
        results,
        lambda r: (r.extra or {}).get("host") if r.provider == "claude-code" else None,
    )


def created_dates(results: Sequence["SearchResult"]) -> List[datetime]:
    """Parsed, local-time creation datetimes for every result that has one."""
    out = []
    for r in results:
        dt = parse_dt(r.created_at)
        if dt is not None:
            out.append(to_local(dt))
    return out


def monthly_counts(results: Sequence["SearchResult"]) -> List[Tuple[str, int]]:
    """(YYYY-MM, count) for every month in the span, ascending, gaps filled with 0."""
    dates = created_dates(results)
    if not dates:
        return []
    counter = Counter(d.strftime("%Y-%m") for d in dates)
    start, end = min(dates), max(dates)
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        key = f"{y:04d}-{m:02d}"
        months.append((key, counter.get(key, 0)))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def hour_histogram(results: Sequence["SearchResult"]) -> List[int]:
    """24-bucket histogram of local creation hour."""
    hist = [0] * 24
    for d in created_dates(results):
        hist[d.hour] += 1
    return hist


def weekday_histogram(results: Sequence["SearchResult"]) -> List[int]:
    """7-bucket histogram of local creation weekday (Monday == index 0)."""
    hist = [0] * 7
    for d in created_dates(results):
        hist[d.weekday()] += 1
    return hist


def top_directories(results: Sequence["SearchResult"], n: int = 8) -> List[Tuple[str, int]]:
    """Most frequent Claude Code session directories (home-abbreviated)."""
    counter = count_by(
        results,
        lambda r: (r.extra or {}).get("cwd") if r.provider == "claude-code" else None,
    )
    return [(abbreviate_home(cwd), c) for cwd, c in counter.most_common(n)]


def date_span(results: Sequence["SearchResult"]) -> Optional[Tuple[datetime, datetime]]:
    dates = created_dates(results)
    if not dates:
        return None
    return min(dates), max(dates)


def busiest_day(results: Sequence["SearchResult"]) -> Optional[Tuple[str, int]]:
    dates = created_dates(results)
    if not dates:
        return None
    counter = Counter(d.strftime("%Y-%m-%d") for d in dates)
    day, count = counter.most_common(1)[0]
    return day, count


# ---------------------------------------------------------------------------
# Report rendering (pure)
# ---------------------------------------------------------------------------

def _ranked_block(counter: Counter, total: int, width: int = 20) -> List[str]:
    """Render a counter as aligned `label  count  bar  pct` rows, descending."""
    if not counter:
        return ["  (none)"]
    items = counter.most_common()
    max_count = items[0][1]
    label_w = max(len(label) for label, _ in items)
    lines = []
    for label, count in items:
        pct = (100 * count / total) if total else 0
        lines.append(
            f"  {label.ljust(label_w)}  {count:>5}  {bar(count, max_count, width).ljust(width)}  {pct:4.0f}%"
        )
    return lines


def _monthly_block(months: List[Tuple[str, int]], width: int = 30, tail: int = 12) -> List[str]:
    """Render the most recent `tail` months as count bars."""
    if not months:
        return ["  (none)"]
    recent = months[-tail:]
    max_count = max(c for _, c in recent) or 1
    return [
        f"  {key}  {count:>4}  {bar(count, max_count, width)}"
        for key, count in recent
    ]


def _hour_block(hist: List[int]) -> List[str]:
    """Render the 24-hour histogram as a sparkline with hour ticks."""
    if not any(hist):
        return ["  (none)"]
    blocks = " ▁▂▃▄▅▆▇█"
    peak = max(hist)

    def level(h: int) -> str:
        # Reserve the blank (index 0) for true zeros so a low-but-nonzero hour
        # never looks empty.
        if h <= 0:
            return blocks[0]
        idx = max(1, min(len(blocks) - 1, round((len(blocks) - 1) * h / peak)))
        return blocks[idx]

    spark = "".join(level(h) for h in hist)
    ticks = "".join("|" if hour % 6 == 0 else " " for hour in range(24))
    labels = "0   6   12  18  23"
    return [f"  {spark}", f"  {ticks}", f"  {labels}"]


def _weekday_block(hist: List[int], width: int = 20) -> List[str]:
    if not any(hist):
        return ["  (none)"]
    peak = max(hist)
    return [
        f"  {WEEKDAY_LABELS[i]}  {hist[i]:>5}  {bar(hist[i], peak, width)}"
        for i in range(7)
    ]


def tool_leaderboard(counts: Counter, n: int = 12) -> List[Tuple[str, int]]:
    """Most-invoked Claude Code tools, descending."""
    return counts.most_common(n)


def format_report(
    results: Sequence["SearchResult"],
    width: int = 64,
    tool_counts: Optional[Counter] = None,
) -> str:
    """Render the full analytics report as a plain-text string (pure).

    tool_counts, if given, adds a Claude Code tool-usage leaderboard (the shell
    gathers it by parsing the JSONL transcripts).
    """
    rule = "─" * width

    if not results:
        return "No conversations found to analyze.\n"

    lines: List[str] = []
    lines.append(rule)
    lines.append("  LLM Archive Analytics")
    lines.append(rule)

    span = date_span(results)
    total = len(results)
    if span:
        start, end = span
        days = (end - start).days + 1
        lines.append(f"  {total:,} item(s) spanning {start:%Y-%m-%d} → {end:%Y-%m-%d} ({days:,} days)")
    else:
        lines.append(f"  {total:,} item(s)")
    lines.append("")

    lines.append("  By provider")
    lines += _ranked_block(provider_counts(results), total)
    lines.append("")

    hosts = host_counts(results)
    if hosts:
        cc_total = sum(hosts.values())
        lines.append("  Claude Code by host")
        lines += _ranked_block(hosts, cc_total)
        lines.append("")

    lines.append("  Activity by month (most recent 12)")
    lines += _monthly_block(monthly_counts(results))
    lines.append("")

    lines.append("  Activity by hour of day (local time)")
    lines += _hour_block(hour_histogram(results))
    lines.append("")

    lines.append("  Activity by weekday (local time)")
    lines += _weekday_block(weekday_histogram(results))
    lines.append("")

    dirs = top_directories(results)
    if dirs:
        lines.append("  Top Claude Code directories")
        max_count = dirs[0][1]
        label_w = max(len(d) for d, _ in dirs)
        for d, c in dirs:
            lines.append(f"  {d.ljust(label_w)}  {c:>4}  {bar(c, max_count, 16)}")
        lines.append("")

    if tool_counts:
        leaders = tool_leaderboard(tool_counts)
        total_calls = sum(tool_counts.values())
        lines.append(f"  Claude Code tool usage ({total_calls:,} calls)")
        max_count = leaders[0][1]
        label_w = max(len(name) for name, _ in leaders)
        for name, count in leaders:
            lines.append(f"  {name.ljust(label_w)}  {count:>6}  {bar(count, max_count, 20)}")
        lines.append("")

    busiest = busiest_day(results)
    if busiest:
        day, count = busiest
        lines.append(f"  Busiest day: {day} ({count} item(s))")

    lines.append(rule)
    return "\n".join(lines) + "\n"
