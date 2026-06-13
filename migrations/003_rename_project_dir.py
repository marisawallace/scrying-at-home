#!/usr/bin/env python3
"""
Migration 003: Rename the project directory and shell aliases.

The project was renamed clauding-at-home -> scrying-at-home. Existing clones
still live in a directory named `clauding-at-home/`, and the shell aliases
that point at it (whether the absolute path setup.py bakes in, or a
hand-rolled `$CODE_HOME/clauding-at-home/...` form) break the moment the
directory is renamed.

This migration, in one pass:

  1. Renames the repo directory   .../clauding-at-home  ->  .../scrying-at-home
  2. Rewrites the alias lines in your shell rc file(s) that reference the old
     directory name, plus the `# clauding-at-home aliases` block header, to
     their new forms.

It reuses setup.py's dotfile discovery (CANDIDATE_DOTFILES) so it looks in the
same places setup.py originally wrote the aliases, and keys the rewrite on the
directory-name *segment* (a path component, bounded by `/`) rather than a
single absolute path — so both alias styles are caught.

Scope: it edits the repo directory and your shell rc alias lines only. It does
NOT touch .env or non-alias rc lines; if those reference the old name (e.g. a
separate data directory that happens to share it), it lists them so you can
decide for yourself.

Usage:
  python3 migrations/003_rename_project_dir.py
  python3 migrations/003_rename_project_dir.py --yes   # skip confirmation
"""

from __future__ import annotations

import argparse
import re
import sys
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

# Reuse setup.py's dotfile discovery and file-edit plumbing so this migration
# looks for aliases exactly where setup.py wrote them, and backs up the same way.
from setup import (  # noqa: E402
    ALIAS_BLOCK_HEADER,
    _backup,
    _prompt_yn,
    _readable_dotfiles,
    _tilde,
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
    """Match the old name only as a path segment: preceded by `/`, and not
    followed by a name char (so `clauding-at-home-backup` is left alone)."""
    return re.compile(r"(?<=/)" + re.escape(OLD_NAME) + r"(?![\w-])")


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migration 003: Rename the project directory and aliases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt and proceed immediately",
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

    action, new_root = classify_repo_dir(repo_root)

    print(f"  Repository root: {CYAN}{repo_root}{RESET}\n")

    # --- Plan the directory rename ---
    rename_needed = action == "rename"
    dest_conflict = rename_needed and new_root.exists()

    print(f"{BOLD}Directory:{RESET}")
    if rename_needed:
        if dest_conflict:
            print(f"  {RED}✗ Destination already exists: {new_root}{RESET}")
            print(f"  {RED}  Refusing to overwrite it. Resolve this by hand "
                  f"first.{RESET}")
        else:
            print(f"  {GREEN}rename{RESET}  {CYAN}{repo_root}{RESET}")
            print(f"          {GREEN}→{RESET}     {CYAN}{new_root}{RESET}")
    elif action == "already":
        print(f"  {DIM}Already named {NEW_NAME}/ — nothing to rename.{RESET}")
    else:  # custom
        print(f"  {DIM}Directory is named {repo_root.name!r}, not {OLD_NAME!r} "
              f"— leaving it as-is.{RESET}")

    # --- Plan the alias rewrites (reusing setup.py's dotfile discovery) ---
    dotfiles = _readable_dotfiles()  # [(path, shell, text), ...] for existing rc files
    planned: list[tuple[Path, str, list[tuple[str, str]]]] = []
    other_refs: list[tuple[Path, list[str]]] = []
    for path, _shell, text in dotfiles:
        new_text, changes = rewrite_alias_lines(text, ALIAS_BLOCK_HEADER)
        if changes:
            planned.append((path, new_text, changes))
        others = other_old_name_lines(text, ALIAS_BLOCK_HEADER)
        if others:
            other_refs.append((path, others))

    print(f"\n{BOLD}Shell aliases:{RESET}")
    if planned:
        for path, _new_text, changes in planned:
            print(f"  {CYAN}{_tilde(path)}{RESET}")
            for before, after in changes:
                _print_change(before, after)
    else:
        print(f"  {DIM}No alias lines reference {OLD_NAME!r} — nothing to "
              f"rewrite.{RESET}")

    # --- Nothing to do? ---
    if not rename_needed and not planned:
        print(f"\n{GREEN}✓ Already migrated — nothing to do.{RESET}\n")
        sys.exit(0)
    if dest_conflict:
        print(f"\n{RED}Aborting: cannot rename the directory (see above).{RESET}\n")
        sys.exit(1)

    # --- Informational: other references we will NOT touch ---
    if other_refs:
        print(f"\n{BOLD}Left untouched{RESET} {DIM}(mention {OLD_NAME!r} but "
              f"aren't aliases — review by hand if they point at this repo):{RESET}")
        for path, lines in other_refs:
            print(f"  {DIM}{_tilde(path)}{RESET}")
            for ln in lines:
                print(f"    {DIM}{ln.strip()}{RESET}")
    env_path = repo_root / ".env"
    if env_path.exists():
        try:
            env_text = env_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            env_text = ""
        if OLD_NAME in env_text:
            print(f"\n  {DIM}Note: {env_path.name} also mentions {OLD_NAME!r}. "
                  f"It's left untouched —{RESET}")
            print(f"  {DIM}those paths may be a separate data directory, not "
                  f"this repo. Check them if needed.{RESET}")

    # --- Confirmation ---
    if not args.yes:
        print(f"\n{YELLOW}This will rename the directory and edit the rc file(s) "
              f"above.{RESET}")
        print(f"{YELLOW}Timestamped backups are made before editing any dotfile "
              f"(*.bak.TIMESTAMP).{RESET}")
        if not _prompt_yn("Proceed?", default=True):
            print("Aborted.")
            sys.exit(0)

    print(f"\n{BOLD}Applying...{RESET}")

    # --- Step 1: rename the directory ---
    # Compute alias texts before the move; they bake in the *name*, which the
    # move doesn't change, so they remain valid afterward. The dotfiles live
    # under $HOME and are unaffected by moving the repo dir.
    if rename_needed:
        repo_root.rename(new_root)
        print(f"  {GREEN}✓{RESET} Renamed directory → {new_root}")

    # --- Step 2: rewrite alias lines ---
    for path, new_text, _changes in planned:
        _backup(path)
        path.write_text(new_text, encoding="utf-8")
        print(f"  {GREEN}✓{RESET} Updated aliases in {_tilde(path)}")

    # --- Done ---
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Migration complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    tips = []
    if rename_needed:
        tips.append(
            f"  Your shell is still sitting in the old path. Switch to the new one:\n"
            f"    {CYAN}cd {new_root}{RESET}"
        )
    if planned:
        sources = "\n".join(f"      source {_tilde(p)}" for p, _, _ in planned)
        tips.append(f"  Reload your updated aliases (or open a new shell):\n{sources}")
    if tips:
        print("\n" + "\n\n".join(tips) + "\n")
    else:
        print()


if __name__ == "__main__":
    main()
