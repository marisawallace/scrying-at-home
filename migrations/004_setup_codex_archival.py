#!/usr/bin/env python3
"""
Migration 004: Set up OpenAI Codex session archival.

The Codex sibling of migration 002. Wires the in-repo `codex_sync.py` into Codex
via a Stop lifecycle hook so every completed turn reconciles
$CODEX_HOME/sessions/ rollouts into a per-host archive that the search/view tools
index.

What it does:
  1. Resolves a human-readable name for this machine (reuses MACHINE_NAME, or a
     legacy CLAUDE_CODE_HOST, if migration 002 already set it; otherwise prompts,
     default normalized gethostname()) and persists it as MACHINE_NAME. The host
     identity is shared by both providers.
  2. Adds a Stop hook to $CODEX_HOME/hooks.json (with backup).
  3. Upserts CODEX_SOURCES=<host>=<archive-path> in .env.
  4. Creates data/llm_data/codex/<host>/.
  5. Optionally backfills existing $CODEX_HOME/sessions/ history via codex_sync.

Why a Stop hook + sweep: the Codex Stop event fires per turn and — unlike Claude
Code — its payload carries NO transcript_path, so codex_sync sweeps the whole
sessions tree on each invocation (idempotent and cheap; see codex_sync.py).

Hook trust: Codex will not run an untrusted hook. After this migration installs
hooks.json, start Codex and run `/hooks` to review and trust it (a one-time
"Hooks need review" prompt). Headless automation can instead pass
`codex exec --dangerously-bypass-hook-trust`.

Usage:
  python3 migrations/004_setup_codex_archival.py
  python3 migrations/004_setup_codex_archival.py --yes   # skip prompts

To uninstall: delete the Stop entry pointing at codex_sync.py in
$CODEX_HOME/hooks.json, and unset CODEX_SOURCES in .env.
"""

from __future__ import annotations

import argparse
import json
import shlex
import socket
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

REPO_MARKER = "codex_sync.py"

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from scrying_at_home.config.paths import (  # noqa: E402
    CLAUDE_CODE_HOST_ENV_KEY,
    CODEX_SOURCES_ENV_KEY,
    MACHINE_NAME_ENV_KEY,
    active_env_values,
    codex_home,
    explicit_host_name,
    load_env_file,
    normalize_hostname,
    parse_sources_string,
    remove_env_key,
    resolve_data_dir,
    resolve_invocation,
    set_env_value,
)


def find_repo_root() -> Path | None:
    """Find the repo root by looking for codex_sync.py."""
    candidate = Path(__file__).resolve().parent
    for _ in range(4):
        if (candidate / REPO_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return None


def hook_command(repo_root: Path) -> str:
    """Build the hook command line.

    Uses sys.executable (the interpreter that ran the migration) rather than
    bare `python3`, since the hook is spawned by Codex with whatever PATH it
    inherited. Both interpreter and script path are shell-quoted.
    """
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(repo_root / REPO_MARKER))}"


def stop_hook_installed(hooks_config: dict, command: str) -> bool:
    """True if a Stop hook already runs `command` (Codex hooks.json shape)."""
    for matcher in hooks_config.get("hooks", {}).get("Stop", []):
        for h in matcher.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == command:
                return True
    return False


def add_stop_hook(hooks_config: dict, command: str) -> None:
    """Append a Stop command hook in the Codex hooks.json structure:
    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ...}]}]}}."""
    hooks_section = hooks_config.setdefault("hooks", {})
    stop_list = hooks_section.setdefault("Stop", [])
    stop_list.append({"hooks": [{"type": "command", "command": command}]})


def serialize_sources(pairs: list[tuple[str, str]]) -> str:
    return ",".join(f"{h}={p}" for h, p in pairs)


def merged_source_pairs(env_text: str) -> dict[str, str]:
    """Merge host→path pairs from every active CODEX_SOURCES line (later wins)."""
    pairs: dict[str, str] = {}
    for raw_value in active_env_values(env_text, CODEX_SOURCES_ENV_KEY):
        pairs.update(parse_sources_string(raw_value, CODEX_SOURCES_ENV_KEY))
    return pairs


def _human_bytes(n: float) -> str:
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


def main():
    parser = argparse.ArgumentParser(
        description="Migration 004: Set up OpenAI Codex session archival",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    parser.add_argument(
        "--search-alias", default="",
        help="Alias name for the search command (e.g. 'cs'), shown in the "
             "closing hint instead of the python3 form.",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Migration 004: OpenAI Codex session archival{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    repo_root = find_repo_root()
    if repo_root is None:
        print(f"{RED}ERROR: Could not find scrying-at-home repo root.{RESET}")
        print(f"  Looked for {REPO_MARKER} starting from {Path(__file__).resolve().parent}.")
        sys.exit(1)

    codex_dir = codex_home()
    sessions_dir = codex_dir / "sessions"
    hooks_path = codex_dir / "hooks.json"

    if not codex_dir.exists():
        print(f"{RED}ERROR: Codex home {codex_dir} does not exist.{RESET}")
        print("  Install and run Codex once first, or set $CODEX_HOME.")
        sys.exit(1)

    env_path = repo_root / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    existing_env = load_env_file(env_path)

    # Reuse the shared MACHINE_NAME (or legacy CLAUDE_CODE_HOST, set by migration
    # 002) for a consistent per-machine identity. Otherwise prompt, defaulting to
    # a normalized hostname. The host identity is shared by both providers.
    raw_host = socket.gethostname()
    default_host = normalize_hostname(raw_host) or raw_host
    canonical_host = existing_env.get(MACHINE_NAME_ENV_KEY, "").strip()
    legacy_host = existing_env.get(CLAUDE_CODE_HOST_ENV_KEY, "").strip()
    existing_host = explicit_host_name(existing_env)
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
        hostname = input(prompt).strip() or default_host

    archive_dir = resolve_data_dir(repo_root, existing_env) / "codex" / hostname
    command = hook_command(repo_root)

    print(f"\n  Repository root:  {CYAN}{repo_root}{RESET}")
    print(f"  Codex home:       {CYAN}{codex_dir}{RESET}")
    print(f"  Host name:        {CYAN}{hostname}{RESET}")
    print(f"  Archive path:     {CYAN}{archive_dir}{RESET}")
    print(f"  Hook command:     {CYAN}{command}{RESET}")
    print(f"  Hooks file:       {CYAN}{hooks_path}{RESET}")
    print(f"  .env file:        {CYAN}{env_path}{RESET}")

    # Load (or initialize) hooks.json
    if hooks_path.exists():
        try:
            hooks_config = json.loads(hooks_path.read_text())
        except json.JSONDecodeError as e:
            print(f"\n{RED}ERROR: {hooks_path} is not valid JSON: {e}{RESET}")
            print("  Fix it manually before re-running this migration.")
            sys.exit(1)
    else:
        hooks_config = {}

    stop_done = stop_hook_installed(hooks_config, command)

    print(f"\n{BOLD}Planned changes:{RESET}")
    if stop_done:
        print(f"  {DIM}Stop hook already installed — skip{RESET}")
    else:
        print(f"  {GREEN}+{RESET} Add Stop hook → {command}")

    try:
        existing_pairs = merged_source_pairs(env_text)
    except ValueError as e:
        print(f"\n{RED}ERROR: {env_path} has a malformed {CODEX_SOURCES_ENV_KEY} line.{RESET}")
        print(f"  {e}\n  Fix it manually before re-running this migration.")
        sys.exit(1)

    # Collision guard: another machine already registered this hostname with a
    # different, existing archive path. Refuse to silently repoint it.
    if hostname in existing_pairs and existing_pairs[hostname] != str(archive_dir):
        prior_path = existing_pairs[hostname]
        if Path(prior_path).exists():
            print(f"\n{RED}ERROR: hostname collision in {CODEX_SOURCES_ENV_KEY}.{RESET}")
            print(
                f"  An entry for {CYAN}{hostname}{RESET} already points at:\n"
                f"    {CYAN}{prior_path}{RESET}\n"
                f"  which exists on disk and may belong to a different machine.\n"
                f"  Refusing to overwrite. Give one machine a unique host name first."
            )
            sys.exit(1)

    if existing_pairs.get(hostname) == str(archive_dir):
        env_action = "unchanged"
        print(f"  {DIM}.env {CODEX_SOURCES_ENV_KEY} already has {hostname}={archive_dir} — skip{RESET}")
    elif hostname in existing_pairs:
        env_action = "update"
        print(f"  {YELLOW}~{RESET} .env {CODEX_SOURCES_ENV_KEY}[{hostname}]: "
              f"{existing_pairs[hostname]} → {archive_dir}")
    elif existing_pairs:
        env_action = "append"
        print(f"  {GREEN}+{RESET} .env {CODEX_SOURCES_ENV_KEY}: append {hostname}={archive_dir}")
    else:
        env_action = "create"
        print(f"  {GREEN}+{RESET} .env {CODEX_SOURCES_ENV_KEY}={hostname}={archive_dir}")

    if canonical_host == hostname and not legacy_host:
        host_action = "unchanged"
        print(f"  {DIM}.env {MACHINE_NAME_ENV_KEY} already set to {hostname} — skip{RESET}")
    elif legacy_host and not canonical_host:
        host_action = "migrate"
        print(f"  {YELLOW}~{RESET} .env: rename {CLAUDE_CODE_HOST_ENV_KEY} → "
              f"{MACHINE_NAME_ENV_KEY}={hostname}")
    elif canonical_host:
        host_action = "update"
        print(f"  {YELLOW}~{RESET} .env {MACHINE_NAME_ENV_KEY}: {canonical_host} → {hostname}")
    else:
        host_action = "add"
        print(f"  {GREEN}+{RESET} .env {MACHINE_NAME_ENV_KEY}={hostname}")

    new_pairs = dict(existing_pairs)
    new_pairs[hostname] = str(archive_dir)
    new_env_text = set_env_value(
        env_text, CODEX_SOURCES_ENV_KEY, serialize_sources(list(new_pairs.items())))
    # Persist the shared machine identity so the runtime hook's resolve_host_name
    # matches the CODEX_SOURCES key even when gethostname differs from the chosen
    # name (the original bug: archiving silently failed on custom-named hosts).
    # Retire any legacy CLAUDE_CODE_HOST line in the same write.
    new_env_text = set_env_value(new_env_text, MACHINE_NAME_ENV_KEY, hostname)
    new_env_text = remove_env_key(new_env_text, CLAUDE_CODE_HOST_ENV_KEY)
    env_write_needed = new_env_text != env_text

    archive_dir_exists = archive_dir.exists()
    if archive_dir_exists:
        print(f"  {DIM}Archive dir already exists — skip mkdir{RESET}")
    else:
        print(f"  {GREEN}+{RESET} mkdir -p {archive_dir}")

    if stop_done and not env_write_needed and archive_dir_exists:
        print(f"\n{GREEN}✓ Already installed — nothing to do.{RESET}")
        print(f"  {DIM}(If Codex hasn't trusted the hook yet, run `/hooks` in Codex.){RESET}\n")
        sys.exit(0)

    if not args.yes:
        print(f"\n{YELLOW}This will modify {hooks_path} and {env_path}.{RESET}")
        print(f"{YELLOW}Timestamped backups will be made before writing (*.bak.TIMESTAMP).{RESET}")
        if not _prompt_yn("Proceed?", default=True):
            print("Aborted.")
            sys.exit(0)

    print(f"\n{BOLD}Applying...{RESET}")

    # 1. hooks.json
    if not stop_done:
        if hooks_path.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = hooks_path.with_suffix(f".json.bak.{ts}")
            backup.write_text(hooks_path.read_text())
            print(f"  {GREEN}✓{RESET} Backed up hooks.json → {backup.name}")
        add_stop_hook(hooks_config, command)
        hooks_path.write_text(json.dumps(hooks_config, indent=2) + "\n")
        print(f"  {GREEN}✓{RESET} Updated {hooks_path}")

    # 2. .env
    if env_write_needed:
        if env_path.exists() and env_path.stat().st_size > 0:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = env_path.with_suffix(f".bak.{ts}")
            backup.write_text(env_path.read_text())
            print(f"  {GREEN}✓{RESET} Backed up .env → {backup.name}")
        env_path.write_text(new_env_text, encoding="utf-8")
        print(f"  {GREEN}✓{RESET} Updated .env "
              f"({CODEX_SOURCES_ENV_KEY}: {env_action}, {MACHINE_NAME_ENV_KEY}: {host_action})")

    # 3. archive dir
    if not archive_dir_exists:
        archive_dir.mkdir(parents=True, exist_ok=True)
        print(f"  {GREEN}✓{RESET} Created {archive_dir}")

    # 4. Optional backfill of existing $CODEX_HOME/sessions/ history, using the
    #    same sweep code as the runtime hook so behavior matches exactly.
    from scrying_at_home.sync import codex_sync  # noqa: E402
    # Bind the sync module to the archive we just resolved (its module-level
    # CODEX_DIR is read at call time; the archive comes from .env via the sweep).
    if sessions_dir.exists():
        jsonls = list(sessions_dir.rglob("rollout-*.jsonl"))
        total_bytes = sum((j.stat().st_size if j.exists() else 0) for j in jsonls)
        if jsonls:
            print(
                f"\n{BOLD}Backfill existing Codex history?{RESET}\n"
                f"  Found {CYAN}{len(jsonls)}{RESET} rollout file(s) under "
                f"{CYAN}{sessions_dir}{RESET} ({CYAN}{_human_bytes(total_bytes)}{RESET})."
            )
            if args.yes or _prompt_yn("Run backfill now?", default=True):
                codex_sync.CODEX_DIR = codex_dir  # ensure validation root matches
                codex_sync.sync_directory(sessions_dir, archive_dir, "backfill")
                print(f"  {GREEN}✓{RESET} Backfill complete.")
        else:
            print(f"\n{DIM}No existing rollout transcripts to backfill.{RESET}")
    else:
        print(f"\n{DIM}{sessions_dir} does not exist yet — skipping backfill.{RESET}")

    # Done.
    search_cmd = resolve_invocation(
        Path("full_text_search_chats_archive.py"),
        explicit_alias=args.search_alias or None,
    )
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Setup complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"""
  How it works:
    When a Codex turn completes (Stop), the hook sweeps
    {CYAN}{sessions_dir}{RESET}/**/rollout-*.jsonl into:

      {CYAN}{archive_dir}{RESET}

    The Codex Stop payload carries no transcript path, so the hook reconciles
    the whole sessions tree — idempotent and cheap (it line-count compares the
    archive and writes only the new tail; unchanged files cost one stat()).

    {BOLD}Assumption:{RESET} Codex rollout transcripts are immutable append-only logs.
    If that ever changes, archives could diverge — the script writes to
    {CYAN}codex_anomalies.log{RESET} as a canary.

  {BOLD}⚠ Trust the hook (required — Codex won't run an untrusted hook):{RESET}
    1. Start Codex, then run {CYAN}/hooks{RESET} to review and trust the new Stop hook.
       (Codex shows a one-time "Hooks need review" prompt.)
    2. Headless automation can instead pass
       {CYAN}codex exec --dangerously-bypass-hook-trust{RESET}.

  {BOLD}Verify it's working:{RESET}
    1. Run a Codex turn (or rely on the backfill above), then:
    2. {CYAN}{search_cmd} -s codex <some query>{RESET}
       Results from this machine are tagged with host {CYAN}{hostname}{RESET}.

  {BOLD}To uninstall:{RESET}
    Delete the Stop entry pointing at {REPO_MARKER} in {hooks_path},
    and unset {CODEX_SOURCES_ENV_KEY} in .env.
""")


if __name__ == "__main__":
    main()
