"""
Default directory paths relative to the repository root.

All data lives under data/ so that the entire data/ folder can be
synced as a single unit (e.g. with MEGA, Syncthing, or similar).

  data/
    llm_data/           - organized chat archives (claude/, chatgpt/, etc.)
    archived_exports/   - processed export zip files
    local_views/        - generated Markdown/HTML conversation views

Any of these can be overridden via .env:
  LLM_DATA_DIR=/absolute/path/to/llm_data
  ARCHIVED_EXPORTS_DIR=/absolute/path/to/archived_exports
  LOCAL_VIEWS_DIR=/absolute/path/to/local_views
  SEARCH_INDEX_DB=/absolute/path/to/index.db

The search index is per-machine derived state (rebuildable from llm_data),
so unlike the directories above it deliberately lives OUTSIDE data/ — cloud
sync tools corrupt SQLite files. Default: $XDG_CACHE_HOME (or ~/.cache)
/scrying-at-home/index.db.

DATA_DIR was the former name of LLM_DATA_DIR. It is no longer supported: a
DATA_DIR entry in .env now raises so a stale override can't silently point
the tool at the wrong directory. Rename it to LLM_DATA_DIR.

This module is the single source of truth for resolving path-related
environment variables. Entry points should call `resolve_data_dir`,
`resolve_archived_exports_dir`, and `resolve_local_views_dir` rather
than reading these keys from `config` directly.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# The repository root, derived from this module's location
# (scrying_at_home/config/paths.py -> parents[2]). This is the stable anchor the
# root entry-point shims and the moved CLIs use for the default .env, the assets/
# stylesheet, migration paths, and anomaly logs — replacing the old
# Path(__file__).parent, which used to be the repo root only because the scripts
# lived there. The seven entry shims stay at the repo root, so this holds.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Single sync root - everything lives under here
DATA_ROOT = Path("data")

# Subdirectories under DATA_ROOT
LLM_DATA_SUBDIR = DATA_ROOT / "llm_data"
ARCHIVED_EXPORTS_SUBDIR = DATA_ROOT / "archived_exports"
LOCAL_VIEWS_SUBDIR = DATA_ROOT / "local_views"

# The web-export tree stores each provider's items under
# data_dir/<provider>/<email>/<subdir>, mapping subdir -> item_type. One
# definition, walked by the search scan, the indexer (scan_disk), and the
# viewer's uuid lookup; the writer (sync) mirrors the same two subdir names.
WEB_EXPORT_SUBDIRS = (("conversations", "conversation"), ("projects", "project"))

# External data sources for Claude Code conversations (JSONL archives).
# Configured via CLAUDE_CODE_SOURCES in .env as comma-separated host=path
# pairs, e.g. "laptop=/Users/me/cah/data/llm_data/claude-code/laptop,
# desktop=/Users/me/cah/data/llm_data/claude-code/desktop".
#
# Each path is the archive root for one machine. They live on local disk
# (typically under this repo's data/ tree); cloud-syncing the whole repo
# with MEGA / Dropbox / Syncthing is fine and is how multi-machine search
# works — every host writes to its *own* subdirectory, so the file-level
# locking the hook does only ever needs to coordinate writers on one host.
# The host label is stamped onto search results so the resume command can
# be attributed to the originating machine.
CLAUDE_CODE_SOURCES_ENV_KEY = "CLAUDE_CODE_SOURCES"

# External data sources for OpenAI Codex conversations (rollout JSONL archives).
# Configured via CODEX_SOURCES in .env in the same comma-separated host=path
# form as CLAUDE_CODE_SOURCES, e.g.
# "laptop=/Users/me/cah/data/llm_data/codex/laptop". Each path is one machine's
# Codex archive root, mirroring Codex's own sessions/YYYY/MM/DD/ date tree.
CODEX_SOURCES_ENV_KEY = "CODEX_SOURCES"

# Optional override for the human-readable name of this machine. If unset, we
# normalize socket.gethostname() (lowercased, .local stripped). The host
# identity is per-machine and shared by every provider — it tags which archive
# subdir a machine writes to (claude-code/<host>/, codex/<host>/) and attributes
# search results and --here to the originating box — so the key is deliberately
# provider-neutral: MACHINE_NAME.
MACHINE_NAME_ENV_KEY = "MACHINE_NAME"

# Legacy name for MACHINE_NAME. Before Codex support the key was Claude-Code
# specific (CLAUDE_CODE_HOST); once it came to govern every provider the name no
# longer fit, so it was renamed. resolve_host_name still reads this as a
# fallback, and migrations 002/004 rewrite an existing CLAUDE_CODE_HOST line to
# MACHINE_NAME in place, so pre-rename .env files keep working untouched.
CLAUDE_CODE_HOST_ENV_KEY = "CLAUDE_CODE_HOST"

# Read precedence for the machine name: canonical key first, then legacy alias.
_HOST_NAME_ENV_KEYS = (MACHINE_NAME_ENV_KEY, CLAUDE_CODE_HOST_ENV_KEY)


def parse_env_assignment(line: str) -> tuple[str, str, bool] | None:
    """Parse one raw .env line as a KEY=VALUE assignment, active or commented.

    Returns (key, value, is_commented), or None when the line is not an
    assignment (blank, or no '='). Commented forms — `#KEY=value` and
    `# KEY=value` — are recognized so migrations can find and activate
    .env.example documentation lines in place. Prose comments that happen to
    contain '=' parse to a key nobody looks up, so they are harmless.

    This is the single source of truth for .env line semantics; anything that
    reads or rewrites .env lines (load_env_file, the migrations' upserts) must
    go through it so they can't disagree on what a line means:
    - whitespace around '=' is tolerated
    - inline trailing '#' comments are stripped only when preceded by
      whitespace (so a literal '#' inside an unquoted value still works as
      long as it isn't preceded by whitespace)
    - matching surrounding single or double quotes are stripped from the value
    """
    stripped = line.strip()
    if not stripped:
        return None
    is_commented = stripped.startswith("#")
    body = stripped.lstrip("#").strip() if is_commented else stripped
    if "=" not in body:
        return None
    key, value = body.split("=", 1)
    key = key.strip()
    value = value.strip()
    m = re.search(r"\s+#", value)
    if m:
        value = value[: m.start()].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value, is_commented


def load_env_file(path: Path) -> dict:
    """Parse a simple .env file (utf-8) into a dict.

    Active assignments only; line semantics per parse_env_assignment. When a
    key is assigned more than once, the last assignment wins.
    """
    config: dict = {}
    if not path.exists():
        return config
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_assignment(raw)
        if parsed is None:
            continue
        key, value, is_commented = parsed
        if is_commented:
            continue
        config[key] = value
    return config


def resolve_env_path(script_dir: Path, config_arg: str | None) -> Path:
    """Resolve which .env file to load.

    With an explicit --config value, use it (expanduser, relative to cwd).
    Otherwise fall back to the script-local default, unchanged behavior.

    Validation (erroring on an explicit-but-missing path) lives in
    load_env_or_exit, the shared imperative shell, not here, so this stays pure.
    """
    if config_arg:
        return Path(config_arg).expanduser()
    return script_dir / ".env"


def add_config_arg(parser) -> None:
    """Add the shared ``--config PATH`` flag (the .env override) to a parser."""
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="Path to the .env config file (default: alongside this script)",
    )


def load_env_or_exit(script_dir: Path, config_arg: str | None) -> dict:
    """Resolve the .env path (honoring --config), error-and-exit when an explicit
    --config file is missing, and return the loaded config dict — the shared
    imperative shell behind every entry point's config load."""
    env_path = resolve_env_path(script_dir, config_arg)
    if config_arg and not env_path.is_file():
        print(f"Error: --config file not found: {env_path}", file=sys.stderr)
        sys.exit(1)
    return load_env_file(env_path)


def active_env_values(text: str, key: str) -> list[str]:
    """Return the value of every *active* line assigning `key`, in file order.

    Unlike load_env_file (last assignment wins), this exposes all assignments
    so callers that merge multi-valued keys (CLAUDE_CODE_SOURCES) can see
    entries that a stale duplicate line would otherwise shadow.
    """
    out: list[str] = []
    for line in text.splitlines():
        parsed = parse_env_assignment(line)
        if parsed is not None and parsed[0] == key and not parsed[2]:
            out.append(parsed[1])
    return out


def set_env_value(text: str, key: str, value: str) -> str:
    """Pure rewrite of .env text setting `key` to `value`.

    Collapses every line assigning `key` — active or commented-out
    documentation, however many — into a single active `key=value` line at
    the first such line's position (preserving its indentation), dropping the
    rest. Appends at the end when no line assigns the key. The value is
    quoted if it would otherwise be truncated by inline-comment stripping on
    the next read.
    """
    if re.search(r"\s+#", value):
        value = f'"{value}"'
    new_lines: list[str] = []
    anchor: int | None = None
    leading = ""
    for line in text.splitlines():
        parsed = parse_env_assignment(line)
        if parsed is None or parsed[0] != key:
            new_lines.append(line)
            continue
        if anchor is None:
            anchor = len(new_lines)
            leading = line[: len(line) - len(line.lstrip())]
            new_lines.append("")  # reserved; filled below
        # any further line assigning the key is a duplicate — drop it
    if anchor is None:
        new_lines.append(f"{key}={value}")
    else:
        new_lines[anchor] = f"{leading}{key}={value}"
    return "\n".join(new_lines) + "\n"


def remove_env_key(text: str, key: str) -> str:
    """Pure: drop every *active* line assigning `key`, leaving the rest intact.

    Commented-out lines (e.g. .env.example documentation) are preserved; only
    active `key=...` assignments are removed. Used by the migrations to retire a
    renamed key after writing its replacement, so a stale active assignment
    can't shadow or contradict the new one. Returns "" if nothing is left.
    """
    kept: list[str] = []
    for line in text.splitlines():
        parsed = parse_env_assignment(line)
        if parsed is not None and parsed[0] == key and not parsed[2]:
            continue
        kept.append(line)
    return "\n".join(kept) + "\n" if kept else ""


def _resolve_dir(
    config: dict, env_key: str, script_dir: Path, default_subdir: Path
) -> Path:
    """Resolve a path-valued env var from `config`, falling back to a default
    relative to `script_dir`."""
    raw = config.get(env_key, "").strip() if config.get(env_key) else ""
    if raw:
        return Path(raw).expanduser()
    return script_dir / default_subdir


def resolve_data_dir(script_dir: Path, config: dict) -> Path:
    """Return the llm_data directory, honoring LLM_DATA_DIR from .env.

    DATA_DIR was the former name of this key. It is no longer supported: if
    it is set in .env, we raise rather than silently ignore it, so a stale
    override can't point the tool at the wrong directory.
    """
    if (config.get("DATA_DIR") or "").strip():
        raise SystemExit(
            "DATA_DIR in .env is no longer supported; rename it to LLM_DATA_DIR. "
            "See .env.example for the current configuration keys."
        )
    return _resolve_dir(config, "LLM_DATA_DIR", script_dir, LLM_DATA_SUBDIR)


def resolve_archived_exports_dir(script_dir: Path, config: dict) -> Path:
    """Return the archived_exports directory, honoring ARCHIVED_EXPORTS_DIR from .env."""
    return _resolve_dir(config, "ARCHIVED_EXPORTS_DIR", script_dir, ARCHIVED_EXPORTS_SUBDIR)


def resolve_local_views_dir(script_dir: Path, config: dict) -> Path:
    """Return the local_views directory, honoring LOCAL_VIEWS_DIR from .env."""
    return _resolve_dir(config, "LOCAL_VIEWS_DIR", script_dir, LOCAL_VIEWS_SUBDIR)


# Optional override for the search index database file. The index is derived,
# per-machine state, so its default lives in the XDG cache dir rather than the
# cloud-synced data/ tree (SQLite databases corrupt under file-level sync).
SEARCH_INDEX_ENV_KEY = "SEARCH_INDEX_DB"

# Per-machine cache dir name (under $XDG_CACHE_HOME or ~/.cache). Was
# "clauding-at-home" before the project was renamed; migrate_legacy_index_cache
# does a one-time move of an existing index from the old location.
CACHE_DIR_NAME = "scrying-at-home"
LEGACY_CACHE_DIR_NAME = "clauding-at-home"


def _cache_base() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME", "").strip()
    return Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"


def resolve_search_index_path(config: dict) -> Path:
    """Return the search index db path, honoring SEARCH_INDEX_DB from .env."""
    raw = (config.get(SEARCH_INDEX_ENV_KEY) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _cache_base() / CACHE_DIR_NAME / "index.db"


def legacy_search_index_path(config: dict) -> Path | None:
    """The pre-rename default index path, or None if a custom path is set.

    Used only to migrate an existing index out of the old cache dir; returns
    None when SEARCH_INDEX_DB is configured, since then there is nothing of
    ours to migrate.
    """
    raw = (config.get(SEARCH_INDEX_ENV_KEY) or "").strip()
    if raw:
        return None
    return _cache_base() / LEGACY_CACHE_DIR_NAME / "index.db"


def migrate_legacy_index_cache(config: dict) -> None:
    """One-time move of the index cache from the pre-rename location.

    The project was renamed clauding-at-home -> scrying-at-home; the
    per-machine search index (often a few hundred MB) lived under
    ~/.cache/clauding-at-home. Move it to the new cache dir so existing users
    neither reindex nor leave the old cache orphaned on disk. No-op once
    migrated, if a custom SEARCH_INDEX_DB is set, or if the new dir already
    exists.
    """
    old_path = legacy_search_index_path(config)
    if old_path is None:
        return
    old_dir = old_path.parent
    new_dir = resolve_search_index_path(config).parent
    if new_dir == old_dir or not old_dir.is_dir() or new_dir.exists():
        return
    print(
        f"Project renamed to scrying-at-home: moving search index cache "
        f"{old_dir} -> {new_dir}"
    )
    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))


def parse_sources_string(
    raw: str, env_key: str = CLAUDE_CODE_SOURCES_ENV_KEY
) -> list[tuple[str, str]]:
    """Parse a 'host1=path1,host2=path2' string into [(host, path), ...].

    Returns raw strings without expanduser/Path conversion. Raises ValueError
    on malformed entries; `env_key` names the offending var in the message
    (CLAUDE_CODE_SOURCES or CODEX_SOURCES — both share this format).
    """
    raw = raw.strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"{env_key} entry {entry!r} is missing '=': expected 'host=path'"
            )
        host, path = entry.split("=", 1)
        host = host.strip()
        path = path.strip()
        if not host or not path:
            raise ValueError(
                f"{env_key} entry {entry!r} has empty host or path"
            )
        out.append((host, path))
    return out


def parse_claude_code_sources(config: dict) -> list[tuple[str, Path]]:
    """Return list of (host, expanded Path) tuples parsed from CLAUDE_CODE_SOURCES.

    Returns [] if the var is unset or empty.
    """
    raw = config.get(CLAUDE_CODE_SOURCES_ENV_KEY, "")
    return [(h, Path(p).expanduser())
            for h, p in parse_sources_string(raw, CLAUDE_CODE_SOURCES_ENV_KEY)]


def parse_codex_sources(config: dict) -> list[tuple[str, Path]]:
    """Return list of (host, expanded Path) tuples parsed from CODEX_SOURCES.

    Same host=path format and semantics as parse_claude_code_sources; returns
    [] if the var is unset or empty.
    """
    raw = config.get(CODEX_SOURCES_ENV_KEY, "")
    return [(h, Path(p).expanduser())
            for h, p in parse_sources_string(raw, CODEX_SOURCES_ENV_KEY)]


def normalize_hostname(raw: str) -> str:
    """Lowercase and strip a trailing '.local' (macOS Bonjour suffix)."""
    name = raw.strip().lower()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name


def explicit_host_name(config: dict) -> str:
    """The user-configured machine name from `config`, or '' if none is set.

    Reads MACHINE_NAME, falling back to the legacy CLAUDE_CODE_HOST key. Unlike
    resolve_host_name it does not consult the OS hostname — callers that want
    that default use resolve_host_name; callers that need to know whether the
    user set a name at all (vs. inheriting gethostname) use this.
    """
    for key in _HOST_NAME_ENV_KEYS:
        value = (config.get(key) or "").strip()
        if value:
            return value
    return ""


def resolve_host_name(config: dict) -> str:
    """Return the human-readable host name for this machine.

    Uses an explicit MACHINE_NAME (or the legacy CLAUDE_CODE_HOST) from `config`
    if set; otherwise falls back to a normalized socket.gethostname(). The
    explicit override exists because macOS hostnames are unstable
    (network-dependent, can flip between 'machine.local' and 'machine-2.local'),
    so a hand-picked name is more reliable for cross-machine search attribution.
    """
    return explicit_host_name(config) or normalize_hostname(socket.gethostname())


def codex_home() -> Path:
    """The Codex home directory, honoring $CODEX_HOME (default ~/.codex).

    Shared by codex_sync (the archival hook) and migration 004, so the
    'CODEX_HOME' env key and the '~/.codex' default live in exactly one place.
    """
    raw = os.environ.get("CODEX_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def resolve_provider_archive_dir(
    config: dict,
    sources: list[tuple[str, Path]],
    *,
    env_key: str,
    env_file: Path,
    setup_command: str,
) -> Path:
    """Select this machine's archive directory for a local-CLI provider.

    Shared by the Claude Code and Codex sync adapters, which differ only in their
    env key, the parsed `sources`, and the migration named in the setup hint.
    Resolves the host via resolve_host_name(config) and returns the matching
    entry's Path, raising RuntimeError (with the setup hint) when `sources` is
    unset or carries no entry for this host.
    """
    if not sources:
        raise RuntimeError(
            f"{env_key} is not set in {env_file}. Run `{setup_command}` to configure."
        )
    host = resolve_host_name(config)
    for entry_host, path in sources:
        if entry_host == host:
            return path
    raise RuntimeError(
        f"No entry for host {host!r} in {env_key}. Run `{setup_command}` on this machine."
    )


def _default_open_command(path: Path) -> list[str] | None:
    """The OS 'open with default app' command for `path`, or None if unknown."""
    if sys.platform == "darwin":
        return ["open", str(path)]
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "", str(path)]
    if sys.platform.startswith("linux"):
        return ["xdg-open", str(path)]
    return None


def open_in_editor(*paths: Path) -> None:
    """Open one or more files for the user, preferring $VISUAL/$EDITOR.

    Falls back through three tiers so it works whether or not the user has
    configured an editor:
      1. $VISUAL or $EDITOR, if set (one invocation, all files; blocks until exit).
      2. The OS 'open with default app' command, once per file (returns at once).
      3. No opener available -> print the saved path(s) and exit non-zero.

    The files are assumed to already exist on disk; this only opens them.
    """
    targets = [str(p) for p in paths]
    if not targets:
        return

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        print(f"Opening {len(targets)} file(s) in {editor}...")
        try:
            subprocess.run([editor, *targets])
            return
        except FileNotFoundError:
            print(
                f"$EDITOR '{editor}' not found; trying the system default app...",
                file=sys.stderr,
            )

    command = _default_open_command(paths[0])
    if command and shutil.which(command[0]):
        print(f"Opening {len(targets)} file(s) with the default app...")
        try:
            for target in targets:
                subprocess.run([*command[:-1], target])
            return
        except OSError as e:
            print(f"Could not open the default app: {e}", file=sys.stderr)

    saved = "\n".join(f"  {t}" for t in targets)
    print(
        "No editor available. Set $EDITOR to your preferred editor.\n"
        f"File(s) saved at:\n{saved}",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Shell alias resolution
#
# Both setup.py and migration 002 print "here's how to run the search" hints.
# We want those hints to show the user's own alias (e.g. `cs "query"`) when one
# exists, instead of the verbose `python3 /abs/path/...py` form. The alias may
# be one setup.py just created (passed in explicitly), or one already living in
# the current shell's rc that we can detect by scanning.
# --------------------------------------------------------------------------

# Candidate dotfiles, in preference order, paired with the shell whose rc they
# are (empty tag = shell-agnostic, e.g. ~/.profile). setup.py reuses this list
# for its alias-writing flow; here it drives current-shell rc detection.
CANDIDATE_DOTFILES = (
    ("~/.bashrc", "bash"),
    ("~/.bash_profile", "bash"),
    ("~/.zshrc", "zsh"),
    ("~/.profile", ""),
    ("~/.config/fish/config.fish", "fish"),
)


def alias_in_dotfiles(
    dotfile_texts: list[str], script_basename: str, flag: str = ""
) -> str | None:
    """Name of the first alias whose body invokes `script_basename` (+ `flag`).

    Pure. Scans `alias NAME=...` lines for ones whose definition mentions the
    script's basename (a substring of the absolute path the alias bakes in) and,
    when given, the distinguishing `flag` (e.g. `--claude` vs `--chatgpt`).
    Returns None if no alias matches.
    """
    needles = [script_basename] + ([flag] if flag else [])
    for text in dotfile_texts:
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped.startswith("alias "):
                continue
            name, sep, value = stripped[len("alias "):].partition("=")
            if not sep:
                continue
            if all(n in value for n in needles):
                return name.strip()
    return None


def format_invocation(
    script_path: Path, flag: str = "", alias: str | None = None
) -> str:
    """Display string for invoking `script_path`: the alias, or `python3 ...`.

    Pure. When `alias` is given it is returned as-is — the alias already bakes
    in any `flag`. Otherwise falls back to `python3 <path>[ flag]`. Never
    appends a trailing user argument (e.g. a search query); the caller adds that
    after this base command in whatever form it needs.
    """
    if alias:
        return alias
    return f"python3 {script_path} {flag}".rstrip()


def current_shell_rc_texts(env: dict | None = None) -> list[str]:
    """UTF-8 texts of the current shell's rc file(s), for alias detection.

    Impure (reads $SHELL and the filesystem). Keeps candidate dotfiles whose
    shell tag matches $SHELL's basename, plus shell-agnostic ones; when the
    shell is unknown, reads them all. Missing / unreadable / non-UTF-8 files are
    silently skipped — this only powers display sugar, never a correctness path.
    """
    env = os.environ if env is None else env
    shell = Path((env.get("SHELL") or "").strip()).name
    texts = []
    for raw, tag in CANDIDATE_DOTFILES:
        if shell and tag and tag != shell:
            continue
        try:
            texts.append(Path(raw).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def resolve_invocation(
    script_path: Path,
    flag: str = "",
    *,
    explicit_alias: str | None = None,
    dotfile_texts: list[str] | None = None,
) -> str:
    """Best display command for `script_path`, preferring an alias.

    Resolution order:
      1. `explicit_alias` — an alias the caller knows about (e.g. one setup.py
         just wrote). Trusted as-is.
      2. an alias already defined in `dotfile_texts` (defaults to the current
         shell's rc files) whose body invokes this script + `flag`.
      3. `python3 <script_path>[ flag]` — the always-correct fallback.
    """
    if dotfile_texts is None:
        dotfile_texts = current_shell_rc_texts()
    alias = explicit_alias or alias_in_dotfiles(
        dotfile_texts, script_path.name, flag
    )
    return format_invocation(script_path, flag, alias)
