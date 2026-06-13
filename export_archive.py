#!/usr/bin/env python3
"""
Bulk-export the whole chat archive to a tree of Markdown (or HTML) files.

Why: JSON/JSONL is the durable source of truth, but a flat folder of dated
Markdown is portable, grep-able, and drops straight into Obsidian or any note
app. This walks every conversation across every provider/host and renders it
with the same engine `view_conversation.py` uses for single conversations.

Layout:
    OUTPUT/
      index.md                         # links to everything, grouped
      claude/user@example.com/conversations/2026-01-02_Title.md
      claude/user@example.com/projects/2026-01-02_Title.md
      chatgpt/user@example.com/conversations/2026-01-02_Title.md
      claude-code/<host>/conversations/2026-01-02_first-prompt.md

Usage:
    python export_archive.py [OUTPUT_DIR] [-s SOURCE] [--format md|html] [--dry-run]

Design: a functional core. plan_exports() and build_index() are pure — they map
the enumerated items to unique relative paths and an index document with no I/O.
The imperative shell (run_export / main) gathers items and writes files.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import List, Sequence, Tuple

import providers
from paths import load_env_file, parse_claude_code_sources, resolve_data_dir, resolve_env_path
from full_text_search_chats_archive import (
    SOURCE_CHOICES,
    SearchResult,
    search_archive,
    search_claude_code_archive,
)
from sync_local_chats_archive import build_filename
from view_conversation import render_conversation

# Friendly per-provider labels for the index headings, derived from the
# provider registry (shared with analytics.PROVIDER_LABELS). build_index keeps
# the .get(provider, provider) passthrough for any unregistered provider.
PROVIDER_LABELS = {pid: p.analytics_label for pid, p in providers.all_providers().items()}


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def export_group(result: SearchResult) -> str:
    """Second path component: the account email, or the host for Claude Code."""
    if result.provider == "claude-code":
        return (result.extra or {}).get("host") or "unknown-host"
    return result.email or "unknown"


def plan_exports(
    results: Sequence[SearchResult], extension: str = "md"
) -> List[Tuple[SearchResult, Path]]:
    """Assign each result a unique relative output path (pure, deterministic).

    Files are named `<YYYY-MM-DD>_<sanitized-name>.<ext>` under
    `<provider>/<group>/<conversations|projects>/`. Collisions (same date +
    name in a directory) get a numeric suffix, resolved in a stable order so
    reruns are reproducible.
    """
    ordered = sorted(
        results, key=lambda r: (r.provider, export_group(r), r.created_at, r.uuid)
    )
    seen = set()
    planned: List[Tuple[SearchResult, Path]] = []
    for r in ordered:
        rel_dir = Path(r.provider) / export_group(r) / f"{r.type}s"
        stem = build_filename(r.created_at, r.name)  # YYYY-MM-DD_sanitized
        candidate = rel_dir / f"{stem}.{extension}"
        i = 2
        while candidate in seen:
            candidate = rel_dir / f"{stem}-{i}.{extension}"
            i += 1
        seen.add(candidate)
        planned.append((r, candidate))
    return planned


def build_index(planned: Sequence[Tuple[SearchResult, Path]], today: str = "") -> str:
    """Render index.md: every successfully written export linked, grouped by
    provider then account. Callers must pass only entries that exist on disk,
    or the index will contain dead links.

    Links are relative to the index at the export root, so the tree is portable.
    """
    today = today or date.today().isoformat()
    lines = ["# LLM Archive Export", "", f"_{len(planned)} item(s) · generated {today}_", ""]

    # Group by (provider, group) preserving the planned order, newest first within.
    groups: dict[Tuple[str, str], List[Tuple[SearchResult, Path]]] = {}
    for r, rel in planned:
        groups.setdefault((r.provider, export_group(r)), []).append((r, rel))

    for (provider, group) in sorted(groups):
        label = PROVIDER_LABELS.get(provider, provider)
        lines.append(f"## {label} / {group}")
        lines.append("")
        entries = sorted(groups[(provider, group)], key=lambda pr: pr[0].created_at, reverse=True)
        for r, rel in entries:
            day = (r.created_at or "")[:10] or "????-??-??"
            name = r.name or "(untitled)"
            tag = " *(project)*" if r.type == "project" else ""
            lines.append(f"- {day} — [{name}]({rel.as_posix()}){tag}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Imperative shell
# ---------------------------------------------------------------------------

def gather_results(config: dict, source: str) -> List[SearchResult]:
    """Enumerate every conversation and project (empty query = browse) across the source(s)."""
    results: List[SearchResult] = []
    if source in ("all", "llm"):
        data_dir = resolve_data_dir(Path(__file__).parent.resolve(), config)
        results.extend(search_archive(data_dir, "", apply_recency_boost=False))
    if source in ("all", "claude-code"):
        sources = parse_claude_code_sources(config)
        if sources:
            results.extend(search_claude_code_archive(sources, "", apply_recency_boost=False))
    return results


def run_export(output_dir: Path, results: Sequence[SearchResult], fmt: str, dry_run: bool) -> int:
    """Write the planned files and the index. Returns the number written."""
    extension = "md" if fmt == "markdown" else "html"
    planned = plan_exports(results, extension=extension)

    if dry_run:
        print(f"Would export {len(planned)} item(s) to {output_dir}/")
        by_provider: dict[str, int] = {}
        for r, _ in planned:
            by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
        for provider, count in sorted(by_provider.items()):
            print(f"  {PROVIDER_LABELS.get(provider, provider):<14} {count}")
        return 0

    written: List[Tuple[SearchResult, Path]] = []
    failed = 0
    for r, rel in planned:
        out_path = output_dir / rel
        try:
            content = render_conversation(r.provider, r.filepath, fmt, r.type)
        except Exception as e:  # one bad file shouldn't abort the whole export
            print(f"Warning: could not render {r.filepath}: {e}", file=sys.stderr)
            failed += 1
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        written.append((r, rel))

    # Index only what actually landed on disk, so failures never become
    # dead links.
    (output_dir / "index.md").write_text(build_index(written), encoding="utf-8")

    print(f"Exported {len(written)} item(s) to {output_dir}/")
    if failed:
        print(f"  ({failed} could not be rendered — see warnings above)")
    print(f"Index: {output_dir / 'index.md'}")
    return len(written)


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-export the chat archive to dated Markdown/HTML files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  # export all to ./markdown-export/
  %(prog)s ~/Obsidian/llm-archive           # choose the output directory
  %(prog)s -s claude-code --dry-run         # preview counts, write nothing
  %(prog)s --format html ./html-export      # HTML instead of Markdown
        """,
    )
    parser.add_argument(
        "output_dir", nargs="?", default="markdown-export",
        help="Directory to write the export into (default: ./markdown-export)",
    )
    parser.add_argument(
        "-s", "--source", choices=SOURCE_CHOICES, default="all",
        help="Which source(s) to export (default: all)",
    )
    parser.add_argument(
        "--format", choices=["markdown", "html"], default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be exported without writing files",
    )
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="Path to the .env config file (default: alongside this script)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    env_path = resolve_env_path(script_dir, args.config)
    if args.config and not env_path.is_file():
        print(f"Error: --config file not found: {env_path}", file=sys.stderr)
        sys.exit(1)
    config = load_env_file(env_path)

    results = gather_results(config, args.source)
    if not results:
        print("No conversations found to export.")
        return

    run_export(Path(args.output_dir).expanduser(), results, args.format, args.dry_run)


if __name__ == "__main__":
    main()
