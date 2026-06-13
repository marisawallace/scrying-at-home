#!/usr/bin/env python3
"""
Migration 003: Rename the project directory, .env paths, and shell aliases.

The project was renamed clauding-at-home -> scrying-at-home. Existing clones
still live in a directory named `clauding-at-home/`, the shell aliases that
point at it break the moment that directory is renamed, AND — for anyone who
put absolute paths in `.env` — the `LLM_DATA_DIR` / `ARCHIVED_EXPORTS_DIR` /
`LOCAL_VIEWS_DIR` / `CLAUDE_CODE_SOURCES` / `SEARCH_INDEX_DB` values, plus the
on-disk directories they reference, also still carry the old name.

This migration, in one pass:

  1. Plans and (after an explicit yes/No gate) performs the *directory moves*:
     every directory whose path has `clauding-at-home` as an exact path
     component — the repo dir AND any separate, e.g. cloud-synced, data dir
     named the same — is renamed in place to `scrying-at-home`. Directories
     nest, so the moves are deduped to the shallowest rename points and run
     ancestor-first; a descendant is carried along by its ancestor's rename.
  2. Rewrites the matching `clauding-at-home` components in your `.env` values
     to `scrying-at-home`, at the file's post-move location.
  3. Rewrites the alias lines in your shell rc file(s) that reference the old
     directory name, plus the `# clauding-at-home aliases` block header.

A path is in scope only when one of its `/`-separated components is *exactly*
`clauding-at-home`; siblings like `clauding-at-home-backup` are left alone. All
matching is component-aware, never a raw `str.replace`.

Moving user data is risky, so the directory-move phase has its own confirmation
(default No), an always-written recovery manifest, an opt-in byte-for-byte
backup, and pre-flight safety checks (mountpoint / cross-device refusal,
destination-collision abort — an existing file or directory at a move's
destination is never overwritten — dangling-reference skip, cloud-sync
warnings).

Usage:
  python3 migrations/003_rename_project_and_env_paths.py
  python3 migrations/003_rename_project_and_env_paths.py --dry-run  # plan only
  python3 migrations/003_rename_project_and_env_paths.py --yes      # skip prompts
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ANSI colors (match migrations 001 / 002)
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

REPO_MARKER = "full_text_search_chats_archive.py"

# Resolve the repo root early so we can import the shared helpers without each
# call site repeating the sys.path dance (mirrors migration 002).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# The project's old and new names live in paths.py as the (legacy) cache-dir
# names — the cache dir is named after the project, and paths.py documents the
# rename there. Reuse them so the literals don't drift across the codebase.
from paths import CACHE_DIR_NAME as NEW_NAME  # noqa: E402  ("scrying-at-home")
from paths import LEGACY_CACHE_DIR_NAME as OLD_NAME  # noqa: E402  ("clauding-at-home")
from paths import parse_env_assignment, parse_sources_string  # noqa: E402

# Reuse setup.py's dotfile discovery and file-edit plumbing so this migration
# looks for aliases exactly where setup.py wrote them, and backs up the same way.
from setup import (  # noqa: E402
    ALIAS_BLOCK_HEADER,
    _backup,
    _prompt_yn,
    _readable_dotfiles,
    _tilde,
    _timestamp,
)


# --------------------------------------------------------------------------
# Pure helpers (functional core)
# --------------------------------------------------------------------------

def old_alias_header(new_header: str) -> str:
    """The pre-rename alias-block header (`# clauding-at-home aliases`).

    Derived from the current header by swapping the new name back to the old,
    so the two stay in lockstep with ALIAS_BLOCK_HEADER.
    """
    return new_header.replace(NEW_NAME, OLD_NAME)


def classify_repo_dir(repo_root: Path) -> tuple[str, Path]:
    """Decide the directory-rename action from the repo dir's basename. Pure.

    Returns (action, new_root):
      "rename"  basename is the old name -> move to parent/<new name>
      "already" basename is already the new name -> nothing to move
      "custom"  some other clone dir name -> leave the directory alone
    new_root is where the repo should live (== repo_root unless action=="rename").
    """
    name = repo_root.name
    if name == OLD_NAME:
        return "rename", repo_root.parent / NEW_NAME
    if name == NEW_NAME:
        return "already", repo_root
    return "custom", repo_root


def _segment_re() -> re.Pattern:
    """Match the old name only as a whole path segment: preceded by `/`, and not
    followed by a name char, `-`, or `.`.

    Excluding `.` (alongside `\\w` and `-`) keeps `clauding-at-home.bak` and
    `clauding-at-home.old` — which are *different* path components, never moved
    on disk — out of scope, exactly like `clauding-at-home-backup`. This keeps
    the text rewrite in lockstep with the move planner's component-exact
    `comp == OLD_NAME` test, so disk and text never diverge on a dotted sibling.
    """
    return re.compile(r"(?<=/)" + re.escape(OLD_NAME) + r"(?![\w.-])")


def rewrite_alias_lines(
    text: str, new_header: str
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite alias lines that reference the old dir name, plus the old header.

    Pure. Only lines beginning with `alias ` (any shell) and the exact
    `# clauding-at-home aliases` header line are touched; every other line is
    preserved verbatim. Returns (new_text, changes) where changes is a list of
    (before, after) for the preview.
    """
    seg = _segment_re()
    old_header = old_alias_header(new_header)
    new_lines: list[str] = []
    changes: list[tuple[str, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("alias "):
            updated = seg.sub(NEW_NAME, raw)
        elif stripped == old_header:
            updated = raw.replace(old_header, new_header)
        else:
            new_lines.append(raw)
            continue
        new_lines.append(updated)
        if updated != raw:
            changes.append((raw, updated))
    out = "\n".join(new_lines)
    if text.endswith("\n"):
        out += "\n"
    return out, changes


def other_old_name_lines(text: str, new_header: str) -> list[str]:
    """Non-alias, non-header lines that still mention the old name. Pure.

    Purely informational: these are reported, never edited (they may point at
    an unrelated directory that happens to share the name).
    """
    old_header = old_alias_header(new_header)
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("alias ") or stripped == old_header:
            continue
        if OLD_NAME in raw:
            out.append(raw)
    return out


# --------------------------------------------------------------------------
# Move planning (validated functional core)
#
# The same component-rename transform "rename every clauding-at-home component
# to scrying-at-home" drives both the physical directory moves and the .env
# text rewrite, so disk and text stay consistent by construction.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Move:
    """One planned directory rename.

    origin  the rename point in its *original* coordinates (basename == OLD_NAME);
            used to match .env values against refused moves.
    src     the current source after applying earlier moves (== origin unless a
            shallower move already relocated an ancestor of this rename point).
    dst     src.parent / NEW_NAME — the rename always swaps the final component
            in place within one parent.
    """

    origin: Path
    src: Path
    dst: Path


def _norm_parts(path: Path) -> tuple[str, ...]:
    """Normalized (no `.`/`..`, no doubled slashes) path components, for prefix
    comparison and dedup. Does not touch the filesystem (no symlink resolution)."""
    return Path(os.path.normpath(str(path))).parts


def _is_under_or_eq(path: Path, ancestor: Path) -> bool:
    """True when `path` is `ancestor` or lives somewhere beneath it. Pure."""
    p = _norm_parts(path)
    a = _norm_parts(ancestor)
    return p[: len(a)] == a


def _value_paths(value: str) -> list[str]:
    """The path string(s) embedded in one .env value.

    A CLAUDE_CODE_SOURCES value is `host=path,host=path`; everything else is a
    bare path. Parsed leniently — anything parse_sources_string rejects (e.g. a
    normal path, which has no '=') is treated as a single path.
    """
    try:
        pairs = parse_sources_string(value)
    except ValueError:
        return [value]
    if not pairs:
        return [value]
    return [p for _host, p in pairs]


def _collapse_parent_symlinks(p: Path) -> Path:
    """Resolve symlinks in the parent, keep the final component verbatim. Impure.

    Two symbolic routes to the same physical directory collapse to one candidate
    (so dedup merges them), while the directory we intend to rename keeps its own
    name rather than being replaced by its symlink target. realpath on a path
    whose parent does not exist still normalizes it (no error), so this is safe
    to call on dangling references too.
    """
    p = p.expanduser()
    if not p.is_absolute():
        return Path(os.path.normpath(str(p)))
    return Path(os.path.realpath(str(p.parent))) / p.name


def collect_candidate_paths(repo_root: Path, env_text: str) -> list[Path]:
    """Absolute directories in scope for renaming. Impure (resolves symlinks).

    The repo root (only when its basename is exactly the old name) plus every
    *active* .env value that, ~-expanded and absolute, has clauding-at-home as a
    path component. CLAUDE_CODE_SOURCES is split into its individual paths. All
    active keys are scanned, so future path keys are covered automatically.
    """
    candidates: list[Path] = []
    if repo_root.name == OLD_NAME:
        candidates.append(_collapse_parent_symlinks(repo_root))
    for raw in env_text.splitlines():
        parsed = parse_env_assignment(raw)
        if parsed is None or parsed[2]:  # skip blanks and commented lines
            continue
        for path_str in _value_paths(parsed[1]):
            p = Path(path_str).expanduser()
            if not p.is_absolute():
                continue  # only ever move real, absolute directories
            p = _collapse_parent_symlinks(p)
            if OLD_NAME in p.parts:
                candidates.append(p)
    return candidates


def rename_points(candidates: list[Path]) -> list[Path]:
    """Deduped, shallowest-first rename points across all candidates. Pure.

    For every candidate and *every* component index whose component is exactly
    the old name (a path may contain it twice), the rename point is that
    directory (its basename == OLD_NAME). Deduped by normalized path, then
    sorted fewest-components-first so an ancestor is always renamed before any
    descendant rename point it contains.
    """
    points: list[Path] = []
    for c in candidates:
        parts = c.parts
        for i, comp in enumerate(parts):
            if comp == OLD_NAME:
                points.append(Path(*parts[: i + 1]))
    seen: dict[str, Path] = {}
    for pt in points:
        seen.setdefault(os.path.normpath(str(pt)), pt)
    unique = list(seen.values())
    unique.sort(key=lambda p: len(p.parts))
    return unique


def plan_moves(candidates: list[Path]) -> list[Move]:
    """Ordered, executable list of directory moves. Pure.

    Each rename point becomes one move. Moves run shallowest-first, so before
    emitting a move its *current* source is recomputed by applying every earlier
    move as a component-prefix substitution — load-bearing only for the rare
    case where one path holds two clauding-at-home components, where the deeper
    rename point's source has already been relocated by the shallower move.
    """
    applied: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    moves: list[Move] = []
    for origin in rename_points(candidates):
        cur = list(origin.parts)
        for old_parts, new_parts in applied:
            if tuple(cur[: len(old_parts)]) == old_parts:
                cur = list(new_parts) + cur[len(old_parts):]
        src = Path(*cur)
        dst = src.parent / NEW_NAME
        moves.append(Move(origin=origin, src=src, dst=dst))
        applied.append((tuple(src.parts), tuple(dst.parts)))
    return moves


# --------------------------------------------------------------------------
# Pre-flight validation
#
# Sources/dsts of nested moves don't exist on disk yet (an earlier move creates
# their parent), so validate against a *simulated post-move namespace* rather
# than naively lstat-ing every path against the live filesystem.
# --------------------------------------------------------------------------

# status values a move is classified into
OK = "ok"                # will execute
DANGLING = "dangling"    # source missing (or its ancestor move was skipped) -> skip, still rewrite .env
REFUSED = "refused"      # mountpoint / fs-root / $HOME -> skip, do NOT rewrite .env, print manual mv
CONFLICT = "conflict"    # real dst collision -> hard error, abort the whole move phase


@dataclass(frozen=True)
class Classified:
    move: Move
    status: str
    nested: bool


def _has_partial_old_name(path: Path) -> bool:
    """True if any component *contains* the old name without *being* it.

    Component-exact matching should make this impossible; it's a defensive
    assertion that no `clauding-at-home-backup`-style partial slipped in.
    """
    return any(OLD_NAME in comp and comp != OLD_NAME for comp in path.parts)


def classify_moves(
    moves: list[Move],
    *,
    lexists=os.path.lexists,
    ismount=os.path.ismount,
    home: Path | None = None,
) -> list[Classified]:
    """Classify each move against a simulated post-move namespace. Mostly pure.

    Filesystem access is confined to the injectable `lexists` / `ismount`
    predicates and `home`, so the classifier is unit-testable without real
    mounts. Order matters: a move's classification can depend on whether an
    earlier (ancestor) move executes.
    """
    home = Path.home() if home is None else home
    out: list[Classified] = []
    prior_dsts: list[Path] = []   # every earlier move's dst (for nesting)
    ok_dsts: list[Path] = []      # dsts of moves that will execute (for "produced")
    for mv in moves:
        for p in (mv.src, mv.dst):
            assert not _has_partial_old_name(p), f"partial old-name match in {p}"
        nested = any(_is_under_or_eq(mv.src, d) for d in prior_dsts)
        status = _classify_one(mv, nested, ok_dsts, lexists, ismount, home)
        out.append(Classified(move=mv, status=status, nested=nested))
        prior_dsts.append(mv.dst)
        if status == OK:
            ok_dsts.append(mv.dst)
    return out


def _classify_one(mv, nested, ok_dsts, lexists, ismount, home) -> str:
    src, dst = mv.src, mv.dst
    # Sanity refusals: never rename a filesystem root or $HOME itself.
    if src == Path(src.anchor) or src == home:
        return REFUSED
    produced = any(_is_under_or_eq(dst, d) for d in ok_dsts)
    if nested:
        # Source is created by an earlier move; we can't lstat it pre-move.
        if not any(_is_under_or_eq(src, d) for d in ok_dsts):
            return DANGLING  # the ancestor move was skipped, so src never appears
        if lexists(dst) and not produced:
            return CONFLICT
        return OK
    # Root move (not nested under any earlier move): validate against live FS.
    if not lexists(src):
        return DANGLING
    if ismount(src):
        # mv renames the final component in place, so src and dst.parent differ
        # in device only when src is itself a mountpoint -> refuse (never copy).
        return REFUSED
    if lexists(dst) and not produced:
        return CONFLICT
    return OK


# --------------------------------------------------------------------------
# .env value rewrite
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvRewrite:
    text: str                          # rewritten .env text
    changes: list[tuple[str, str]]     # (before, after) for active lines we edited
    kept_lines: list[str]              # active lines left as-is (no planned move covers them)
    residual: list[str]                # commented/prose lines that still mention OLD_NAME


def value_is_rewritable(value: str, movable_origins: list[Path]) -> bool:
    """True when rewriting OLD_NAME components in a .env `value` matches a real
    directory move. Impure (resolves parent symlinks).

    For every path in the value that literally contains an OLD_NAME segment, its
    parent-symlink-resolved form (the same coordinates plan_moves works in) must
    live at or under a *planned* move's origin — the OK and DANGLING moves, NOT
    the refused ones. This keeps .env in lockstep with the disk:

      - a path whose move was REFUSED (mountpoint) keeps pointing at the real,
        unmoved location;
      - a path whose OLD_NAME component a parent symlink resolves away (so no
        move was ever planned for it) is left verbatim rather than repointed at
        a directory that will not exist.

    A value with no OLD_NAME path segment has nothing to rewrite -> False.
    """
    seg = _segment_re()
    matched = [p for p in _value_paths(value) if seg.search(p)]
    if not matched:
        return False
    for path_str in matched:
        resolved = _collapse_parent_symlinks(Path(path_str).expanduser())
        if not any(_is_under_or_eq(resolved, origin) for origin in movable_origins):
            return False
    return True


def rewrite_env_paths(text: str, *, is_rewritable=lambda _value: True) -> EnvRewrite:
    """Rename OLD_NAME path components in active .env assignment values.

    Pure given the injected `is_rewritable` predicate; the shell supplies the
    impure one (value_is_rewritable) that consults the actual move plan, mirroring
    how classify_moves takes injectable filesystem predicates.

    Only active assignment lines whose value has an old-name component are
    edited, via the component-aware segment regex (so every such component is
    renamed, and siblings like `clauding-at-home-backup` / `clauding-at-home.bak`
    are left alone). A line whose value is NOT rewritable — its directory move was
    refused, or a symlink resolves its old-name component away so no planned move
    covers it — is left verbatim and reported instead, so .env never names a
    directory that was not actually moved. Commented and prose lines are never
    edited; any that still mention the old name are reported.
    """
    seg = _segment_re()
    new_lines: list[str] = []
    changes: list[tuple[str, str]] = []
    kept_lines: list[str] = []
    residual: list[str] = []
    for raw in text.splitlines():
        parsed = parse_env_assignment(raw)
        is_active = parsed is not None and not parsed[2]
        if is_active and seg.search(raw):
            if not is_rewritable(parsed[1]):
                new_lines.append(raw)
                kept_lines.append(raw)
                continue
            updated = seg.sub(NEW_NAME, raw)
            new_lines.append(updated)
            if updated != raw:
                changes.append((raw, updated))
            continue
        new_lines.append(raw)
        if OLD_NAME in raw:
            residual.append(raw)
    out = "\n".join(new_lines)
    if text.endswith("\n"):
        out += "\n"
    return EnvRewrite(out, changes, kept_lines, residual)


# --------------------------------------------------------------------------
# Cloud-sync heuristics and manifest text (pure)
# --------------------------------------------------------------------------

# Path components that strongly suggest a cloud-sync root. Renaming a synced
# directory can make the client treat it as a mass delete + re-upload.
_SYNC_COMPONENTS = {"sync", "syncs"}
_SYNC_CLIENT_DIRS = {
    "dropbox", ".dropbox", "mega", "megasync", "syncthing", "onedrive",
    "nextcloud", "owncloud", "icloud", "iclouddrive",
}


def under_sync_root(path: Path) -> bool:
    """Heuristic: does `path` sit under a likely cloud-sync root? Pure."""
    lowered = [c.lower() for c in path.parts]
    if any(c in _SYNC_COMPONENTS for c in lowered):
        return True
    return any(c in _SYNC_CLIENT_DIRS for c in lowered)


def manifest_text(classified: list[Classified], timestamp: str) -> str:
    """Plain-text recovery record of every planned move and its status. Pure."""
    lines = [
        f"scrying-at-home migration 003 — move manifest ({timestamp})",
        "Reverse a partial run by renaming each dst back to its src, deepest-first.",
        "",
    ]
    for c in classified:
        lines.append(f"[{c.status}] {c.move.src}  ->  {c.move.dst}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Imperative shell
# --------------------------------------------------------------------------

def find_repo_root() -> Path | None:
    """Find the repo root by looking for the search entry script."""
    candidate = Path(__file__).resolve().parent
    for _ in range(4):
        if (candidate / REPO_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return None


def _print_change(before: str, after: str) -> None:
    print(f"      {RED}- {before.strip()}{RESET}")
    print(f"      {GREEN}+ {after.strip()}{RESET}")


def _dir_size(path: Path) -> int:
    """Total size in bytes of the files under `path` (best-effort)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.lstat().st_size
            except OSError:
                pass
    return total


def _outermost_sources(ok_moves: list[Move]) -> list[Move]:
    """Moves whose source is not nested under another move's source.

    Backing up these alone copies every nested move's source too (it lives
    inside an ancestor's tree), avoiding a double copy.
    """
    out: list[Move] = []
    for mv in ok_moves:
        if not any(
            other is not mv and _is_under_or_eq(mv.src, other.src)
            for other in ok_moves
        ):
            out.append(mv)
    return out


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_hms() -> str:
    return datetime.now(timezone.utc).strftime("%H%M%S")


def _write_manifest(classified: list[Classified], ts: str) -> Path:
    """Always-written recovery record under $HOME. Returns its path."""
    dest = Path.home() / f"scrying-at-home-migration-manifest-{ts}.txt"
    dest.write_text(manifest_text(classified, ts), encoding="utf-8")
    return dest


def _maybe_backup_dirs(ok_moves: list[Move]) -> Path | None:
    """Copy the outermost move sources into a dated backup dir under $HOME.

    Pre-checks free space and aborts the copy (returning None) if insufficient,
    rather than starting a copy that will fail partway. Returns the backup dir
    on success, None if it couldn't run.
    """
    sources = _outermost_sources(ok_moves)
    total = sum(_dir_size(mv.src) for mv in sources)
    dest_parent = Path.home()
    free = shutil.disk_usage(dest_parent).free
    print(f"  {DIM}Backing up {len(sources)} dir(s), ~{total / 1e6:.0f} MB.{RESET}")
    if free < total:
        print(f"  {RED}✗ Not enough free space in {dest_parent} "
              f"(need ~{total / 1e6:.0f} MB, have ~{free / 1e6:.0f} MB). "
              f"Skipping the byte-for-byte backup.{RESET}")
        return None
    backup_dir = dest_parent / f"scrying-at-home-data-backup-before-migration-{_utc_date()}"
    if backup_dir.exists():
        backup_dir = backup_dir.with_name(backup_dir.name + f"-{_utc_hms()}")
    backup_dir.mkdir(parents=True)
    for mv in sources:
        # Mirror each source's full path under the backup dir. Outermost sources
        # can share a basename (the repo dir and a separate data dir are both
        # named clauding-at-home), so a flat `backup_dir / name` would collide;
        # the full path is unique and shows exactly where each copy came from.
        dest = backup_dir / mv.src.relative_to(mv.src.anchor)
        shutil.copytree(mv.src, dest)
        print(f"  {GREEN}✓{RESET} Copied {mv.src} → {_tilde(dest)}")
    return backup_dir


def _execute_moves(ok: list[Classified]) -> list[Move]:
    """Run the ok moves shallowest-first with os.rename (atomic in one fs).

    Verifies dst appeared and src is gone after each. On any unexpected failure,
    stops immediately and re-raises so the caller can report what completed.
    Returns the moves that succeeded.
    """
    done: list[Move] = []
    for c in ok:
        mv = c.move
        os.rename(mv.src, mv.dst)
        if not mv.dst.exists() or mv.src.exists():
            raise OSError(f"rename {mv.src} -> {mv.dst} left the filesystem in an "
                          f"unexpected state")
        print(f"  {GREEN}✓{RESET} Moved {mv.src} → {mv.dst}")
        done.append(mv)
    return done


def _final_repo_root(repo_root: Path, executed: list[Move]) -> Path:
    """Where the repo lives after the moves (it moves if its dir was renamed)."""
    for mv in executed:
        if mv.src == repo_root:
            return mv.dst
    return repo_root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migration 003: Rename the project dir, .env paths, and aliases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the full plan and exit without prompting or changing anything",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompts (still honors cross-fs/collision refusals)",
    )
    parser.add_argument(
        "--allow-sync-root", action="store_true",
        help="Permit renaming a directory under a detected cloud-sync root "
             "together with --yes, after you've paused your sync client. "
             "Without it, --yes refuses such a move.",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Migration 003: Rename {OLD_NAME} -> {NEW_NAME}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    repo_root = find_repo_root()
    if repo_root is None:
        print(f"{RED}ERROR: Could not find the repository root.{RESET}")
        print(f"  Looked for {REPO_MARKER} starting from "
              f"{Path(__file__).resolve().parent}.")
        sys.exit(1)

    print(f"  Repository root: {CYAN}{repo_root}{RESET}\n")

    action, _new_root = classify_repo_dir(repo_root)

    # --- Read .env (its absolute paths drive both the moves and the rewrite) ---
    env_path = repo_root / ".env"
    env_text = ""
    if env_path.exists():
        try:
            env_text = env_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            env_text = ""

    # --- Plan the directory moves (pure core) ---
    candidates = collect_candidate_paths(repo_root, env_text)
    classified = classify_moves(plan_moves(candidates))

    ok = [c for c in classified if c.status == OK]
    dangling = [c for c in classified if c.status == DANGLING]
    refused = [c for c in classified if c.status == REFUSED]
    conflicts = [c for c in classified if c.status == CONFLICT]

    # --- Plan the .env rewrite ---
    # Rewrite a value only when a *planned* move (OK or dangling, never refused)
    # actually covers the directory it names, matched in the move planner's own
    # symlink-resolved coordinates. This stops .env from being repointed at a
    # path nothing moved — a refused mountpoint, or a component a parent symlink
    # resolves away.
    movable_origins = [c.move.origin for c in classified if c.status in (OK, DANGLING)]
    env_rewrite = rewrite_env_paths(
        env_text,
        is_rewritable=lambda value: value_is_rewritable(value, movable_origins),
    )

    # --- Plan the alias rewrites (reusing setup.py's dotfile discovery) ---
    dotfiles = _readable_dotfiles()
    alias_planned: list[tuple[Path, str, list[tuple[str, str]]]] = []
    alias_other: list[tuple[Path, list[str]]] = []
    for path, _shell, text in dotfiles:
        new_text, changes = rewrite_alias_lines(text, ALIAS_BLOCK_HEADER)
        if changes:
            alias_planned.append((path, new_text, changes))
        others = other_old_name_lines(text, ALIAS_BLOCK_HEADER)
        if others:
            alias_other.append((path, others))

    # ----------------------------------------------------------------------
    # Preview
    # ----------------------------------------------------------------------
    print(f"{BOLD}Directory moves:{RESET}")
    if not classified:
        if action == "already":
            print(f"  {DIM}Repo already named {NEW_NAME}/ and no .env paths "
                  f"reference {OLD_NAME!r} — nothing to move.{RESET}")
        elif action == "custom":
            print(f"  {DIM}Repo is named {repo_root.name!r} (not {OLD_NAME!r}) and "
                  f"no .env paths reference {OLD_NAME!r} — nothing to move.{RESET}")
        else:
            print(f"  {DIM}Nothing to move.{RESET}")
    for c in classified:
        mv = c.move
        if c.status == OK:
            sync = under_sync_root(mv.src)
            tag = f"  {YELLOW}[cloud-sync root — see warning below]{RESET}" if sync else ""
            print(f"  {GREEN}mv{RESET} {CYAN}{mv.src}{RESET}")
            print(f"     {GREEN}→{RESET} {CYAN}{mv.dst}{RESET}{tag}")
        elif c.status == DANGLING:
            print(f"  {YELLOW}skip (source does not exist){RESET} "
                  f"{DIM}{mv.src} → {mv.dst}{RESET}")
            print(f"       {DIM}.env line(s) still rewritten — the reference stays "
                  f"equivalently dangling, now under the new name.{RESET}")
        elif c.status == REFUSED:
            print(f"  {RED}REFUSED (mountpoint / cross-device){RESET} "
                  f"{DIM}{mv.src}{RESET}")
            print(f"       {YELLOW}Run this yourself once you understand the "
                  f"consequences:{RESET}")
            print(f"         {CYAN}mv {mv.src} {mv.dst}{RESET}")
            print(f"       {DIM}.env left pointing at the real (unmoved) "
                  f"location.{RESET}")
        elif c.status == CONFLICT:
            print(f"  {RED}✗ Destination already exists: {mv.dst}{RESET}")
            print(f"  {RED}  Refusing to overwrite it. Resolve this by hand "
                  f"first.{RESET}")

    if ok:
        print(f"  {DIM}If a destination above already exists, that move is reported as "
              f"a conflict{RESET}")
        print(f"  {DIM}and the whole move phase aborts — an existing file or directory "
              f"is never overwritten.{RESET}")

    sync_moves = [c for c in ok if under_sync_root(c.move.src)]
    if sync_moves:
        print(f"\n{YELLOW}{BOLD}⚠ Cloud-sync warning:{RESET}")
        print(f"  {YELLOW}A move source sits under a likely cloud-sync root "
              f"(MEGA/Dropbox/Syncthing/…).{RESET}")
        print(f"  {YELLOW}Renaming a synced directory is the single highest-risk "
              f"action here — the{RESET}")
        print(f"  {YELLOW}client may see it as a mass delete + re-upload of the "
              f"whole tree. Prefer to{RESET}")
        print(f"  {YELLOW}pause/quiesce your sync client and run that {BOLD}mv{RESET}"
              f"{YELLOW} yourself.{RESET}")

    # --- .env diff ---
    print(f"\n{BOLD}.env ({_tilde(env_path)}):{RESET}")
    if env_rewrite.changes:
        for before, after in env_rewrite.changes:
            _print_change(before, after)
    else:
        print(f"  {DIM}No active path values reference {OLD_NAME!r} — nothing to "
              f"rewrite.{RESET}")
    if env_rewrite.kept_lines:
        print(f"  {YELLOW}Left as-is (no directory move covers them — the move was "
              f"refused, or a symlink resolves the path elsewhere):{RESET}")
        for ln in env_rewrite.kept_lines:
            print(f"    {DIM}{ln.strip()}{RESET}")
    if env_rewrite.residual:
        print(f"  {DIM}Still mention {OLD_NAME!r} (comments/prose — not edited):{RESET}")
        for ln in env_rewrite.residual:
            print(f"    {DIM}{ln.strip()}{RESET}")

    # --- alias diff ---
    print(f"\n{BOLD}Shell aliases:{RESET}")
    if alias_planned:
        for path, _new_text, changes in alias_planned:
            print(f"  {CYAN}{_tilde(path)}{RESET}")
            for before, after in changes:
                _print_change(before, after)
    else:
        print(f"  {DIM}No alias lines reference {OLD_NAME!r} — nothing to "
              f"rewrite.{RESET}")
    if alias_other:
        print(f"\n{BOLD}Left untouched{RESET} {DIM}(mention {OLD_NAME!r} but "
              f"aren't aliases — review by hand if they point at this repo):{RESET}")
        for path, lines in alias_other:
            print(f"  {DIM}{_tilde(path)}{RESET}")
            for ln in lines:
                print(f"    {DIM}{ln.strip()}{RESET}")

    # --- Hard conflict: abort with nothing done ---
    if conflicts:
        print(f"\n{RED}Aborting: a destination directory already exists "
              f"(see above). No moves performed.{RESET}\n")
        sys.exit(1)

    has_moves = bool(ok)
    has_edits = bool(env_rewrite.changes or alias_planned)

    # --- Nothing to do? (prose residue is expected and still reported) ---
    if not has_moves and not has_edits and not dangling:
        print(f"\n{GREEN}✓ Already migrated — nothing to move or rewrite.{RESET}")
        if env_rewrite.residual or alias_other:
            print(f"{DIM}(Informational mentions of {OLD_NAME!r} above are expected "
                  f"residue.){RESET}")
        print()
        sys.exit(0)

    print(f"\n{BOLD}Recovery:{RESET} a move manifest is always written to "
          f"{CYAN}~/scrying-at-home-migration-manifest-<timestamp>.txt{RESET}")

    # --- Dry run stops here ---
    if args.dry_run:
        print(f"\n{DIM}--dry-run: no prompts, nothing changed.{RESET}\n")
        sys.exit(0)

    # --- Never rename a cloud-synced directory unattended ---
    # Renaming a synced tree can make the client see a mass delete + re-upload
    # and propagate that to other machines, so --yes alone must not do it.
    # Require an explicit --allow-sync-root, after the user has quiesced the
    # client; otherwise fall back to the interactive gate (rerun without --yes).
    if sync_moves and args.yes and not args.allow_sync_root:
        print(f"\n{RED}Refusing: --yes will not silently rename a directory under "
              f"a cloud-sync root.{RESET}")
        print(f"{YELLOW}Pause/quiesce your sync client first, then either rerun "
              f"without --yes to confirm interactively, or add "
              f"--allow-sync-root to override.{RESET}\n")
        sys.exit(1)

    # ----------------------------------------------------------------------
    # Confirmation. If the user opts into a backup it runs immediately — before
    # the proceed gate — so the safety copy exists the moment they ask for it.
    # The manifest (cheap recovery record) is written up front and folded into
    # the backup; otherwise it's written below in the Applying step.
    # ----------------------------------------------------------------------
    ts = _timestamp()
    manifest_path: Path | None = None
    backup_dir: Path | None = None

    if not args.yes:
        if has_moves:
            print(f"\n{YELLOW}The directory moves below rename real directories on "
                  f"disk (every move is an{RESET}")
            print(f"{YELLOW}atomic os.rename within one filesystem, trivially "
                  f"reversible by renaming back).{RESET}")
            print(f"{YELLOW}If any destination already exists the move aborts with "
                  f"nothing done — an{RESET}")
            print(f"{YELLOW}existing file or directory there is never overwritten.{RESET}")
            if _prompt_yn("Also copy directories to a backup first?", default=False):
                print(f"\n{BOLD}Backing up...{RESET}")
                manifest_path = _write_manifest(classified, ts)
                print(f"  {GREEN}✓{RESET} Wrote manifest → {_tilde(manifest_path)}")
                backup_dir = _maybe_backup_dirs([c.move for c in ok])
                if backup_dir is not None:
                    (backup_dir / manifest_path.name).write_text(
                        manifest_text(classified, ts), encoding="utf-8"
                    )
            if not _prompt_yn(
                "Proceed with directory moves and the edits above?", default=False
            ):
                print("Aborted.")
                sys.exit(0)
        else:
            if not _prompt_yn(
                "Proceed with the .env / alias edits above?", default=True
            ):
                print("Aborted.")
                sys.exit(0)

    print(f"\n{BOLD}Applying...{RESET}")

    # --- Manifest (always written; the backup step above may already have) ---
    if manifest_path is None:
        manifest_path = _write_manifest(classified, ts)
        print(f"  {GREEN}✓{RESET} Wrote manifest → {_tilde(manifest_path)}")

    # --- Execute moves shallowest-first ---
    executed: list[Move] = []
    if has_moves:
        try:
            executed = _execute_moves(ok)
        except OSError as e:
            print(f"\n{RED}A move failed: {e}{RESET}")
            print(f"{RED}Completed moves: "
                  f"{[str(m.dst) for m in executed] or 'none'}.{RESET}")
            print(f"{YELLOW}Recover using the manifest at {_tilde(manifest_path)}"
                  + (f" or the backup at {_tilde(backup_dir)}" if backup_dir else "")
                  + f".{RESET}")
            sys.exit(1)

    # --- Rewrite .env LAST, at its post-move location ---
    if env_rewrite.changes:
        final_root = _final_repo_root(repo_root, executed)
        final_env = final_root / ".env"
        if final_env.exists():
            _backup(final_env)
            final_env.write_text(env_rewrite.text, encoding="utf-8")
            print(f"  {GREEN}✓{RESET} Rewrote paths in {_tilde(final_env)}")
        else:
            print(f"  {YELLOW}!{RESET} Expected {final_env} but it is missing — "
                  f"skipped the .env rewrite.")

    # --- Rewrite alias dotfiles (live under $HOME, unaffected by the moves) ---
    for path, new_text, _changes in alias_planned:
        _backup(path)
        path.write_text(new_text, encoding="utf-8")
        print(f"  {GREEN}✓{RESET} Updated aliases in {_tilde(path)}")

    # ----------------------------------------------------------------------
    # Done
    # ----------------------------------------------------------------------
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Migration complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    tips = []
    final_root = _final_repo_root(repo_root, executed)
    if final_root != repo_root:
        tips.append(
            f"  Your shell is still sitting in the old path. Switch to the new one:\n"
            f"    {CYAN}cd {final_root}{RESET}"
        )
    if refused:
        tips.append(
            f"  {len(refused)} directory move(s) were refused (mountpoint/cross-device).\n"
            f"  Run the printed {BOLD}mv{RESET} command(s) yourself when ready."
        )
    if alias_planned:
        sources = "\n".join(f"      source {_tilde(p)}" for p, _, _ in alias_planned)
        tips.append(f"  Reload your updated aliases (or open a new shell):\n{sources}")
    if tips:
        print("\n" + "\n\n".join(tips) + "\n")
    else:
        print()


if __name__ == "__main__":
    main()
