"""
Default directory paths relative to the repository root.

All data lives under data/ so that the entire data/ folder can be
synced as a single unit (e.g. with MEGA, Syncthing, or similar).

  data/
    llm_data/           - organized chat archives (claude/, chatgpt/, etc.)
    archived_exports/   - processed export zip files
    local_views/        - generated Markdown/HTML conversation views

Any of these can be overridden via .env:
  DATA_DIR=/absolute/path/to/llm_data
  ARCHIVED_EXPORTS_DIR=/absolute/path/to/archived_exports
  LOCAL_VIEWS_DIR=/absolute/path/to/local_views

This module is the single source of truth for resolving path-related
environment variables. Entry points should call `resolve_data_dir`,
`resolve_archived_exports_dir`, and `resolve_local_views_dir` rather
than reading these keys from `config` directly.
"""
from __future__ import annotations

import re
import socket
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


def load_env_file(path: Path) -> dict:
    """Parse a simple .env file into a dict.

    - utf-8
    - blank lines and lines starting with '#' are ignored
    - inline trailing '#' comments are stripped only when preceded by whitespace
      (so a literal '#' inside an unquoted value still works as long as it isn't
      preceded by whitespace)
    - matching surrounding single or double quotes are stripped from the value
    """
    config: dict = {}
    if not path.exists():
        return config
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        m = re.search(r"\s+#", value)
        if m:
            value = value[: m.start()].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        config[key] = value
    return config


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
    """Return the llm_data directory, honoring DATA_DIR from .env."""
    return _resolve_dir(config, "DATA_DIR", script_dir, LLM_DATA_SUBDIR)


def resolve_archived_exports_dir(script_dir: Path, config: dict) -> Path:
    """Return the archived_exports directory, honoring ARCHIVED_EXPORTS_DIR from .env."""
    return _resolve_dir(config, "ARCHIVED_EXPORTS_DIR", script_dir, ARCHIVED_EXPORTS_SUBDIR)


def resolve_local_views_dir(script_dir: Path, config: dict) -> Path:
    """Return the local_views directory, honoring LOCAL_VIEWS_DIR from .env."""
    return _resolve_dir(config, "LOCAL_VIEWS_DIR", script_dir, LOCAL_VIEWS_SUBDIR)


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
