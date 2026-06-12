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
/clauding-at-home/index.db.

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

# Single sync root - everything lives under here
DATA_ROOT = Path("data")

# Subdirectories under DATA_ROOT
LLM_DATA_SUBDIR = DATA_ROOT / "llm_data"
ARCHIVED_EXPORTS_SUBDIR = DATA_ROOT / "archived_exports"
LOCAL_VIEWS_SUBDIR = DATA_ROOT / "local_views"

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

# Optional override for the human-readable name of this machine. If unset,
# we normalize socket.gethostname() (lowercased, .local stripped).
CLAUDE_CODE_HOST_ENV_KEY = "CLAUDE_CODE_HOST"


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

    Validation (erroring on an explicit-but-missing path) belongs in the
    imperative shell of each entry point, not here, so this stays pure.
    """
    if config_arg:
        return Path(config_arg).expanduser()
    return script_dir / ".env"


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


def resolve_search_index_path(config: dict) -> Path:
    """Return the search index db path, honoring SEARCH_INDEX_DB from .env."""
    raw = (config.get(SEARCH_INDEX_ENV_KEY) or "").strip()
    if raw:
        return Path(raw).expanduser()
    cache_root = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "clauding-at-home" / "index.db"


def parse_sources_string(raw: str) -> list[tuple[str, str]]:
    """Parse a 'host1=path1,host2=path2' string into [(host, path), ...].

    Returns raw strings without expanduser/Path conversion. Raises ValueError
    on malformed entries.
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
                f"{CLAUDE_CODE_SOURCES_ENV_KEY} entry {entry!r} is missing '=': "
                f"expected 'host=path'"
            )
        host, path = entry.split("=", 1)
        host = host.strip()
        path = path.strip()
        if not host or not path:
            raise ValueError(
                f"{CLAUDE_CODE_SOURCES_ENV_KEY} entry {entry!r} has empty host or path"
            )
        out.append((host, path))
    return out


def parse_claude_code_sources(config: dict) -> list[tuple[str, Path]]:
    """Return list of (host, expanded Path) tuples parsed from CLAUDE_CODE_SOURCES.

    Returns [] if the var is unset or empty.
    """
    raw = config.get(CLAUDE_CODE_SOURCES_ENV_KEY, "")
    return [(h, Path(p).expanduser()) for h, p in parse_sources_string(raw)]


def normalize_hostname(raw: str) -> str:
    """Lowercase and strip a trailing '.local' (macOS Bonjour suffix)."""
    name = raw.strip().lower()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name


def resolve_host_name(config: dict) -> str:
    """Return the human-readable host name for this machine.

    Reads CLAUDE_CODE_HOST from `config` if set; otherwise falls back to a
    normalized socket.gethostname(). The explicit override exists because
    macOS hostnames are unstable (network-dependent, can flip between
    'machine.local' and 'machine-2.local'), so a hand-picked name is more
    reliable for cross-machine search attribution.
    """
    explicit = config.get(CLAUDE_CODE_HOST_ENV_KEY, "").strip()
    if explicit:
        return explicit
    return normalize_hostname(socket.gethostname())


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
