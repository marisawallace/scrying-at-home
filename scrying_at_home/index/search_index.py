"""
SQLite FTS5 candidate-filter index for the chat archive.

The index never answers a query by itself: it only narrows the set of files
the existing extract/match/score pipeline has to parse, so search results
stay byte-identical to a full scan. It additionally carries an items
metadata table so browse mode, --stats, and uuid lookup can skip parsing.

Correctness invariants:
  - FTS rows are a SUPERSET of scan-path matches (false positives are
    filtered when candidates are re-scored by the real pipeline; false
    negatives are never acceptable).
  - The FTS body is Python-lowercased and the trigram tokenizer runs
    case_sensitive, because SQLite's own case folding differs from
    str.lower() on edge cases and could drop matches.
  - The index is derived, per-machine state: it self-bootstraps, rebuilds
    itself when corrupt, stale-schemed, or built by different extractor
    source (see schema_identity), and every entry point falls back to the
    full scan when the index is unavailable.

This module must not import full_text_search_chats_archive (it runs as
__main__ and would be loaded twice); the llm text extractors are passed in
as a callable instead. claude_code_parser is a leaf module and is imported
directly.
"""
from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from scrying_at_home.parsers import claude_code as ccp
from scrying_at_home.parsers import codex as cxp
from scrying_at_home.common.ansi import muted, warning
from scrying_at_home.parsers.transcript_jsonl import parse_jsonl_lines

from scrying_at_home.common.timestamps import derive_updated_at
from scrying_at_home.common.constants import UNTITLED
from scrying_at_home.config.paths import WEB_EXPORT_SUBDIRS
from scrying_at_home import providers

SCHEMA_VERSION = 6
RECONCILE_BATCH = 200

# Below this many files a refresh is fast enough that progress output is just
# noise; we stay silent (matches the old "Indexing ..." gate).
PROGRESS_MIN_FILES = 50
PROGRESS_BAR_WIDTH = 30

# Modules whose source determines what ends up in the index: the extractors,
# the metadata derivation, and this module itself. Their bytes are hashed
# into the schema identity (see schema_identity) so that ANY change to
# extraction logic automatically invalidates the index — without this, an
# extractor edit would leave stale FTS bodies that silently drop matches.
SCHEMA_SOURCE_FILES = (
    # Repo-root-relative paths; schema_identity resolves them from the repo root.
    "scrying_at_home/index/search_index.py",
    "scrying_at_home/parsers/claude_code.py",
    "scrying_at_home/parsers/codex.py",
    "scrying_at_home/parsers/transcript_jsonl.py",  # shared tokenizer / model tie-break
    "scrying_at_home/search/engine.py",  # scan-path extractors / scoring
    "scrying_at_home/search/result.py",  # SearchResult assembly + name-bonus/recency scoring
    # scrying_at_home.common leaves whose output is STORED in the index, so an edit must
    # invalidate it too: timestamps.derive_updated_at -> items.updated_at,
    # text.truncate_name -> items.name. (Over-invalidates on sibling edits like
    # parse_iso/normalize_uuid — the safe direction.)
    "scrying_at_home/common/timestamps.py",
    "scrying_at_home/common/text.py",
    "scrying_at_home/common/constants.py",  # UNTITLED -> items.name substitution
)


@functools.lru_cache(maxsize=None)
def schema_identity(src_dir: Optional[Path] = None) -> int:
    """The PRAGMA user_version this index must carry: SCHEMA_VERSION mixed
    with a hash of the source files listed in SCHEMA_SOURCE_FILES.

    Deliberately blunt — a comment-only edit to any of those modules
    triggers a full rebuild — because a rebuild costs seconds while a
    missed extractor change silently breaks the FTS-superset invariant.
    """
    h = hashlib.sha1(str(SCHEMA_VERSION).encode())
    # Resolve repo-root-relative SCHEMA_SOURCE_FILES from the repo root
    # (search_index.py now lives at scrying_at_home/index/, two levels down).
    base = Path(__file__).resolve().parents[2] if src_dir is None else src_dir
    for name in SCHEMA_SOURCE_FILES:
        h.update(name.encode())
        try:
            h.update((base / name).read_bytes())
        except OSError:
            pass  # missing module file: SCHEMA_VERSION alone still applies
    # Positive 31-bit int (user_version is a signed 32-bit field); never 0,
    # which is indistinguishable from a fresh empty database.
    return (int.from_bytes(h.digest()[:4], "big") & 0x7FFFFFFF) or 1

# files.source values
SOURCE_LLM = "llm"
SOURCE_CC = "claude-code"
# JSONL transcripts (e.g. subagents/*.jsonl) that the search walk never
# visits but gather_cc_tool_counts()'s rglob does: indexed for the --stats
# tool leaderboard only — no fts/items rows, so they can never match a
# search, exactly like the scan path.
SOURCE_CC_TOOLS = "claude-code-tools-only"
# OpenAI Codex rollout transcripts (rollout-*.jsonl). Unlike Claude Code there
# is no tools-only variant: one rollout file per session holds both the
# searchable text and the tool calls, so every Codex file is searchable.
SOURCE_CODEX = "codex"

SCHEMA_SQL = """
CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    source        TEXT NOT NULL,            -- 'llm' | 'claude-code'
    mtime_ns      INTEGER NOT NULL,
    ctime_ns      INTEGER NOT NULL,         -- inode change time; catches
                                            -- mtime-preserving copies (cp -p,
                                            -- rsync --times) whose write still
                                            -- stamps ctime
    size          INTEGER NOT NULL,
    indexed_bytes INTEGER NOT NULL          -- jsonl: end of last complete line;
                                            -- a torn trailing line (no newline)
                                            -- leaves size > indexed_bytes, so
                                            -- the file re-indexes until it lands
);

CREATE TABLE items (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    uuid        TEXT NOT NULL,
    item_type   TEXT NOT NULL,              -- 'conversation' | 'project'
    provider    TEXT NOT NULL,              -- 'claude' | 'chatgpt' | 'claude-code' | 'codex'
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    email       TEXT NOT NULL,              -- account email; project slug for claude-code
    name_raw    TEXT NOT NULL DEFAULT '',   -- raw extracted name (pre-(untitled)
                                            -- substitution); the search name bonus
                                            -- replicates the scan path, which keys
                                            -- the bonus off the raw, possibly-empty
                                            -- name. '' here means "no name", which
                                            -- must stay distinct from a conversation
                                            -- literally titled "(untitled)".
    model       TEXT NOT NULL DEFAULT '',   -- raw provider model id; '' if unknown
    host        TEXT NOT NULL DEFAULT '',
    cwd         TEXT NOT NULL DEFAULT '',
    git_branch  TEXT NOT NULL DEFAULT '',
    preview     TEXT NOT NULL DEFAULT '',   -- browse-mode snippet
    has_preview INTEGER NOT NULL DEFAULT 0  -- 0: no extractable text, hidden from browse
);
CREATE INDEX idx_items_uuid ON items(uuid);

CREATE VIRTUAL TABLE fts USING fts5(
    body,
    tokenize = "trigram case_sensitive 1",
    content = '',
    contentless_delete = 1
);

CREATE TABLE fts_map (
    fts_rowid INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_fts_map_file ON fts_map(file_id);

CREATE TABLE cc_tool_counts (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tool    TEXT NOT NULL,
    count   INTEGER NOT NULL,
    PRIMARY KEY (file_id, tool)
);

CREATE TABLE file_texts (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    texts   TEXT NOT NULL   -- JSON array of original-case extracted texts, in
                            -- extraction order; boundaries are preserved
                            -- (matching is per-text, and texts may themselves
                            -- contain newlines). Present for every searchable
                            -- file, including ones with zero texts ([]): a
                            -- missing row means "fall back to the real file",
                            -- which must stay distinct from "no texts".
);
"""


# One definition of the files / items column sets, so the DDL above, the INSERTs,
# and the SELECT projections below cannot drift in name, order, or count. The DDL
# stays the canonical human-readable declaration (it carries the per-column
# types/constraints); the INSERT/SELECT strings derive from these tuples.
FILES_COLUMNS = ("id", "path", "source", "mtime_ns", "ctime_ns", "size", "indexed_bytes")
ITEMS_COLUMNS = (
    "file_id", "uuid", "item_type", "provider", "name", "name_raw",
    "created_at", "updated_at", "email", "model", "host", "cwd", "git_branch",
    "preview", "has_preview",
)
# files.id is INTEGER PRIMARY KEY (autoincrement): selected, never inserted.
# Placeholder counts are len-derived, so they can't fall out of sync with columns.
_FILES_INSERT_COLS = FILES_COLUMNS[1:]
_FILES_INSERT_SQL = (
    f"INSERT INTO files({', '.join(_FILES_INSERT_COLS)}) "
    f"VALUES ({', '.join(['?'] * len(_FILES_INSERT_COLS))})"
)
_FILES_SELECT_SQL = ", ".join(FILES_COLUMNS)
_ITEMS_INSERT_SQL = (
    f"INSERT INTO items({', '.join(ITEMS_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(ITEMS_COLUMNS))})"
)


@dataclass(frozen=True)
class FileStat:
    """One archive file as seen on disk, with the walk-derived context
    needed to index it."""
    path: str
    source: str          # SOURCE_LLM | SOURCE_CC | SOURCE_CC_TOOLS | SOURCE_CODEX
    mtime_ns: int
    ctime_ns: int
    size: int
    provider: str = ""   # llm: 'claude' | 'chatgpt'
    email: str = ""      # llm: account email; cc: project slug; codex: ""
    item_type: str = ""  # llm: 'conversation' | 'project'
    host: str = ""       # cc / codex only


@dataclass(frozen=True)
class IndexedFile:
    """One archive file as recorded in the files table."""
    id: int
    path: str
    source: str
    mtime_ns: int
    ctime_ns: int
    size: int
    indexed_bytes: int


@dataclass(frozen=True)
class ReconcilePlan:
    new: tuple
    rewritten: tuple        # (FileStat, IndexedFile)
    removed: tuple          # IndexedFile

    def is_noop(self) -> bool:
        return not (self.new or self.rewritten or self.removed)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def build_fts_query(query: str, exact: bool) -> Optional[str]:
    """Translate a user query into an FTS5 MATCH string, or None when the
    trigram index cannot serve it (terms under 3 chars produce no trigrams).

    Multi-word queries use only the words >= 3 chars: dropping shorter words
    widens the candidate set (superset stays correct) and the rescoring pass
    applies the full AND semantics.
    """
    q = query.lower()
    if not q.strip():
        return None
    if exact:
        if len(q) < 3:
            return None
        return '"' + q.replace('"', '""') + '"'
    words = [w for w in q.split() if len(w) >= 3]
    if not words:
        return None
    return " AND ".join('"' + w.replace('"', '""') + '"' for w in words)


def searchable_body(texts: list[str]) -> str:
    """The FTS body for one file: every extracted text, Python-lowercased.

    Joined with newline so each text stays a contiguous substring of the
    body — that is what makes FTS matches a superset of per-text matches.
    """
    return "\n".join(t for t in texts if t).lower()


def preview_from_texts(texts: list[str]) -> tuple[str, bool]:
    """Browse-mode snippet, replicating find_matches_in_texts() for an empty
    query: first non-empty text, newlines collapsed, truncated to 200 chars.

    The bool is False when no text exists at all — such items are excluded
    from browse results, exactly as the scan path excludes them.
    """
    for text in texts:
        if not text:
            continue
        preview = text.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        return preview, True
    return "", False


def make_llm_item_meta(data: dict, item_type: str, texts: list[str]) -> dict:
    """Item metadata for a claude/chatgpt JSON file, replicating exactly how
    search_item() derives name, updated_at, and the browse preview."""
    # `or ""` (not a plain default): an export with "name": null yields None,
    # which would land in the NOT NULL name_raw column and raise IntegrityError
    # — misread by refresh() as write contention, silently disabling the index
    # every run. The scan path coerces the same null via `name.lower() if name`.
    name = data.get("name") or ""
    updated_at = derive_updated_at(data, item_type)
    preview, has_preview = preview_from_texts(texts)
    return {
        "uuid": data["uuid"],
        "item_type": item_type,
        "name": name if name else UNTITLED,
        "name_raw": name,  # raw, possibly empty: drives the name bonus
        "created_at": data["created_at"],
        "updated_at": updated_at,
        "model": "",  # filled by the caller, which has the provider
        "host": "",
        "cwd": "",
        "git_branch": "",
        "preview": preview,
        "has_preview": has_preview,
    }


def make_local_cli_item_meta(metadata: dict, texts: list[str]) -> dict:
    """Item metadata for a local-CLI transcript (Claude Code or Codex) from its
    parsed extract_session_metadata() dict.

    Both providers share one row shape: uuid is the session_id, name == name_raw
    (local-CLI names are always non-empty, derived from the first prompt), host
    is filled from the FileStat by the caller, and git_branch comes straight from
    the parser (always "" for Codex, which records no git info)."""
    preview, has_preview = preview_from_texts(texts)
    return {
        "uuid": metadata["session_id"],
        "item_type": "conversation",
        "name": metadata["name"],
        "name_raw": metadata["name"],  # local-CLI names are always non-empty: name == raw
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
        "model": metadata.get("model", ""),
        "host": "",  # filled from FileStat by the caller
        "cwd": metadata["cwd"],
        "git_branch": metadata["git_branch"],
        "preview": preview,
        "has_preview": has_preview,
    }


class TranscriptParser(Protocol):
    """The interface a local-CLI transcript parser exposes (claude_code_parser
    today; codex_parser next). A plain module satisfies it by defining these as
    top-level functions. The indexer below uses the first three; the viewer and
    search CLI also call extract_conversation_turns / find_session_file."""
    extract_searchable_text: Callable[[list], list]
    extract_session_metadata: Callable[[list], dict]
    count_tool_uses: Callable[[list], "Counter"]
    extract_conversation_turns: Callable[[list], list]
    find_session_file: Callable[..., object]


@dataclass(frozen=True)
class _TranscriptIndexer:
    """How to index one family of append-only JSONL transcripts (Claude Code,
    Codex). The same parser/meta handle a source's searchable files and, where a
    tool-counts-only variant exists, its non-searchable files; only files whose
    source == `searchable` get fts/items rows."""
    provider: str                              # items.provider value
    parser: TranscriptParser                   # module of extractor functions
    meta_builder: Callable[[dict, list], dict]  # make_*_item_meta
    searchable: str                            # the source that gets fts/items


# Source -> how to index it. Claude Code's searchable transcripts (SOURCE_CC)
# and tool-counts-only files (SOURCE_CC_TOOLS) share one indexer; a tools-only
# file just skips the fts/items rows (its fs.source != indexer.searchable).
# Codex has no tools-only variant — its single rollout per session is always
# searchable, so SOURCE_CODEX maps straight to its own indexer.
_CC_INDEXER = _TranscriptIndexer("claude-code", ccp, make_local_cli_item_meta, SOURCE_CC)
_CODEX_INDEXER = _TranscriptIndexer("codex", cxp, make_local_cli_item_meta, SOURCE_CODEX)
_TRANSCRIPT_INDEXERS: dict = {
    SOURCE_CC: _CC_INDEXER,
    SOURCE_CC_TOOLS: _CC_INDEXER,
    SOURCE_CODEX: _CODEX_INDEXER,
}
# Sources that follow the append-only transcript re-index rule in diff_index.
_TRANSCRIPT_SOURCES = frozenset(_TRANSCRIPT_INDEXERS)


def diff_index(disk: list[FileStat], indexed: list[IndexedFile]) -> ReconcilePlan:
    """Pure diff between the filesystem and the files table.

    Any change to a file — append, rewrite, copy-in from sync — resolves to a
    single `rewritten` op: the file's index rows are dropped and the whole
    file is re-read and re-indexed. There is no incremental-append fast path.
    """
    indexed_by_path = {ix.path: ix for ix in indexed}
    disk_paths = set()
    new, rewritten = [], []

    for fs in disk:
        disk_paths.add(fs.path)
        ix = indexed_by_path.get(fs.path)
        if ix is None:
            new.append(fs)
            continue
        # ctime is the belt to mtime's suspenders: any content write moves
        # both, but a copy/sync that restores the source mtime still leaves a
        # fresh ctime, so OR-ing them closes the mtime-preserving-edit gap.
        # mtime is kept for filesystems with unreliable ctime (NFS/SMB/FAT).
        stat_changed = (fs.mtime_ns != ix.mtime_ns or fs.ctime_ns != ix.ctime_ns
                        or fs.size != ix.size)
        if fs.source in _TRANSCRIPT_SOURCES:
            # Append-only transcripts re-index whole on any change. An
            # unchanged stat with size > indexed_bytes means the last scan saw
            # a torn trailing line — re-index until the newline lands.
            if not stat_changed and fs.size <= ix.indexed_bytes:
                continue
            rewritten.append((fs, ix))
        else:
            if stat_changed:
                rewritten.append((fs, ix))

    removed = [ix for ix in indexed if ix.path not in disk_paths]
    return ReconcilePlan(
        new=tuple(new),
        rewritten=tuple(rewritten),
        removed=tuple(removed),
    )


def parse_jsonl_texts(raw_lines: list[str], filepath: str) -> list[dict]:
    """json.loads each raw JSONL line (blanks skipped, malformed warned).
    Delegates to the shared transcript_jsonl tokenizer."""
    return parse_jsonl_lines(raw_lines, filepath)


# ---------------------------------------------------------------------------
# Imperative shell
# ---------------------------------------------------------------------------

@contextmanager
def _refresh_lock(db_path: Path):
    """Exclusive advisory lock serializing index refreshes across processes.

    Held on a separate <db_path>.lock file (never the db itself, so it does
    not interfere with SQLite's own WAL locking). Blocks until acquired; a
    competing full rebuild holds it ~10s worst case, normally a few ms.
    """
    lock_path = Path(str(db_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Discard an aborted transaction so a reused connection can BEGIN again.
    A failed statement leaves the transaction open in sqlite3; without this a
    subsequent reconcile() hits 'cannot start a transaction within a
    transaction'. Best-effort: a dead connection has nothing to roll back."""
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _delete_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def drop_index(db_path: Path) -> None:
    """Delete the index so the next open_index() rebuilds it from scratch.
    Backs --reindex; the index is derived state, so this is always safe."""
    _delete_db_files(db_path)


def open_index(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open (or bootstrap) the index database.

    A corrupt or stale-schemed database is deleted and rebuilt — the index
    is derived state. Returns None when SQLite is unusable; callers fall
    back to the full scan.

    The open/rebuild runs under the same advisory lock as refresh(): a schema
    bump (or corruption) makes every concurrent process delete-and-recreate
    the db at once, and without the lock one process can write through a conn
    pointing at an inode another already replaced. The lock is released before
    returning; refresh() re-acquires it for the indexing pass.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _refresh_lock(db_path):
        return _open_index_locked(db_path)


def _open_index_locked(db_path: Path) -> Optional[sqlite3.Connection]:
    for attempt in (1, 2):
        conn = None
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == schema_identity():
                return conn
            empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
            if empty:
                conn.executescript(SCHEMA_SQL)
                conn.execute(f"PRAGMA user_version = {schema_identity()}")
                conn.commit()
                return conn
            # Stale schema or extractor source changed: rebuild from scratch
            # on the next attempt.
            conn.close()
            conn = None
            _delete_db_files(db_path)
        except sqlite3.Error as e:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if attempt == 1:
                _delete_db_files(db_path)
            else:
                print(
                    warning(f"Warning: search index unavailable ({e}); using full scan.", stream=sys.stderr),
                    file=sys.stderr,
                )
                return None
    return None


def _scan_suffix(dir_path, suffix: str):
    """Yield (path_str, os.stat_result) for entries in dir_path whose name
    ends with suffix, reproducing Path.glob('*' + suffix): name-based
    matching (so dotfiles match and matching is case-sensitive), stat that
    follows symlinks, entries failing to stat skipped silently. A missing or
    unreadable directory yields nothing."""
    try:
        entries = os.scandir(dir_path)
    except OSError:
        return
    with entries:
        for entry in entries:
            if not entry.name.endswith(suffix):
                continue
            try:
                st = entry.stat()  # follows symlinks, like Path.stat()
            except OSError:
                continue
            yield entry.path, st


def _is_jsonl(name: str) -> bool:
    return name.endswith(".jsonl")


def _is_codex_rollout(name: str) -> bool:
    """A Codex rollout transcript: rollout-<ISO-ts>-<uuid>.jsonl. The prefix
    distinguishes a session rollout from any other .jsonl Codex may drop in the
    tree, so only real transcripts are indexed."""
    return name.startswith("rollout-") and name.endswith(".jsonl")


def _walk_jsonl(root, name_matches: Callable[[str], bool] = _is_jsonl, depth: int = 1):
    """Yield (path_str, os.stat_result, depth) for every regular file at or below
    root whose basename satisfies name_matches (default: any '*.jsonl'), without
    following symlinks: symlinked directories are not descended into and symlinked
    matching entries are skipped entirely. depth counts levels below the source
    root, so a direct child of root is depth 1.

    Not following symlinks is a deliberate security boundary: only files that
    physically live inside a configured source get stat'd, indexed, and later
    read for search. A symlink planted in the tree cannot pull out-of-tree
    content into the index. It also removes the duplicate-row / double-count that
    a symlinked project dir used to produce."""
    try:
        entries = os.scandir(root)
    except OSError:
        return
    with entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_jsonl(entry.path, name_matches, depth + 1)
            elif name_matches(entry.name):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                yield entry.path, st, depth


def scan_disk(data_dir: Path, cc_sources: list[tuple[str, Path]],
              codex_sources: list[tuple[str, Path]] | None = None) -> list[FileStat]:
    """Stat every archive file, walking directories exactly like
    search_archive() and search_claude_code_archive() do.

    Uses os.scandir rather than pathlib/glob for speed while preserving the
    exact classification of the old implementation; see the characterization
    tests in tests/test_search_index.py.

    codex_sources defaults to none so existing callers are undisturbed; when
    given, each (host, root) is walked for rollout-*.jsonl transcripts."""
    stats: list[FileStat] = []

    for provider in providers.ingest_dir_providers():
        provider_dir = data_dir / provider
        try:
            user_entries = os.scandir(provider_dir)
        except OSError:
            continue
        with user_entries:
            for user_entry in user_entries:
                if not user_entry.is_dir():  # follows symlinks, like Path.is_dir()
                    continue
                email = user_entry.name
                for subdir, item_type in WEB_EXPORT_SUBDIRS:
                    item_dir = os.path.join(user_entry.path, subdir)
                    for path, st in _scan_suffix(item_dir, ".json"):
                        stats.append(FileStat(
                            path=path, source=SOURCE_LLM,
                            mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size,
                            provider=provider, email=email, item_type=item_type,
                        ))

    for host, cc_data_dir in cc_sources:
        # A single non-symlink-following walk classifies by depth: a *.jsonl
        # that is a direct child of a depth-1 project directory (i.e. depth 2)
        # is a searchable transcript (SOURCE_CC, keyed by that directory's
        # name); every other *.jsonl — root level or deeper, e.g. subagents —
        # feeds the tool leaderboard only (SOURCE_CC_TOOLS).
        for path, st, depth in _walk_jsonl(cc_data_dir):
            if depth == 2:
                stats.append(FileStat(
                    path=path, source=SOURCE_CC,
                    mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size,
                    email=os.path.basename(os.path.dirname(path)), host=host,
                ))
            else:
                stats.append(FileStat(
                    path=path, source=SOURCE_CC_TOOLS,
                    mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size, host=host,
                ))

    for host, codex_data_dir in (codex_sources or []):
        # Codex's layout is sessions/YYYY/MM/DD/rollout-*.jsonl, so unlike CC
        # there is no depth-based classification and no project-slug dir: match
        # rollout files by name at any depth, all searchable (SOURCE_CODEX), and
        # leave email empty (cwd from session_meta is the project identity).
        for path, st, _depth in _walk_jsonl(codex_data_dir, _is_codex_rollout):
            stats.append(FileStat(
                path=path, source=SOURCE_CODEX,
                mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size,
                email="", host=host,
            ))

    return stats


def load_indexed_files(conn: sqlite3.Connection) -> list[IndexedFile]:
    rows = conn.execute(
        f"SELECT {_FILES_SELECT_SQL} FROM files"
    ).fetchall()
    return [IndexedFile(*row) for row in rows]


def _read_complete_lines(path: str) -> tuple[list[str], int]:
    """Read the whole file's raw JSONL lines, stopping at the last newline.

    A torn trailing line (active session mid-write) stays unindexed; the
    returned end offset only covers complete lines, so the next reconcile
    picks the line up once its newline lands.

    Decoding is strict UTF-8: an invalid byte raises UnicodeDecodeError,
    which the caller treats as an unreadable file (skip + warn). This matches
    the scan path (claude_code_parser.parse_jsonl opens strict-UTF-8); a
    lenient errors="replace" here would silently index mojibake the scan path
    refuses to read.
    """
    with open(path, "rb") as f:
        buf = f.read()
    # Validate the ENTIRE buffer as strict UTF-8 first — including any torn
    # trailing line (a partial multibyte char at EOF decodes as invalid). The
    # scan path opens the whole file strict-UTF-8 and fails it on the first bad
    # byte anywhere; validating only the complete-line prefix would silently
    # index a mid-write file the scan path skips wholesale, diverging the two.
    buf.decode("utf-8")
    last_nl = buf.rfind(b"\n")
    if last_nl == -1:
        return [], 0
    # Split on byte-\n only: str.splitlines() also breaks on  /\x85
    # etc., which are legal inside JSON strings and would shred valid lines.
    # A 0x0a byte is never part of a multibyte UTF-8 sequence, so splitting
    # the already-validated buffer cannot tear a character.
    complete = buf[:last_nl]
    lines = [seg.decode("utf-8") for seg in complete.split(b"\n")]
    return lines, last_nl + 1


def _delete_file_rows(conn: sqlite3.Connection, file_id: int) -> None:
    # FK cascades cover items/fts_map/cc_tool_counts but not the FTS virtual
    # table, whose rows must be deleted explicitly first.
    conn.execute(
        "DELETE FROM fts WHERE rowid IN (SELECT fts_rowid FROM fts_map WHERE file_id = ?)",
        (file_id,),
    )
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def _insert_fts_segment(conn: sqlite3.Connection, file_id: int, body: str) -> None:
    if not body:
        return
    cur = conn.execute("INSERT INTO fts(body) VALUES (?)", (body,))
    conn.execute(
        "INSERT INTO fts_map(fts_rowid, file_id) VALUES (?, ?)",
        (cur.lastrowid, file_id),
    )


def _insert_file_row(conn: sqlite3.Connection, fs: FileStat,
                     indexed_bytes: int) -> int:
    cur = conn.execute(
        _FILES_INSERT_SQL,
        (fs.path, fs.source, fs.mtime_ns, fs.ctime_ns, fs.size, indexed_bytes),
    )
    return cur.lastrowid


def _insert_file_texts(conn: sqlite3.Connection, file_id: int, texts: list) -> None:
    """Store the original-case extracted texts as a JSON array. Written for
    every searchable file, including zero-text files ([]), so the rescore path
    can tell "no texts" (score nothing, no fallback) from a missing row (read
    the real file)."""
    conn.execute(
        "INSERT INTO file_texts(file_id, texts) VALUES (?, ?)",
        (file_id, json.dumps(texts)),
    )


def _insert_item_row(conn: sqlite3.Connection, file_id: int, provider: str,
                     email: str, meta: dict) -> None:
    conn.execute(
        _ITEMS_INSERT_SQL,
        (file_id, meta["uuid"], meta["item_type"], provider, meta["name"],
         meta.get("name_raw", meta["name"]), meta["created_at"], meta["updated_at"],
         email, meta.get("model", ""), meta["host"], meta["cwd"], meta["git_branch"],
         meta["preview"], int(meta["has_preview"])),
    )


def _index_llm_file(conn: sqlite3.Connection, fs: FileStat,
                    extract_llm_texts: Callable[[dict, str, str], list],
                    extract_llm_model: Callable[[dict, str, str], str]) -> Optional[str]:
    """Index one claude/chatgpt JSON file. Returns the path on failure.

    A file that cannot be read or parsed gets NO files row: it stays out of
    the index, is retried (and warned about) on every run, and self-heals if
    a partial cloud sync later completes. Corrupt archive files must stay
    loud — they should never exist, so they warrant manual attention rather
    than a silent permanent skip.
    """
    try:
        raw = Path(fs.path).read_bytes()
        data = json.loads(raw)
        texts = extract_llm_texts(data, fs.item_type, fs.provider)
        meta = make_llm_item_meta(data, fs.item_type, texts)
        meta["model"] = extract_llm_model(data, fs.item_type, fs.provider)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(warning(f"Warning: could not index {fs.path}: {e}", stream=sys.stderr), file=sys.stderr)
        return fs.path
    file_id = _insert_file_row(conn, fs, len(raw))
    _insert_fts_segment(conn, file_id, searchable_body(texts))
    _insert_file_texts(conn, file_id, texts)
    _insert_item_row(conn, file_id, fs.provider, fs.email, meta)
    return None


def _index_transcript_file(conn: sqlite3.Connection, fs: FileStat,
                           indexer: _TranscriptIndexer) -> Optional[str]:
    """Index one append-only JSONL transcript via `indexer`. Returns the path on
    failure (unreadable file; retried and re-warned every run, like LLM files).

    The reader (_read_complete_lines, strict-UTF-8 + torn-trailing-line aware)
    and the JSONL parse stay here — only the per-provider text/metadata/tool
    extraction is injected via the indexer's parser and meta_builder."""
    try:
        raw_lines, end_offset = _read_complete_lines(fs.path)
    except (OSError, UnicodeDecodeError) as e:
        # Invalid UTF-8 is treated like an unreadable file: no index rows, the
        # path is returned as failed (loud banner every run), exactly as the
        # scan path's strict-UTF-8 open skips-and-warns the whole file.
        print(warning(f"Warning: could not index {fs.path}: {e}", stream=sys.stderr), file=sys.stderr)
        return fs.path
    lines = parse_jsonl_texts(raw_lines, fs.path)
    file_id = _insert_file_row(conn, fs, end_offset)
    if fs.source == indexer.searchable:
        texts = indexer.parser.extract_searchable_text(lines)
        meta = indexer.meta_builder(indexer.parser.extract_session_metadata(lines), texts)
        meta["host"] = fs.host
        _insert_fts_segment(conn, file_id, searchable_body(texts))
        _insert_file_texts(conn, file_id, texts)
        _insert_item_row(conn, file_id, indexer.provider, fs.email, meta)
    for tool, count in indexer.parser.count_tool_uses(lines).items():
        conn.execute(
            "INSERT INTO cc_tool_counts(file_id, tool, count) VALUES (?, ?, ?)",
            (file_id, tool, count),
        )
    return None


def render_progress_bar(done: int, total: int, width: int = PROGRESS_BAR_WIDTH) -> str:
    """Render a single-line progress bar, e.g. '[######----] 60/100 (60%)'.

    Pure: builds the string for `done` of `total` items. `total` of 0 reads
    as complete (an empty plan has nothing left to do).
    """
    frac = 1.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = round(frac * width)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {done}/{total} ({frac * 100:.0f}%)"


def reconcile(conn: sqlite3.Connection, plan: ReconcilePlan,
              extract_llm_texts: Callable[[dict, str, str], list],
              extract_llm_model: Callable[[dict, str, str], str]) -> list:
    """Apply a ReconcilePlan in batched transactions (interrupt-safe: a
    killed run loses at most one uncommitted batch and resumes next run).

    Returns the paths of files that could not be indexed (unreadable or
    corrupt); they hold no index rows and will be retried — and re-warned
    about — on every run until fixed or removed.
    """
    total = len(plan.new) + len(plan.rewritten) + len(plan.removed)
    # Animate a bar only on an interactive stderr; otherwise (pipes, logs) the
    # \r repaint would spam, so fall back to a single static line.
    show_progress = total >= PROGRESS_MIN_FILES
    animate = show_progress and sys.stderr.isatty()
    if show_progress and not animate:
        print(muted(f"Indexing {total} archive file(s)...", stream=sys.stderr), file=sys.stderr)

    def ops():
        for ix in plan.removed:
            yield ("remove", None, ix)
        for fs, ix in plan.rewritten:
            yield ("rewrite", fs, ix)
        for fs in plan.new:
            yield ("new", fs, None)

    failed = []
    pending = 0
    done = 0
    conn.execute("BEGIN IMMEDIATE")
    for op, fs, ix in ops():
        if op == "remove":
            _delete_file_rows(conn, ix.id)
        elif op == "rewrite":
            _delete_file_rows(conn, ix.id)
            failed.append(_index_file(conn, fs, extract_llm_texts, extract_llm_model))
        else:
            failed.append(_index_file(conn, fs, extract_llm_texts, extract_llm_model))
        done += 1
        if animate:
            print("\r" + muted(f"Indexing {render_progress_bar(done, total)}", stream=sys.stderr),
                  end="", file=sys.stderr, flush=True)
        pending += 1
        if pending >= RECONCILE_BATCH:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            pending = 0
    conn.commit()
    if animate:
        print(file=sys.stderr)  # terminate the bar's line
    return [path for path in failed if path is not None]


def _index_file(conn, fs: FileStat, extract_llm_texts, extract_llm_model) -> Optional[str]:
    if fs.source == SOURCE_LLM:
        return _index_llm_file(conn, fs, extract_llm_texts, extract_llm_model)
    return _index_transcript_file(conn, fs, _TRANSCRIPT_INDEXERS[fs.source])


def refresh(conn: sqlite3.Connection, data_dir: Path,
            cc_sources: list[tuple[str, Path]],
            extract_llm_texts: Callable[[dict, str, str], list],
            extract_llm_model: Callable[[dict, str, str], str],
            codex_sources: list[tuple[str, Path]] | None = None) -> Optional[list]:
    """Bring the index up to date with the filesystem.

    Returns the (usually empty) list of file paths that could not be
    indexed, or None when the index itself could not be refreshed and the
    caller should fall back to scanning.

    The whole scan-disk → diff → reconcile sequence runs under an exclusive
    advisory lock on <db_path>.lock, serializing concurrent refreshes (the
    user searches while sessions are being written; two terminals are
    realistic). Without it, two processes can both plan an INSERT for the
    same new file and the loser hits the files.path UNIQUE constraint. The
    diff runs against the now-updated index after acquiring, so the loser's
    refresh naturally becomes a no-op. Readers take no lock.
    """
    try:
        raw_db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    except sqlite3.Error as e:
        print(warning(f"Warning: search index busy ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None
    # An in-memory db (empty path) is private to this process — nothing to
    # serialize against, and no real file to lock — so skip the lock there.
    lock = _refresh_lock(Path(raw_db_path)) if raw_db_path else nullcontext()
    try:
        with lock:
            disk = scan_disk(data_dir, cc_sources, codex_sources)
            plan = diff_index(disk, load_indexed_files(conn))
            if plan.is_noop():
                return []
            return reconcile(conn, plan, extract_llm_texts, extract_llm_model)
    except sqlite3.IntegrityError as e:
        # A concurrent refresh raced us to a new files.path row (or a UNIQUE
        # constraint otherwise tripped). This is contention, never
        # corruption: warn and fall back to the full scan this run; the next
        # run sees the already-indexed file and no-ops. Never delete the db.
        # Roll back the aborted reconcile transaction so a reused connection
        # can BEGIN cleanly next time.
        _rollback_quietly(conn)
        print(warning(f"Warning: search index busy ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None
    except sqlite3.DatabaseError as e:
        # Corruption: drop the db so the next run rebuilds it cleanly.
        # (OperationalError — e.g. locked — is a subclass; deleting on a
        # transient lock would be wrong, so it is handled first.)
        if isinstance(e, sqlite3.OperationalError):
            _rollback_quietly(conn)
            print(warning(f"Warning: search index busy ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
            return None
        try:
            db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            conn.close()
            _delete_db_files(db_path)
        except sqlite3.Error:
            pass
        print(warning(f"Warning: search index corrupt ({e}); rebuilt on next run. Using full scan.", stream=sys.stderr),
              file=sys.stderr)
        return None


def candidate_paths(conn: sqlite3.Connection, fts_query: str,
                    source: str) -> Optional[set]:
    """Paths of files whose FTS body matches — a superset of the files the
    scan path would surface. None means the index could not answer."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT f.path FROM fts "
            "JOIN fts_map m ON fts.rowid = m.fts_rowid "
            "JOIN files f ON f.id = m.file_id "
            "WHERE fts MATCH ? AND f.source = ?",
            (fts_query, source),
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error as e:
        print(warning(f"Warning: search index query failed ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None


# Columns every rescore row carries, in SELECT order. Mirrors what the scan
# path reconstructs per file (make_llm_item_meta / make_local_cli_item_meta + the
# raw name and stored texts) so results_from_index_rows can rebuild a
# SearchResult without touching the file.
# One ordered (key, sql_expr) projection: _ROW_KEYS and _ROW_SELECT derive from
# it, so they can't drift out of alignment (reordering one alone would silently
# mislabel every rescore row — e.g. created_at values landing under updated_at).
_ROW_COLUMNS = (
    ("path", "f.path"),
    ("texts", "t.texts"),
    ("uuid", "i.uuid"),
    ("item_type", "i.item_type"),
    ("provider", "i.provider"),
    ("name", "i.name"),
    ("name_raw", "i.name_raw"),
    ("created_at", "i.created_at"),
    ("updated_at", "i.updated_at"),
    ("email", "i.email"),
    ("model", "i.model"),
    ("host", "i.host"),
    ("cwd", "i.cwd"),
    ("git_branch", "i.git_branch"),
)
_ROW_KEYS = tuple(k for k, _ in _ROW_COLUMNS)
_ROW_SELECT = ", ".join(expr for _, expr in _ROW_COLUMNS)

# Browse-mode projection (no texts/name_raw; adds preview), same derive pattern.
_BROWSE_COLUMNS = (
    ("uuid", "i.uuid"),
    ("item_type", "i.item_type"),
    ("provider", "i.provider"),
    ("name", "i.name"),
    ("created_at", "i.created_at"),
    ("updated_at", "i.updated_at"),
    ("email", "i.email"),
    ("model", "i.model"),
    ("host", "i.host"),
    ("cwd", "i.cwd"),
    ("git_branch", "i.git_branch"),
    ("preview", "i.preview"),
    ("path", "f.path"),
)
_BROWSE_KEYS = tuple(k for k, _ in _BROWSE_COLUMNS)
_BROWSE_SELECT = ", ".join(expr for _, expr in _BROWSE_COLUMNS)


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """Wrap a search's index reads in one deferred transaction so they all
    observe a single WAL snapshot.

    Without it, a concurrent refresh() committing between two reads of one
    search could pair an old candidate list with newer texts (a silent drop).
    Reads only: the block is committed on exit (nothing was written), and a
    BEGIN is issued only if no transaction is already open, so the helper
    nests harmlessly."""
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if started:
            conn.commit()


def candidate_rows(conn: sqlite3.Connection, fts_query: str,
                   source: str) -> Optional[list]:
    """Rescore rows for files whose FTS body matches — a superset of the scan
    path's matches, with stored texts + item metadata attached so the caller
    never re-reads the file. file_texts is LEFT JOINed: a missing row surfaces
    as texts=None (→ caller falls back to the real file), never a dropped file.
    None means the index could not answer."""
    try:
        rows = conn.execute(
            f"SELECT {_ROW_SELECT} FROM fts "
            "JOIN fts_map m ON fts.rowid = m.fts_rowid "
            "JOIN files f ON f.id = m.file_id "
            "JOIN items i ON i.file_id = f.id "
            "LEFT JOIN file_texts t ON t.file_id = f.id "
            "WHERE fts MATCH ? AND f.source = ? ORDER BY f.path",
            (fts_query, source),
        ).fetchall()
    except sqlite3.Error as e:
        print(warning(f"Warning: search index query failed ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None
    return [dict(zip(_ROW_KEYS, row)) for row in rows]


def all_searchable_rows(conn: sqlite3.Connection, source: str) -> Optional[list]:
    """Rescore rows for every searchable file in `source`, no FTS filter —
    serves queries the trigram index can't (build_fts_query returns None for
    sub-3-char words), replacing the full filesystem scan for those. Same row
    shape as candidate_rows. None on db error."""
    try:
        rows = conn.execute(
            f"SELECT {_ROW_SELECT} FROM files f "
            "JOIN items i ON i.file_id = f.id "
            "LEFT JOIN file_texts t ON t.file_id = f.id "
            "WHERE f.source = ? ORDER BY f.path",
            (source,),
        ).fetchall()
    except sqlite3.Error as e:
        print(warning(f"Warning: search index query failed ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None
    return [dict(zip(_ROW_KEYS, row)) for row in rows]


def browse_items(conn: sqlite3.Connection, source: str) -> Optional[list]:
    """All indexed items with a preview, for browse mode. Ordered by path
    for determinism (the scan path's iterdir order is filesystem-dependent;
    browse output is re-sorted by recency either way). None on db error —
    callers fall back to the scan path."""
    try:
        rows = conn.execute(
            f"SELECT {_BROWSE_SELECT} "
            "FROM items i JOIN files f ON f.id = i.file_id "
            "WHERE i.has_preview = 1 AND f.source = ? ORDER BY f.path",
            (source,),
        ).fetchall()
    except sqlite3.Error as e:
        print(warning(f"Warning: search index query failed ({e}); using full scan.", stream=sys.stderr), file=sys.stderr)
        return None
    return [dict(zip(_BROWSE_KEYS, row)) for row in rows]


def tool_counts(conn: sqlite3.Connection,
                sources: Optional[list] = None) -> Optional[Counter]:
    """Transcript tool_use totals across the archive (for --stats).

    With `sources` given, only files of those sources are counted (e.g.
    [SOURCE_CC, SOURCE_CC_TOOLS] for Claude Code only, or [SOURCE_CODEX] for
    Codex only), keeping the index path's leaderboard scoped exactly like the
    per-source scan fallback. None counts every source. None return on db error
    — callers fall back to scanning the JSONL files."""
    try:
        if sources:
            placeholders = ",".join("?" * len(sources))
            rows = conn.execute(
                f"SELECT c.tool, SUM(c.count) FROM cc_tool_counts c "
                f"JOIN files f ON f.id = c.file_id "
                f"WHERE f.source IN ({placeholders}) GROUP BY c.tool",
                tuple(sources),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tool, SUM(count) FROM cc_tool_counts GROUP BY tool"
            ).fetchall()
    except sqlite3.Error:
        return None
    return Counter(dict(rows))


def lookup_uuid(conn: sqlite3.Connection, uuid: str) -> Optional[tuple]:
    """Resolve a claude/chatgpt item uuid to (filepath, provider), or None.
    Callers must verify the file still contains this uuid (the index may be
    stale) and fall back to scanning on a miss."""
    try:
        row = conn.execute(
            "SELECT f.path, i.provider FROM items i JOIN files f ON f.id = i.file_id "
            "WHERE i.uuid = ? AND f.source = ? LIMIT 1",
            (uuid, SOURCE_LLM),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return Path(row[0]), row[1]
