#!/usr/bin/env python3
"""
setup.py: Interactive post-clone setup for clauding-at-home.

Walks you through everything after `git clone` + `cd clauding-at-home`:

  1. chmod +x the three entry scripts
  2. Create .env from .env.example (if missing)
  3. Set ZIP_SEARCH_DIR (the one variable worth prompting for)
  4. Optionally add shell aliases to a dotfile of your choosing
  5. Verify $EDITOR is set (offer to add it if not)
  6. Run the Claude Code archival setup (migrations/002...)

Conventions mirror migrations/002_setup_claude_code_archival.py: colored
output, a "Planned changes" preview before writing, timestamped backups
before editing any file (*.bak.YYYYMMDDTHHMMSSZ), and idempotent re-runs.

This does NOT re-implement the Claude Code migration; it shells out to it.

Usage:
  python3 setup.py
  python3 setup.py --yes                 # take all defaults, non-interactive
  python3 setup.py --yes --claude-hooks  # also run step 6 (installs hooks
                                         # into ~/.claude/settings.json)

Step 6 installs Stop/SessionEnd hooks into ~/.claude/settings.json — code
that runs on every Claude Code session. It is never run non-interactively
unless --claude-hooks is passed alongside --yes.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ANSI colors (match migration 002)
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))
from paths import (  # noqa: E402
    CANDIDATE_DOTFILES,
    current_shell_rc_texts,
    resolve_invocation,
    set_env_value,
)

# The three entry scripts that need to be executable.
ENTRY_SCRIPTS = (
    "sync_local_chats_archive.py",
    "full_text_search_chats_archive.py",
    "view_conversation.py",
)

# CANDIDATE_DOTFILES (the preference-ordered dotfile/shell pairs) lives in
# paths.py so migration 002 can reuse it for current-shell alias detection.

# Fallback names to suggest when the default "cs" conflicts.
BACKUP_ALIAS_NAMES = ("csr", "cls", "csearch", "chats", "cax")

ALIAS_BLOCK_HEADER = "# clauding-at-home aliases"


# --------------------------------------------------------------------------
# Pure helpers (functional core)
# --------------------------------------------------------------------------

def guess_downloads_dir(platform: str, env: dict) -> str:
    """Best guess at the user's downloads folder.

    ~/Downloads is correct on both macOS and Linux. On Linux we additionally
    consult `xdg-user-dir DOWNLOAD` (handled by the shell, passed in via
    `env["XDG_DOWNLOAD"]`) so localized/relocated dirs are honored.
    """
    xdg = (env.get("XDG_DOWNLOAD") or "").strip()
    if xdg:
        return xdg
    return "~/Downloads"


def alias_definition(name: str, command: str) -> str:
    """One `alias name=...` line with the definition safely quoted.

    The definition is shlex-quoted (single quotes): a clone path containing
    spaces still works, and `$`/backticks in the path stay literal instead of
    being expanded by the shell every time the dotfile is sourced.
    """
    return f"alias {name}={shlex.quote(command)}"


def alias_lines(repo_root: Path, name: str) -> dict[str, str]:
    """Map of label -> the single alias line for each of the 3 aliases."""
    search = shlex.quote(str(repo_root / "full_text_search_chats_archive.py"))
    sync = shlex.quote(str(repo_root / "sync_local_chats_archive.py"))
    return {
        "search": alias_definition(name, f"python3 {search}"),
        "sync-claude": alias_definition(f"{name}-sync-claude", f"python3 {sync} --claude"),
        "sync-chatgpt": alias_definition(f"{name}-sync-chatgpt", f"python3 {sync} --chatgpt"),
    }


def valid_alias_name(name: str) -> bool:
    """Alphanumeric/dash/underscore, and not option-like (leading dash)."""
    if not name or name.startswith("-"):
        return False
    return all(c.isalnum() or c in "-_" for c in name)


def strip_managed_aliases(dotfile_text: str, repo_root: Path) -> str:
    """Drop alias lines we own (they reference this repo's entry scripts).

    Conflict detection scans dotfiles for existing alias/function names, but
    aliases THIS script previously installed point at `repo_root`'s scripts —
    treating them as conflicts would make re-runs pick a fresh name and append
    a duplicate block. Filtering them keeps re-runs idempotent while still
    flagging genuine collisions with unrelated commands.
    """
    marker = str(repo_root)
    kept = [
        raw for raw in dotfile_text.splitlines()
        if not (raw.strip().startswith("alias ") and marker in raw)
    ]
    return "\n".join(kept)


def alias_line_present(dotfile_text: str, line: str) -> bool:
    """True if `line` already appears verbatim (stripped) in the dotfile."""
    target = line.strip()
    return any(raw.strip() == target for raw in dotfile_text.splitlines())


def detect_alias_conflict(
    dotfile_texts: list[str], name: str, which_result: str | None
) -> bool:
    """True if `name` is already defined as an alias/function/command.

    Scans the given dotfile texts for `alias NAME=` or `NAME()` definitions,
    and treats a non-None `shutil.which(name)` result as a live conflict.
    """
    if which_result:
        return True
    needles = (f"alias {name}=", f"{name}()", f"{name} ()")
    for text in dotfile_texts:
        for raw in text.splitlines():
            stripped = raw.strip()
            if any(stripped.startswith(n) for n in needles):
                return True
    return False


def pick_default_alias(candidates: list[str], conflicts: set[str]) -> str:
    """First candidate not in `conflicts`; falls back to the first candidate."""
    for c in candidates:
        if c not in conflicts:
            return c
    return candidates[0]


def assemble_alias_append(dotfile_text: str, selected_lines: list[str]) -> str:
    """Pure: the text to APPEND to the dotfile for the chosen alias lines.

    Skips any line already present (idempotent). Returns "" when nothing new
    needs writing. Includes the header only when at least one fresh line is
    being added and the header isn't already there.
    """
    fresh = [ln for ln in selected_lines if not alias_line_present(dotfile_text, ln)]
    if not fresh:
        return ""
    body = "\n".join(fresh) + "\n"
    if alias_line_present(dotfile_text, ALIAS_BLOCK_HEADER):
        header = ""
    else:
        header = f"{ALIAS_BLOCK_HEADER}\n"
    prefix = "" if dotfile_text.endswith("\n") or not dotfile_text else "\n"
    return f"{prefix}{header}{body}"


def export_editor_line(value: str, shell: str) -> str:
    """The line that sets $EDITOR, in the target dotfile's dialect.

    fish has no `export` builtin; appending one to config.fish would error on
    every shell start.
    """
    if shell == "fish":
        return f"set -gx EDITOR {shlex.quote(value)}"
    return f"export EDITOR={shlex.quote(value)}"


# --------------------------------------------------------------------------
# Imperative shell
# --------------------------------------------------------------------------

def _prompt_yn(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        # Piped/exhausted stdin: take the displayed default instead of crashing.
        print(f"{DIM}(no input — using default){RESET}")
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_text(question: str, default: str) -> str:
    try:
        answer = input(f"{question}\n  [default: {CYAN}{default}{RESET}] ").strip()
    except EOFError:
        print(f"{DIM}(no input — using default){RESET}")
        return default
    return answer or default


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup(path: Path) -> None:
    """Timestamped backup of `path` beside it, if it exists and is non-empty.

    Byte-for-byte copy: backups must succeed even for files that aren't
    valid UTF-8.
    """
    if path.exists() and path.stat().st_size > 0:
        backup = path.with_name(path.name + f".bak.{_timestamp()}")
        backup.write_bytes(path.read_bytes())
        print(f"  {GREEN}✓{RESET} Backed up {path.name} → {backup.name}")


def _tilde(path: Path) -> str:
    """Display form of `path` with the home dir collapsed to `~` when possible."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _read_dotfile_text(path: Path) -> str | None:
    """Read a dotfile as UTF-8; None (with a warning) if it can't be decoded.

    A latin-1 .bashrc must not abort the whole run mid-way — callers skip
    the offending file instead.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"  {YELLOW}!{RESET} {path} is not valid UTF-8 — leaving it untouched.")
        return None


def _xdg_download_dir() -> str:
    """Run `xdg-user-dir DOWNLOAD` if available; return "" on any failure."""
    if not shutil.which("xdg-user-dir"):
        return ""
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    candidate = out.stdout.strip()
    # xdg-user-dir echoes $HOME when DOWNLOAD is unset — not a real guess.
    if not candidate or candidate == str(Path.home()):
        return ""
    return candidate


def _checkbox_prompt(
    labels: list[str],
    descriptions: dict[str, str],
    default_checked: set[str] | None = None,
) -> list[str]:
    """Tiny dependency-free multi-select. Returns the chosen labels.

    Numbered-toggle UI (works on any TTY and degrades gracefully): the user
    types a number to flip that item's checkbox, then a bare Enter to confirm.
    `default_checked` is the set of labels that start checked (None = all).
    Non-interactive stdin -> the default-checked set is returned as-is.
    """
    checked = {
        label: (default_checked is None or label in default_checked)
        for label in labels
    }
    if not sys.stdin.isatty():
        return [label for label in labels if checked[label]]

    while True:
        print()
        for i, label in enumerate(labels, 1):
            box = f"{GREEN}[x]{RESET}" if checked[label] else "[ ]"
            print(f"  {box} {i}. {label}  {DIM}{descriptions.get(label, '')}{RESET}")
        # Two distinct actions, spelled out because it wasn't obvious that a
        # number toggles (rather than confirms) and that confirm is a bare Enter.
        print(
            f"  {DIM}Type a number and press Enter to check/uncheck that item "
            f"(e.g. '2'; space-separated for several: '1 3').{RESET}"
        )
        print(
            f"  {DIM}Press Enter on an empty line to confirm the checked items "
            f"above.{RESET}"
        )
        try:
            raw = input("  > ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return [label for label in labels if checked[label]]
        for tok in raw.replace(",", " ").split():
            if tok.isdigit() and 1 <= int(tok) <= len(labels):
                label = labels[int(tok) - 1]
                checked[label] = not checked[label]


def _infer_shell_name(env: dict) -> str:
    return Path((env.get("SHELL") or "").strip()).name


def step_chmod(repo_root: Path) -> None:
    print(f"\n{BOLD}1. Make entry scripts executable{RESET}")
    for name in ENTRY_SCRIPTS:
        p = repo_root / name
        if not p.exists():
            print(f"  {YELLOW}!{RESET} {name} not found — skip")
            continue
        if os.access(p, os.X_OK):
            print(f"  {DIM}{name} already executable — skip{RESET}")
            continue
        mode = p.stat().st_mode
        os.chmod(p, mode | 0o111)
        print(f"  {GREEN}✓{RESET} chmod +x {name}")


def step_env_file(repo_root: Path) -> Path:
    print(f"\n{BOLD}2. Create .env{RESET}")
    env_path = repo_root / ".env"
    example = repo_root / ".env.example"
    if env_path.exists():
        print(f"  {DIM}.env already exists — editing in place{RESET}")
    elif example.exists():
        shutil.copyfile(example, env_path)
        print(f"  {GREEN}✓{RESET} Copied .env.example → .env")
    else:
        print(f"  {YELLOW}!{RESET} .env.example not found — creating empty .env")
        env_path.write_text("", encoding="utf-8")
    return env_path


def step_zip_search_dir(env_path: Path, yes: bool) -> None:
    print(f"\n{BOLD}3. Set ZIP_SEARCH_DIR{RESET}")
    env = dict(os.environ)
    env["XDG_DOWNLOAD"] = _xdg_download_dir()
    guess = guess_downloads_dir(sys.platform, env)

    if yes:
        chosen = guess
        print(f"  Using {CYAN}{chosen}{RESET}")
    else:
        chosen = _prompt_text(
            "  Where do browser export .zip files land?", guess
        )

    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new_text = set_env_value(text, "ZIP_SEARCH_DIR", chosen)
    if new_text == text:
        print(f"  {DIM}ZIP_SEARCH_DIR already set to {chosen} — skip{RESET}")
    else:
        _backup(env_path)
        env_path.write_text(new_text, encoding="utf-8")
        print(f"  {GREEN}✓{RESET} Set ZIP_SEARCH_DIR={chosen}")
    print(
        f"  {DIM}Other settings have sensible defaults — open .env to read "
        f"what each does.{RESET}"
    )


def _readable_dotfiles() -> list[tuple[Path, str, str]]:
    """Existing candidate dotfiles as (path, shell, text), skipping non-UTF-8."""
    out = []
    for raw, shell in CANDIDATE_DOTFILES:
        p = Path(raw).expanduser()
        if not p.exists():
            continue
        text = _read_dotfile_text(p)
        if text is not None:
            out.append((p, shell, text))
    return out


def canonical_dotfile_targets(
    existing: list[tuple[Path, str, str]], cur_shell: str
) -> tuple[tuple[Path, str, str], list[tuple[Path, str, str]]]:
    """Collapse existing dotfiles to one canonical rc per shell.

    `existing` is in CANDIDATE_DOTFILES preference order, so the first file
    seen for a given shell tag is that shell's canonical rc — .bashrc beats
    .bash_profile for bash, etc. Returns (current, others):

      current  the (path, shell, text) to write to by default: the current
               shell's rc if present, else ~/.profile, else the first file.
      others   canonical rcs for *other* real shells (non-empty tag, not the
               current shell) — distinct parallel shells the user might also
               want aliases in. Empty in the common single-shell case, which
               is the signal to auto-pick `current` without prompting.

    Why fold ~/.profile into `current` instead of offering it: it's the login
    fallback bash reads only when no .bash_profile/.bashrc exists, never a
    separate shell worth a second copy of the aliases.
    """
    canonical: dict[str, tuple[Path, str, str]] = {}
    for entry in existing:
        canonical.setdefault(entry[1], entry)
    current = canonical.get(cur_shell) or canonical.get("") or existing[0]
    others = [
        entry
        for shell, entry in canonical.items()
        if shell and shell != cur_shell and entry != current
    ]
    return current, others


def _choose_alias_name(
    dotfile_texts: list[str], yes: bool, repo_root: Path
) -> str:
    """Pick the main search alias name, detecting conflicts."""
    own_stripped = [strip_managed_aliases(t, repo_root) for t in dotfile_texts]

    def conflicts(name: str) -> bool:
        return detect_alias_conflict(own_stripped, name, shutil.which(name))

    candidates = ["cs", *BACKUP_ALIAS_NAMES]
    conflicting = {c for c in candidates if conflicts(c)}
    default = pick_default_alias(candidates, conflicting)

    if "cs" in conflicting:
        print(
            f"  {YELLOW}!{RESET} 'cs' is already a command/alias — "
            f"suggesting {CYAN}{default}{RESET} instead."
        )
    if yes or not sys.stdin.isatty():
        return default

    while True:
        name = _prompt_text("  Alias name for the search command?", default)
        if not valid_alias_name(name):
            print(f"  {YELLOW}!{RESET} '{name}' isn't a valid alias name; try again.")
            continue
        if conflicts(name):
            if _prompt_yn(
                f"  {YELLOW}'{name}' looks like it already exists. Use it anyway?",
                default=False,
            ):
                return name
            continue
        return name


def _choose_alias_targets(
    existing: list[tuple[Path, str, str]], yes: bool
) -> list[tuple[Path, str, str]]:
    """Which dotfile(s) to write aliases to.

    Common case (one shell): auto-pick the current shell's rc, no prompt. Only
    when a *different* shell's rc is also present do we surface a multi-select,
    defaulting to just the current shell but letting the user tick the others.
    """
    cur_shell = _infer_shell_name(dict(os.environ))
    current, others = canonical_dotfile_targets(existing, cur_shell)
    if yes or not others or not sys.stdin.isatty():
        return [current]

    targets = [current, *others]
    labels = [str(p) for p, _, _ in targets]
    descriptions = {
        str(p): ("current shell" if shell == cur_shell else f"{shell} config")
        for p, shell, _ in targets
    }
    print("\n  You have config files for more than one shell. Pick where to")
    print("  write the aliases (the current shell's file is checked by default):")
    by_label = {label: entry for label, entry in zip(labels, targets)}
    chosen = _checkbox_prompt(labels, descriptions, default_checked={labels[0]})
    return [by_label[label] for label in chosen] or [current]


def step_aliases(repo_root: Path, yes: bool) -> tuple[dict[str, str], list[Path]]:
    """Run the alias step; return ({label: alias_name}, [dotfiles written]).

    The returned map (e.g. {"search": "cs", "sync-claude": "cs-sync-claude"})
    lets later output — including the Claude Code migration we shell out to —
    show the user's actual alias instead of the verbose `python3 ...` form.
    The dotfile list lets the final summary tell the user which file to
    `source`. Both are empty when no aliases were added this run.
    """
    print(f"\n{BOLD}4. Shell aliases{RESET}")
    existing = _readable_dotfiles()
    if not existing:
        print(f"  {YELLOW}!{RESET} No usable dotfiles found — skipping aliases.")
        print(f"  {DIM}(Looked for: {', '.join(r for r, _ in CANDIDATE_DOTFILES)}){RESET}")
        return {}, []

    if not (yes or _prompt_yn("  Add shell aliases?", default=True)):
        print(f"  {DIM}Skipped aliases.{RESET}")
        return {}, []

    targets = _choose_alias_targets(existing, yes)
    all_texts = [text for _, _, text in existing]
    name = _choose_alias_name(all_texts, yes, repo_root)

    lines = alias_lines(repo_root, name)
    descriptions = {
        "search": "full-text search across all chats",
        "sync-claude": "import the latest claude.ai export",
        "sync-chatgpt": "import the latest chatgpt.com export",
    }
    labels = ["search", "sync-claude", "sync-chatgpt"]
    label_to_line = {
        "search": lines["search"],
        "sync-claude": lines["sync-claude"],
        "sync-chatgpt": lines["sync-chatgpt"],
    }

    target_names = ", ".join(str(p) for p, _, _ in targets)
    print(f"\n{BOLD}  Planned aliases{RESET} (write to {CYAN}{target_names}{RESET}):")
    for label in labels:
        print(f"    {label_to_line[label]}")

    if yes:
        chosen = labels
    else:
        print(f"\n  {DIM}Choose which aliases to add:{RESET}")
        chosen = _checkbox_prompt(labels, descriptions)

    if not chosen:
        print(f"  {DIM}No aliases selected — skip.{RESET}")
        return {}, []
    selected_lines = [label_to_line[label] for label in chosen]

    written: list[Path] = []
    for dotfile, _shell, _text in targets:
        dotfile_text = _read_dotfile_text(dotfile)
        if dotfile_text is None:
            continue
        append = assemble_alias_append(dotfile_text, selected_lines)
        if not append:
            print(f"  {DIM}All selected aliases already present in {dotfile.name} — skip.{RESET}")
            continue
        if not yes and not _prompt_yn(f"  Append these to {dotfile.name}?", default=True):
            print(f"  {DIM}Skipped writing to {dotfile.name}.{RESET}")
            continue
        _backup(dotfile)
        with dotfile.open("a", encoding="utf-8") as f:
            f.write(append)
        print(f"  {GREEN}✓{RESET} Added alias(es) to {dotfile}")
        written.append(dotfile)

    if not written:
        # Either declined every write or the lines were already present; in the
        # latter case the final-output resolver still finds them by scanning the
        # rc, so reporting nothing here is fine.
        return {}, []

    print(f"  {DIM}Run `source <dotfile>` or open a new shell to use them.{RESET}")
    alias_names = {
        "search": name,
        "sync-claude": f"{name}-sync-claude",
        "sync-chatgpt": f"{name}-sync-chatgpt",
    }
    return {label: alias_names[label] for label in chosen}, written


def step_editor(yes: bool) -> None:
    print(f"\n{BOLD}5. Verify $EDITOR{RESET}")
    editor = os.environ.get("EDITOR", "").strip()
    if editor:
        print(f"  {DIM}$EDITOR is set to '{editor}' — OK{RESET}")
        return

    print(
        f"  {YELLOW}!{RESET} $EDITOR is not set. It's used to open chats as "
        f"Markdown (hit 'v' on a search result)."
    )
    print(f"  {DIM}The app falls back to 'vim' if you leave it unset.{RESET}")

    if yes:
        print(f"  {DIM}--yes: leaving $EDITOR unset (vim fallback).{RESET}")
        return

    if not _prompt_yn("  Add `export EDITOR=...` to a dotfile now?", default=False):
        print(f"  {DIM}Skipped.{RESET}")
        return

    existing = _readable_dotfiles()
    if not existing:
        print(f"  {YELLOW}!{RESET} No usable dotfiles found — skipping.")
        return
    current, _others = canonical_dotfile_targets(existing, _infer_shell_name(dict(os.environ)))
    dotfile, shell, dotfile_text = current
    value = _prompt_text("  What editor command?", "vim")
    line = export_editor_line(value, shell)

    if alias_line_present(dotfile_text, line):
        print(f"  {DIM}{line} already present — skip.{RESET}")
        return

    if not _prompt_yn(f"  Append `{line}` to {dotfile.name}?", default=True):
        print(f"  {DIM}Skipped.{RESET}")
        return
    _backup(dotfile)
    prefix = "" if dotfile_text.endswith("\n") or not dotfile_text else "\n"
    with dotfile.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{line}\n")
    print(f"  {GREEN}✓{RESET} Added {line} to {dotfile}")
    print(f"  {DIM}Run `source {dotfile}` or open a new shell to apply.{RESET}")


def step_claude_code_migration(
    repo_root: Path, yes: bool, claude_hooks: bool, search_alias: str | None = None
) -> bool:
    """Returns False only when the migration ran and failed."""
    print(f"\n{BOLD}6. Claude Code archival setup{RESET}")
    migration = repo_root / "migrations" / "002_setup_claude_code_archival.py"
    if not migration.exists():
        print(f"  {YELLOW}!{RESET} {migration} not found — skip.")
        return True
    if yes and not claude_hooks:
        # This step installs Stop/SessionEnd hooks into ~/.claude/settings.json
        # (code that runs on every Claude Code session) — too consequential to
        # bundle into a blanket --yes. Require the explicit flag.
        print(f"  {DIM}Skipped: --yes alone never installs Claude Code hooks.{RESET}")
        print(f"  {DIM}Re-run with --yes --claude-hooks, or run it interactively:{RESET}")
        print(f"    python3 {migration}")
        return True
    if not (yes or _prompt_yn("  Run Claude Code archival setup now?", default=True)):
        print(f"  {DIM}Skipped. Run it later with:{RESET}")
        print(f"    python3 {migration}")
        return True
    cmd = [sys.executable, str(migration)]
    if yes:
        cmd.append("--yes")
    if search_alias:
        # So the migration's closing "verify it works" hint shows the alias we
        # just wrote (e.g. `cs "query"`) instead of the verbose python3 form.
        cmd += ["--search-alias", search_alias]
    print(f"  {DIM}Running: {' '.join(cmd)}{RESET}\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  {RED}✗ Claude Code archival setup exited with status {result.returncode}.{RESET}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive post-clone setup for clauding-at-home",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Take all defaults and run non-interactively (skips step 6 "
             "unless --claude-hooks is also given)",
    )
    parser.add_argument(
        "--claude-hooks", action="store_true",
        help="With --yes: also run the Claude Code archival setup, which "
             "installs Stop/SessionEnd hooks into ~/.claude/settings.json",
    )
    args = parser.parse_args()

    repo_root = _REPO_ROOT

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  clauding-at-home setup{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"\n  Repository root: {CYAN}{repo_root}{RESET}")
    if args.yes:
        print(f"  {DIM}--yes: taking defaults throughout.{RESET}")

    step_chmod(repo_root)
    env_path = step_env_file(repo_root)
    step_zip_search_dir(env_path, args.yes)
    aliases, alias_dotfiles = step_aliases(repo_root, args.yes)
    step_editor(args.yes)
    migration_ok = step_claude_code_migration(
        repo_root, args.yes, args.claude_hooks, aliases.get("search")
    )

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if migration_ok:
        print(f"{GREEN}{BOLD}  Setup complete!{RESET}")
    else:
        print(f"{RED}{BOLD}  Setup finished, but step 6 failed (see above).{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    # Show the user's own aliases when we know them — ones we just wrote
    # (aliases dict) or, failing that, ones already in their current shell's rc.
    # Otherwise fall back to the always-correct `python3 <path>` form.
    rc_texts = current_shell_rc_texts()
    search_cmd = resolve_invocation(
        repo_root / "full_text_search_chats_archive.py",
        explicit_alias=aliases.get("search"),
        dotfile_texts=rc_texts,
    )
    sync_path = repo_root / "sync_local_chats_archive.py"
    sync_claude_cmd = resolve_invocation(
        sync_path, "--claude",
        explicit_alias=aliases.get("sync-claude"), dotfile_texts=rc_texts,
    )
    sync_chatgpt_cmd = resolve_invocation(
        sync_path, "--chatgpt",
        explicit_alias=aliases.get("sync-chatgpt"), dotfile_texts=rc_texts,
    )
    # Only show the "Load your aliases" step when we actually wrote a dotfile
    # this run — otherwise there's nothing new to source.
    if alias_dotfiles:
        source_cmds = "\n".join(f"      source {_tilde(p)}" for p in alias_dotfiles)
        load_block = f"""
    {BOLD}Load your aliases:{RESET}
{source_cmds}
"""
    else:
        load_block = ""

    print(f"""
  Next steps:
{load_block}
    {BOLD}Search your chats:{RESET}
      {search_cmd} "some query"

    {BOLD}Export + import chats:{RESET}
      See "Export Your Chats" in README.md. In short: download
      a claude.ai / chatgpt.com export .zip into your ZIP_SEARCH_DIR, then run
      {sync_claude_cmd}   (or {sync_chatgpt_cmd})
""")
    if not migration_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
