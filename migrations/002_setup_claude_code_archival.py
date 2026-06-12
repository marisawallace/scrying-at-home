#!/usr/bin/env python3
"""
Migration 002: Set up Claude Code session archival.

Wires the in-repo `claude_code_hook.py` into Claude Code so every Stop /
SessionEnd event reconciles ~/.claude/projects/ JSONLs into a per-host
archive that the search/view tools index.

What it does:
  1. Prompts for a human-readable name for this machine (defaults to a
     normalized socket.gethostname()), writes it as CLAUDE_CODE_HOST in .env
  2. Adds Stop + SessionEnd hooks to ~/.claude/settings.json (with backup)
  3. Upserts CLAUDE_CODE_SOURCES=<host>=<archive-path> in .env
  4. Creates data/llm_data/claude-code/<host>/

Usage:
  python3 migrations/002_setup_claude_code_archival.py
  python3 migrations/002_setup_claude_code_archival.py --yes   # skip prompts

To uninstall: delete the Stop and SessionEnd entries pointing at
claude_code_hook.py in ~/.claude/settings.json, and unset
CLAUDE_CODE_HOST and CLAUDE_CODE_SOURCES in .env.
"""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

# ANSI colors (match migration 001)
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

REPO_MARKER = "claude_code_hook.py"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Resolve the repo root early so we can import the shared paths helpers
# without each helper site repeating the sys.path dance.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from paths import (  # noqa: E402
    CLAUDE_CODE_HOST_ENV_KEY,
    CLAUDE_CODE_SOURCES_ENV_KEY,
    load_env_file,
    normalize_hostname,
    parse_sources_string,
    resolve_data_dir,
)


def find_repo_root() -> Path | None:
    """Find the repo root by looking for claude_code_hook.py."""
    candidate = Path(__file__).resolve().parent
    for _ in range(4):
        if (candidate / REPO_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return None


def hook_command(repo_root: Path) -> str:
    """Build the hook command line.

    Uses sys.executable (the interpreter that ran the migration) rather than
    bare `python3`, since hooks are spawned by Claude Code with whatever PATH
    it inherited — which may not include pyenv/asdf/brew shims. Both the
    interpreter and script path are shell-quoted so paths with spaces work.
    """
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(repo_root / REPO_MARKER))}"


def settings_already_installed(settings: dict, command: str) -> tuple[bool, bool]:
    """Return (stop_installed, sessionend_installed)."""

    def has_command(event: str) -> bool:
        for matcher in settings.get("hooks", {}).get(event, []):
            for h in matcher.get("hooks", []):
                if h.get("type") == "command" and h.get("command") == command:
                    return True
        return False

    return has_command("Stop"), has_command("SessionEnd")


def add_hook(settings: dict, event: str, command: str) -> None:
    hooks_section = settings.setdefault("hooks", {})
    event_list = hooks_section.setdefault(event, [])
    event_list.append({"hooks": [{"type": "command", "command": command}]})


def serialize_sources(pairs: list[tuple[str, str]]) -> str:
    return ",".join(f"{h}={p}" for h, p in pairs)


def match_env_key(stripped_line: str, key: str) -> tuple[bool, bool]:
    """Classify a (whitespace-stripped) .env line against `key`.

    Returns (matches, is_commented). A line matches if, after dropping any
    leading `#` characters and surrounding whitespace, it reads `key=...`.
    This recognizes both the active form (`KEY=value`) and the commented
    documentation forms shipped in .env.example — `#KEY=value` *and*
    `# KEY=value` (space after the hash) — so the migration edits the example
    lines in place instead of appending duplicates beneath them. Pure comment
    lines (`#`, `# some prose`) don't start with `key=` and so don't match.
    """
    is_commented = stripped_line.startswith("#")
    body = stripped_line.lstrip("#").strip()
    return body.startswith(f"{key}="), is_commented


def _diff_status(old_text: str, new_text: str, had_active: bool) -> str:
    """Classify the effect of a rewrite for console reporting.

    "unchanged" when nothing moved; "updated" when an active line already
    existed and was rewritten; "added" when the key was newly created or
    only commented documentation existed before (now activated).
    """
    if new_text == old_text:
        return "unchanged"
    return "updated" if had_active else "added"


def upsert_env_scalar(env_path: Path, key: str, value: str) -> str:
    """Add or update a scalar KEY=VALUE in env_path.

    Collapses *every* line that sets `key` — active or commented-out
    documentation, however many — into a single active `key=value` line at
    the position of the first such line, dropping the rest. This keeps a
    re-run from leaving a duplicate when an active line and a commented
    .env.example line coexist. Returns "added" | "updated" | "unchanged".
    """
    old_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new_lines: list[str] = []
    anchor: int | None = None
    leading = ""
    had_active = False

    for line in old_text.splitlines():
        matches, is_commented = match_env_key(line.strip(), key)
        if not matches:
            new_lines.append(line)
            continue
        had_active = had_active or not is_commented
        if anchor is None:
            anchor = len(new_lines)
            leading = line[: len(line) - len(line.lstrip())]
            new_lines.append("")  # reserved; filled after the loop
        # any further matching line is a duplicate — drop it

    if anchor is None:
        new_lines.append(f"{key}={value}")
    else:
        new_lines[anchor] = f"{leading}{key}={value}"

    new_text = "\n".join(new_lines) + "\n"
    env_path.write_text(new_text, encoding="utf-8")
    return _diff_status(old_text, new_text, had_active)


def upsert_env_sources(env_path: Path, hostname: str, archive_path: str) -> str:
    """
    Add or update the entry for hostname in CLAUDE_CODE_SOURCES.

    Merges the host→path pairs from every *active* CLAUDE_CODE_SOURCES line
    (commented .env.example lines contribute nothing — their laptop=/desktop=
    pairs are placeholders), sets/overrides this host's entry, and writes a
    single collapsed line at the first match's position. Returns
    "added" | "updated" | "unchanged".
    """
    old_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new_lines: list[str] = []
    anchor: int | None = None
    leading = ""
    had_active = False
    host_to_path: dict[str, str] = {}

    for line in old_text.splitlines():
        stripped = line.strip()
        matches, is_commented = match_env_key(stripped, CLAUDE_CODE_SOURCES_ENV_KEY)
        if not matches:
            new_lines.append(line)
            continue
        if not is_commented:
            had_active = True
            raw_value = stripped.split("=", 1)[1] if "=" in stripped else ""
            host_to_path.update(parse_sources_string(raw_value))
        if anchor is None:
            anchor = len(new_lines)
            leading = line[: len(line) - len(line.lstrip())]
            new_lines.append("")  # reserved; filled after the loop
        # any further matching line is a duplicate — drop it

    host_to_path[hostname] = archive_path
    pairs = list(host_to_path.items())
    if anchor is None:
        new_lines.append(f"CLAUDE_CODE_SOURCES={serialize_sources(pairs)}")
    else:
        new_lines[anchor] = f"{leading}CLAUDE_CODE_SOURCES={serialize_sources(pairs)}"

    new_text = "\n".join(new_lines) + "\n"
    env_path.write_text(new_text, encoding="utf-8")
    return _diff_status(old_text, new_text, had_active)


def _human_bytes(n: float) -> str:
    """Format a byte count like '1.4 GB' or '312 KB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n} B"


def _prompt_yn(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _run_backfill(cch, jsonls: list, archive_dir: Path) -> None:
    """Walk every JSONL through the hook's sync_transcript and report progress."""
    total = len(jsonls)
    new_lines_total = 0
    files_changed = 0
    bytes_done = 0
    bytes_total = sum(
        (j.stat().st_size if j.exists() else 0) for j in jsonls
    )

    print(f"\n{BOLD}Backfilling...{RESET}")
    for i, jsonl in enumerate(jsonls, 1):
        try:
            cch.validate_source_path(jsonl)
            archive_path = cch.get_archive_path(jsonl, archive_dir)
        except ValueError as e:
            print(f"  {YELLOW}skip{RESET} {jsonl.name}: {e}")
            continue

        try:
            new_lines = cch.sync_transcript(jsonl, archive_path)
        except OSError as e:
            print(f"  {YELLOW}skip{RESET} {jsonl.name}: {e}")
            continue

        try:
            bytes_done += jsonl.stat().st_size
        except OSError:
            pass

        if new_lines > 0:
            files_changed += 1
            new_lines_total += new_lines

        # Single-line progress: file count, percentage of total bytes.
        pct = (bytes_done / bytes_total * 100) if bytes_total else 100
        print(
            f"  [{i}/{total}] {pct:5.1f}%  "
            f"{_human_bytes(bytes_done)} / {_human_bytes(bytes_total)}",
            end="\r",
            flush=True,
        )

    print()  # finish the progress line
    print(
        f"  {GREEN}✓{RESET} Backfill complete: "
        f"{new_lines_total} new line(s) across {files_changed} file(s)."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Migration 002: Set up Claude Code session archival",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Migration 002: Claude Code session archival{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    repo_root = find_repo_root()
    if repo_root is None:
        print(f"{RED}ERROR: Could not find clauding-at-home repo root.{RESET}")
        print(f"  Looked for {REPO_MARKER} starting from {Path(__file__).resolve().parent}.")
        sys.exit(1)

    env_path = repo_root / ".env"
    existing_env = load_env_file(env_path)

    # Resolve the human-readable host name. Prefer an existing CLAUDE_CODE_HOST
    # entry so re-running the migration is idempotent. Otherwise prompt the
    # user, defaulting to a normalized gethostname() (lowercased, .local
    # stripped). The override exists because macOS hostnames flip around with
    # network conditions; a hand-picked name is stable and human-readable.
    raw_host = socket.gethostname()
    default_host = normalize_hostname(raw_host) or raw_host
    existing_host = existing_env.get(CLAUDE_CODE_HOST_ENV_KEY, "").strip()
    if existing_host:
        hostname = existing_host
    elif args.yes:
        hostname = default_host
    else:
        prompt = (
            f"\nWhat name do you want to give this machine?\n"
            f"  (used to tag search results and pick this host's archive dir)\n"
            f"  [default: {CYAN}{default_host}{RESET}] "
        )
        answer = input(prompt).strip()
        hostname = answer or default_host

    # Place this host's archive under llm_data per the claude-code/<host>
    # convention, but honor an LLM_DATA_DIR override from .env so relocating
    # llm_data carries the archives with it instead of stranding them at the
    # hardcoded default.
    archive_dir = resolve_data_dir(repo_root, existing_env) / "claude-code" / hostname
    command = hook_command(repo_root)

    print(f"\n  Repository root:  {CYAN}{repo_root}{RESET}")
    print(f"  Host name:        {CYAN}{hostname}{RESET}")
    print(f"  Archive path:     {CYAN}{archive_dir}{RESET}")
    print(f"  Hook command:     {CYAN}{command}{RESET}")
    print(f"  Settings file:    {CYAN}{SETTINGS_PATH}{RESET}")
    print(f"  .env file:        {CYAN}{env_path}{RESET}")

    # Load (or initialize) settings.json
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            print(f"\n{RED}ERROR: {SETTINGS_PATH} is not valid JSON: {e}{RESET}")
            print("  Fix it manually before re-running this migration.")
            sys.exit(1)
    else:
        settings = {}

    stop_done, send_done = settings_already_installed(settings, command)

    print(f"\n{BOLD}Planned changes:{RESET}")
    settings_changes_needed = not (stop_done and send_done)
    if stop_done:
        print(f"  {DIM}Stop hook already installed — skip{RESET}")
    else:
        print(f"  {GREEN}+{RESET} Add Stop hook → {command}")
    if send_done:
        print(f"  {DIM}SessionEnd hook already installed — skip{RESET}")
    else:
        print(f"  {GREEN}+{RESET} Add SessionEnd hook → {command}")

    # Compute env change preview without writing
    existing_pairs = dict(
        parse_sources_string(existing_env.get(CLAUDE_CODE_SOURCES_ENV_KEY, ""))
    )
    raw_existing = existing_env.get(CLAUDE_CODE_SOURCES_ENV_KEY, "")

    # Collision guard: another machine already registered this hostname with
    # a different archive path. Common when default macOS hostnames like
    # "MacBook-Pro.local" collide across users sharing the repo. Refuse to
    # silently repoint the other host's hook.
    if (
        hostname in existing_pairs
        and existing_pairs[hostname] != str(archive_dir)
    ):
        prior_path = existing_pairs[hostname]
        prior_exists = Path(prior_path).exists()
        if prior_exists:
            print(
                f"\n{RED}ERROR: hostname collision in {CLAUDE_CODE_SOURCES_ENV_KEY}.{RESET}"
            )
            print(
                f"  An entry for {CYAN}{hostname}{RESET} already points at:\n"
                f"    {CYAN}{prior_path}{RESET}\n"
                f"  which exists on disk and may belong to a different machine."
            )
            print(
                f"  Refusing to overwrite. If this *is* the same machine and the\n"
                f"  archive moved, edit {env_path} manually. If it's a different\n"
                f"  machine that happens to share this hostname, give one of them\n"
                f"  a unique name first (e.g. rename the host)."
            )
            sys.exit(1)

    if existing_pairs.get(hostname) == str(archive_dir):
        env_action = "unchanged"
        print(f"  {DIM}.env CLAUDE_CODE_SOURCES already has {hostname}={archive_dir} — skip{RESET}")
    elif hostname in existing_pairs:
        env_action = "update"
        print(
            f"  {YELLOW}~{RESET} .env CLAUDE_CODE_SOURCES[{hostname}]: "
            f"{existing_pairs[hostname]} → {archive_dir}"
        )
    elif raw_existing:
        env_action = "append"
        print(f"  {GREEN}+{RESET} .env CLAUDE_CODE_SOURCES: append {hostname}={archive_dir}")
    else:
        env_action = "create"
        print(f"  {GREEN}+{RESET} .env CLAUDE_CODE_SOURCES={hostname}={archive_dir}")

    if existing_host == hostname:
        host_action = "unchanged"
        print(f"  {DIM}.env CLAUDE_CODE_HOST already set to {hostname} — skip{RESET}")
    elif existing_host:
        host_action = "update"
        print(
            f"  {YELLOW}~{RESET} .env CLAUDE_CODE_HOST: {existing_host} → {hostname}"
        )
    else:
        host_action = "add"
        print(f"  {GREEN}+{RESET} .env CLAUDE_CODE_HOST={hostname}")

    archive_dir_exists = archive_dir.exists()
    if archive_dir_exists:
        print(f"  {DIM}Archive dir already exists — skip mkdir{RESET}")
    else:
        print(f"  {GREEN}+{RESET} mkdir -p {archive_dir}")

    if (
        not settings_changes_needed
        and env_action == "unchanged"
        and host_action == "unchanged"
        and archive_dir_exists
    ):
        print(f"\n{GREEN}✓ Already installed — nothing to do.{RESET}\n")
        sys.exit(0)

    if not args.yes:
        print(f"\n{YELLOW}This will modify {SETTINGS_PATH} and {env_path}.{RESET}")
        print(f"{YELLOW}Timestamped backups will be made before writing (*.bak.TIMESTAMP).{RESET}")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    print(f"\n{BOLD}Applying...{RESET}")

    # 1. settings.json
    if settings_changes_needed:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SETTINGS_PATH.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = SETTINGS_PATH.with_suffix(f".json.bak.{ts}")
            backup.write_text(SETTINGS_PATH.read_text())
            print(f"  {GREEN}✓{RESET} Backed up settings.json → {backup.name}")

        if not stop_done:
            add_hook(settings, "Stop", command)
        if not send_done:
            add_hook(settings, "SessionEnd", command)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"  {GREEN}✓{RESET} Updated {SETTINGS_PATH}")

    # 2. .env
    if env_action != "unchanged" or host_action != "unchanged":
        env_path.touch(exist_ok=True)
        if env_path.exists() and env_path.stat().st_size > 0:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = env_path.with_suffix(f".bak.{ts}")
            backup.write_text(env_path.read_text())
            print(f"  {GREEN}✓{RESET} Backed up .env → {backup.name}")
    if host_action != "unchanged":
        result = upsert_env_scalar(env_path, CLAUDE_CODE_HOST_ENV_KEY, hostname)
        print(f"  {GREEN}✓{RESET} .env CLAUDE_CODE_HOST: {result}")
    if env_action != "unchanged":
        result = upsert_env_sources(env_path, hostname, str(archive_dir))
        print(f"  {GREEN}✓{RESET} .env CLAUDE_CODE_SOURCES: {result}")

    # 3. archive dir
    if not archive_dir_exists:
        archive_dir.mkdir(parents=True, exist_ok=True)
        print(f"  {GREEN}✓{RESET} Created {archive_dir}")

    # 4. Optional backfill of existing ~/.claude/projects/ history.
    #    Uses the same sync code as the runtime hook so behavior matches.
    import claude_code_hook as cch  # noqa: E402

    projects_dir = cch.CLAUDE_PROJECTS_DIR
    if projects_dir.exists():
        jsonls = list(projects_dir.rglob("*.jsonl"))
        total_bytes = 0
        for j in jsonls:
            try:
                total_bytes += j.stat().st_size
            except OSError:
                pass

        if jsonls:
            size_str = _human_bytes(total_bytes)
            print(
                f"\n{BOLD}Backfill existing Claude Code history?{RESET}\n"
                f"  Found {CYAN}{len(jsonls)}{RESET} JSONL file(s) "
                f"under {CYAN}{projects_dir}{RESET} ({CYAN}{size_str}{RESET}).\n"
                f"  Importing now is faster than letting it happen on the\n"
                f"  first SessionEnd (which would block Claude Code's exit).\n"
                f"  Skip safely — the next SessionEnd will sweep these in."
            )
            do_backfill = args.yes or _prompt_yn("Run backfill now?", default=True)
            if do_backfill:
                _run_backfill(cch, jsonls, archive_dir)
        else:
            print(f"\n{DIM}No existing JSONL transcripts to backfill.{RESET}")
    else:
        print(
            f"\n{DIM}{projects_dir} does not exist yet — skipping backfill.{RESET}"
        )

    # Done
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Setup complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"""
  How it works:
    Every time a Claude Code session ends (Stop or SessionEnd), the hook
    reconciles ~/.claude/projects/*.jsonl into:

      {CYAN}{archive_dir}{RESET}

    {BOLD}Stop{RESET}        — scans the current session's project dir.
                  Catches the parent session and any subagent transcripts.
    {BOLD}SessionEnd{RESET}  — full sweep of ~/.claude/projects/.
                  Backfills anything missed (crashed sessions, etc.).

    The sync is append-only and idempotent: it line-count compares the
    archive against the source and writes only the new tail.

    {BOLD}Assumption:{RESET} Claude Code JSONL transcripts are immutable append-only
    logs. If that ever changes, archives could diverge — the script writes
    to {CYAN}claude_code_anomalies.log{RESET} as a canary.

  {BOLD}Verify it's working:{RESET}
    1. Open and exit any Claude Code session.
       (If you skipped the optional backfill above, the next SessionEnd will
       sweep any existing history into the archive — that first sweep can be
       slow if your ~/.claude/projects/ tree is large, since it blocks
       Claude Code's exit.)
    2. {CYAN}python3 full_text_search_chats_archive.py <some query>{RESET}
       Results from this machine will be tagged with hostname {CYAN}{hostname}{RESET}.

  {BOLD}To uninstall:{RESET}
    Delete the Stop and SessionEnd entries pointing at {REPO_MARKER}
    in {SETTINGS_PATH}, and unset CLAUDE_CODE_HOST and
    CLAUDE_CODE_SOURCES in .env.
""")


if __name__ == "__main__":
    main()
