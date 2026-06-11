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

import functools
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import claude_code_parser as ccp

SCHEMA_VERSION = 3
HEAD_HASH_BYTES = 1024
TAIL_HASH_BYTES = 1024
RECONCILE_BATCH = 200

# Modules whose source determines what ends up in the index: the extractors,
# the metadata derivation, and this module itself. Their bytes are hashed
# into the schema identity (see schema_identity) so that ANY change to
# extraction logic automatically invalidates the index — without this, an
# extractor edit would leave stale FTS bodies that silently drop matches.
SCHEMA_SOURCE_FILES = (
    "search_index.py",
    "claude_code_parser.py",
    "full_text_search_chats_archive.py",
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
    base = Path(__file__).resolve().parent if src_dir is None else src_dir
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
    indexed_bytes INTEGER NOT NULL,         -- jsonl: end of last complete line
    head_hash     TEXT NOT NULL,            -- sha1 of first head_len bytes
    head_len      INTEGER NOT NULL,         -- distinguishes append vs rewrite
    tail_hash     TEXT NOT NULL             -- sha1 of last TAIL_HASH_BYTES of
                                            -- the indexed region; catches mid-
                                            -- file rewrites the head hash misses
);

CREATE TABLE items (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    uuid        TEXT NOT NULL,
    item_type   TEXT NOT NULL,              -- 'conversation' | 'project'
    provider    TEXT NOT NULL,              -- 'claude' | 'chatgpt' | 'claude-code'
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    email       TEXT NOT NULL,              -- account email; project slug for claude-code
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
"""


@dataclass(frozen=True)
class FileStat:
    """One archive file as seen on disk, with the walk-derived context
    needed to index it."""
    path: str
    source: str          # SOURCE_LLM | SOURCE_CC
    mtime_ns: int
    ctime_ns: int
    size: int
    provider: str = ""   # llm: 'claude' | 'chatgpt'
    email: str = ""      # llm: account email; cc: project slug
    item_type: str = ""  # llm: 'conversation' | 'project'
    host: str = ""       # cc only


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
    head_hash: str
    head_len: int
    tail_hash: str


@dataclass(frozen=True)
class ReconcilePlan:
    new: tuple
    rewritten: tuple        # (FileStat, IndexedFile)
    maybe_appended: tuple   # (FileStat, IndexedFile); head_hash check decides
    removed: tuple          # IndexedFile

    def is_noop(self) -> bool:
        return not (self.new or self.rewritten or self.maybe_appended or self.removed)


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
    name = data.get("name", "")
    updated_at = data.get("updated_at", data["created_at"])
    if item_type == "conversation":
        messages = data.get("chat_messages", [])
        if messages:
            last_msg_date = messages[-1].get("created_at", "")
            if last_msg_date:
                updated_at = last_msg_date
    preview, has_preview = preview_from_texts(texts)
    return {
        "uuid": data["uuid"],
        "item_type": item_type,
        "name": name if name else "(untitled)",
        "created_at": data["created_at"],
        "updated_at": updated_at,
        "host": "",
        "cwd": "",
        "git_branch": "",
        "preview": preview,
        "has_preview": has_preview,
    }


def make_cc_item_meta(metadata: dict, texts: list[str]) -> dict:
    """Item metadata for a Claude Code JSONL session from its parsed
    extract_session_metadata() dict."""
    preview, has_preview = preview_from_texts(texts)
    return {
        "uuid": metadata["session_id"],
        "item_type": "conversation",
        "name": metadata["name"],
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
        "host": "",  # filled from FileStat by the caller
        "cwd": metadata["cwd"],
        "git_branch": metadata["git_branch"],
        "preview": preview,
        "has_preview": has_preview,
    }


def merge_cc_item_meta(existing: dict, tail: dict) -> dict:
    """Merge metadata extracted from an appended JSONL tail into the stored
    item row, reproducing what a full re-parse would derive.

    First-occurrence fields (uuid, name, cwd, git_branch, created_at,
    preview) keep their stored value unless it is still unset; updated_at
    tracks the last timestamped line, so the tail wins when it has one.
    """
    merged = dict(existing)
    if not merged["uuid"]:
        merged["uuid"] = tail["uuid"]
    if merged["name"] == "(untitled)":
        merged["name"] = tail["name"]
    if not merged["created_at"]:
        merged["created_at"] = tail["created_at"]
    if tail["updated_at"]:
        merged["updated_at"] = tail["updated_at"]
    for key in ("cwd", "git_branch"):
        if not merged[key]:
            merged[key] = tail[key]
    if not merged["has_preview"] and tail["has_preview"]:
        merged["preview"] = tail["preview"]
        merged["has_preview"] = True
    return merged


def diff_index(disk: list[FileStat], indexed: list[IndexedFile]) -> ReconcilePlan:
    """Pure diff between the filesystem and the files table."""
    indexed_by_path = {ix.path: ix for ix in indexed}
    disk_paths = set()
    new, rewritten, maybe_appended = [], [], []

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
        if fs.source in (SOURCE_CC, SOURCE_CC_TOOLS):
            # Append-only transcripts: growth is (probably) an append; the
            # head-hash check in reconcile() demotes prefix changes to a
            # rewrite. size > indexed_bytes with unchanged stat happens when
            # the last scan saw a torn trailing line — reparse the tail.
            if not stat_changed and fs.size <= ix.indexed_bytes:
                continue
            if fs.size >= ix.indexed_bytes and fs.size >= ix.size:
                maybe_appended.append((fs, ix))
            else:
                rewritten.append((fs, ix))
        else:
            if stat_changed:
                rewritten.append((fs, ix))

    removed = [ix for ix in indexed if ix.path not in disk_paths]
    return ReconcilePlan(
        new=tuple(new),
        rewritten=tuple(rewritten),
        maybe_appended=tuple(maybe_appended),
        removed=tuple(removed),
    )


def parse_jsonl_texts(raw_lines: list[str], filepath: str) -> list[dict]:
    """json.loads each raw JSONL line, skipping blanks and warning on
    malformed lines — same behavior as claude_code_parser.parse_jsonl()."""
    lines = []
    for i, raw in enumerate(raw_lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError as e:
            print(f"Warning: {filepath}:{i}: malformed JSON: {e}", file=sys.stderr)
    return lines


# ---------------------------------------------------------------------------
# Imperative shell
# ---------------------------------------------------------------------------

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
    """
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
                    f"Warning: search index unavailable ({e}); using full scan.",
                    file=sys.stderr,
                )
                return None
    return None


def scan_disk(data_dir: Path, cc_sources: list[tuple[str, Path]]) -> list[FileStat]:
    """Stat every archive file, walking directories exactly like
    search_archive() and search_claude_code_archive() do."""
    stats: list[FileStat] = []

    for provider in ["claude", "chatgpt"]:
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue
        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue
            email = user_dir.name
            for subdir, item_type in (("conversations", "conversation"), ("projects", "project")):
                item_dir = user_dir / subdir
                if not item_dir.exists():
                    continue
                for f in item_dir.glob("*.json"):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    stats.append(FileStat(
                        path=str(f), source=SOURCE_LLM,
                        mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size,
                        provider=provider, email=email, item_type=item_type,
                    ))

    for host, cc_data_dir in cc_sources:
        if not cc_data_dir.exists():
            continue
        searchable = set()
        for project_dir in cc_data_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                searchable.add(f)
                stats.append(FileStat(
                    path=str(f), source=SOURCE_CC,
                    mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size,
                    email=project_dir.name, host=host,
                ))
        # Deeper transcripts (subagents) feed the tool leaderboard only.
        for f in cc_data_dir.rglob("*.jsonl"):
            if f in searchable:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            stats.append(FileStat(
                path=str(f), source=SOURCE_CC_TOOLS,
                mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, size=st.st_size, host=host,
            ))

    return stats


def load_indexed_files(conn: sqlite3.Connection) -> list[IndexedFile]:
    rows = conn.execute(
        "SELECT id, path, source, mtime_ns, ctime_ns, size, indexed_bytes, head_hash, head_len, tail_hash FROM files"
    ).fetchall()
    return [IndexedFile(*row) for row in rows]


def _read_complete_lines(path: str, offset: int) -> tuple[list[str], int, bytes]:
    """Read raw JSONL lines from byte offset, stopping at the last newline.

    A torn trailing line (active session mid-write) stays unindexed; the
    returned end offset only covers complete lines, so the next reconcile
    picks the line up once its newline lands. Also returns the file head
    (first HEAD_HASH_BYTES) for hash bookkeeping.
    """
    with open(path, "rb") as f:
        head = f.read(HEAD_HASH_BYTES)
        f.seek(offset)
        buf = f.read()
    last_nl = buf.rfind(b"\n")
    if last_nl == -1:
        return [], offset, head
    # Split on byte-\n only: str.splitlines() also breaks on  /\x85
    # etc., which are legal inside JSON strings and would shred valid lines.
    complete = buf[:last_nl]
    lines = [seg.decode("utf-8", errors="replace") for seg in complete.split(b"\n")]
    return lines, offset + last_nl + 1, head


def _head_hash(path: str, length: int) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(length)).hexdigest()


def _tail_hash_bytes(region: bytes) -> str:
    """sha1 of the last TAIL_HASH_BYTES of an in-memory indexed region."""
    return hashlib.sha1(region[-TAIL_HASH_BYTES:]).hexdigest()


def _tail_hash(path: str, end: int) -> str:
    """sha1 of the last TAIL_HASH_BYTES of the on-disk region [0, end).
    Equals _tail_hash_bytes() over the same region."""
    start = max(0, end - TAIL_HASH_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        return hashlib.sha1(f.read(end - start)).hexdigest()


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
                     indexed_bytes: int, head: bytes, tail_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes, head_hash, head_len, tail_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fs.path, fs.source, fs.mtime_ns, fs.ctime_ns, fs.size, indexed_bytes,
         hashlib.sha1(head).hexdigest(), len(head), tail_hash),
    )
    return cur.lastrowid


def _insert_item_row(conn: sqlite3.Connection, file_id: int, provider: str,
                     email: str, meta: dict) -> None:
    conn.execute(
        "INSERT INTO items(file_id, uuid, item_type, provider, name, created_at, "
        "updated_at, email, host, cwd, git_branch, preview, has_preview) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (file_id, meta["uuid"], meta["item_type"], provider, meta["name"],
         meta["created_at"], meta["updated_at"], email, meta["host"],
         meta["cwd"], meta["git_branch"], meta["preview"], int(meta["has_preview"])),
    )


def _index_llm_file(conn: sqlite3.Connection, fs: FileStat,
                    extract_llm_texts: Callable[[dict, str, str], list]) -> Optional[str]:
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"Warning: could not index {fs.path}: {e}", file=sys.stderr)
        return fs.path
    file_id = _insert_file_row(conn, fs, len(raw), raw[:HEAD_HASH_BYTES],
                               _tail_hash_bytes(raw))
    _insert_fts_segment(conn, file_id, searchable_body(texts))
    _insert_item_row(conn, file_id, fs.provider, fs.email, meta)
    return None


def _index_cc_file(conn: sqlite3.Connection, fs: FileStat) -> Optional[str]:
    """Index one Claude Code JSONL transcript. Returns the path on failure
    (unreadable file; retried and re-warned every run, like LLM files)."""
    try:
        raw_lines, end_offset, head = _read_complete_lines(fs.path, 0)
        tail_hash = _tail_hash(fs.path, end_offset)
    except OSError as e:
        print(f"Warning: could not index {fs.path}: {e}", file=sys.stderr)
        return fs.path
    lines = parse_jsonl_texts(raw_lines, fs.path)
    file_id = _insert_file_row(conn, fs, end_offset, head, tail_hash)
    if fs.source == SOURCE_CC:
        texts = ccp.extract_searchable_text(lines)
        meta = make_cc_item_meta(ccp.extract_session_metadata(lines), texts)
        meta["host"] = fs.host
        _insert_fts_segment(conn, file_id, searchable_body(texts))
        _insert_item_row(conn, file_id, "claude-code", fs.email, meta)
    for tool, count in ccp.count_tool_uses(lines).items():
        conn.execute(
            "INSERT INTO cc_tool_counts(file_id, tool, count) VALUES (?, ?, ?)",
            (file_id, tool, count),
        )
    return None


def _append_cc_file(conn: sqlite3.Connection, fs: FileStat, ix: IndexedFile) -> Optional[str]:
    try:
        raw_lines, end_offset, head = _read_complete_lines(fs.path, ix.indexed_bytes)
        tail_hash = _tail_hash(fs.path, end_offset)
    except OSError as e:
        print(f"Warning: could not index {fs.path}: {e}", file=sys.stderr)
        return fs.path
    lines = parse_jsonl_texts(raw_lines, fs.path)
    if fs.source == SOURCE_CC:
        _append_cc_search_rows(conn, fs, ix, lines)

    for tool, count in ccp.count_tool_uses(lines).items():
        conn.execute(
            "INSERT INTO cc_tool_counts(file_id, tool, count) VALUES (?, ?, ?) "
            "ON CONFLICT(file_id, tool) DO UPDATE SET count = count + excluded.count",
            (ix.id, tool, count),
        )

    # Re-hash the head over up to 1KB of the (possibly longer) file so the
    # append-vs-rewrite check strengthens as small files grow; the tail hash
    # advances to the new end of the indexed region.
    conn.execute(
        "UPDATE files SET mtime_ns = ?, ctime_ns = ?, size = ?, indexed_bytes = ?, head_hash = ?, head_len = ?, tail_hash = ? "
        "WHERE id = ?",
        (fs.mtime_ns, fs.ctime_ns, fs.size, end_offset,
         hashlib.sha1(head).hexdigest(), len(head), tail_hash, ix.id),
    )
    return None


def _append_cc_search_rows(conn: sqlite3.Connection, fs: FileStat,
                           ix: IndexedFile, lines: list) -> None:
    """Add the search-facing rows (fts segment, item metadata merge) for an
    appended tail of a searchable Claude Code session."""
    texts = ccp.extract_searchable_text(lines)
    tail_meta = make_cc_item_meta(ccp.extract_session_metadata(lines), texts)
    tail_meta["host"] = fs.host

    _insert_fts_segment(conn, ix.id, searchable_body(texts))

    row = conn.execute(
        "SELECT uuid, name, created_at, updated_at, cwd, git_branch, preview, has_preview "
        "FROM items WHERE file_id = ?", (ix.id,),
    ).fetchone()
    if row is None:
        _insert_item_row(conn, ix.id, "claude-code", fs.email, tail_meta)
    else:
        existing = {
            "uuid": row[0], "name": row[1], "created_at": row[2],
            "updated_at": row[3], "cwd": row[4], "git_branch": row[5],
            "preview": row[6], "has_preview": bool(row[7]),
            "item_type": "conversation", "host": fs.host,
        }
        merged = merge_cc_item_meta(existing, tail_meta)
        conn.execute(
            "UPDATE items SET uuid = ?, name = ?, created_at = ?, updated_at = ?, "
            "cwd = ?, git_branch = ?, preview = ?, has_preview = ? WHERE file_id = ?",
            (merged["uuid"], merged["name"], merged["created_at"], merged["updated_at"],
             merged["cwd"], merged["git_branch"], merged["preview"],
             int(merged["has_preview"]), ix.id),
        )


def reconcile(conn: sqlite3.Connection, plan: ReconcilePlan,
              extract_llm_texts: Callable[[dict, str, str], list]) -> list:
    """Apply a ReconcilePlan in batched transactions (interrupt-safe: a
    killed run loses at most one uncommitted batch and resumes next run).

    Returns the paths of files that could not be indexed (unreadable or
    corrupt); they hold no index rows and will be retried — and re-warned
    about — on every run until fixed or removed.
    """
    total = len(plan.new) + len(plan.rewritten) + len(plan.maybe_appended) + len(plan.removed)
    if total > 50:
        print(f"Indexing {total} archive file(s)...", file=sys.stderr)

    def ops():
        for ix in plan.removed:
            yield ("remove", None, ix)
        for fs, ix in plan.rewritten:
            yield ("rewrite", fs, ix)
        for fs, ix in plan.maybe_appended:
            yield ("append?", fs, ix)
        for fs in plan.new:
            yield ("new", fs, None)

    failed = []
    pending = 0
    conn.execute("BEGIN IMMEDIATE")
    for op, fs, ix in ops():
        if op == "remove":
            _delete_file_rows(conn, ix.id)
        elif op == "rewrite":
            _delete_file_rows(conn, ix.id)
            failed.append(_index_file(conn, fs, extract_llm_texts))
        elif op == "append?":
            try:
                # Head AND tail of the already-indexed region must both be
                # unchanged; a mid-file rewrite that spares the first 1KB
                # would otherwise be mis-taken for an append and its new
                # middle content never indexed.
                is_append = (_head_hash(fs.path, ix.head_len) == ix.head_hash
                             and _tail_hash(fs.path, ix.indexed_bytes) == ix.tail_hash)
            except OSError:
                continue  # file vanished mid-reconcile; next run handles it
            if is_append:
                failed.append(_append_cc_file(conn, fs, ix))
            else:
                _delete_file_rows(conn, ix.id)
                failed.append(_index_file(conn, fs, extract_llm_texts))
        else:
            failed.append(_index_file(conn, fs, extract_llm_texts))
        pending += 1
        if pending >= RECONCILE_BATCH:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            pending = 0
    conn.commit()
    return [path for path in failed if path is not None]


def _index_file(conn, fs: FileStat, extract_llm_texts) -> Optional[str]:
    if fs.source == SOURCE_LLM:
        return _index_llm_file(conn, fs, extract_llm_texts)
    return _index_cc_file(conn, fs)


def refresh(conn: sqlite3.Connection, data_dir: Path,
            cc_sources: list[tuple[str, Path]],
            extract_llm_texts: Callable[[dict, str, str], list]) -> Optional[list]:
    """Bring the index up to date with the filesystem.

    Returns the (usually empty) list of file paths that could not be
    indexed, or None when the index itself could not be refreshed and the
    caller should fall back to scanning.
    """
    try:
        disk = scan_disk(data_dir, cc_sources)
        plan = diff_index(disk, load_indexed_files(conn))
        if plan.is_noop():
            return []
        return reconcile(conn, plan, extract_llm_texts)
    except sqlite3.DatabaseError as e:
        # Corruption: drop the db so the next run rebuilds it cleanly.
        # (OperationalError — e.g. locked — is a subclass; deleting on a
        # transient lock would be wrong, so it is handled first.)
        if isinstance(e, sqlite3.OperationalError):
            print(f"Warning: search index busy ({e}); using full scan.", file=sys.stderr)
            return None
        try:
            db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            conn.close()
            _delete_db_files(db_path)
        except sqlite3.Error:
            pass
        print(f"Warning: search index corrupt ({e}); rebuilt on next run. Using full scan.",
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
        print(f"Warning: search index query failed ({e}); using full scan.", file=sys.stderr)
        return None


def browse_items(conn: sqlite3.Connection, source: str) -> Optional[list]:
    """All indexed items with a preview, for browse mode. Ordered by path
    for determinism (the scan path's iterdir order is filesystem-dependent;
    browse output is re-sorted by recency either way). None on db error —
    callers fall back to the scan path."""
    try:
        rows = conn.execute(
            "SELECT i.uuid, i.item_type, i.provider, i.name, i.created_at, i.updated_at, "
            "i.email, i.host, i.cwd, i.git_branch, i.preview, f.path "
            "FROM items i JOIN files f ON f.id = i.file_id "
            "WHERE i.has_preview = 1 AND f.source = ? ORDER BY f.path",
            (source,),
        ).fetchall()
    except sqlite3.Error as e:
        print(f"Warning: search index query failed ({e}); using full scan.", file=sys.stderr)
        return None
    keys = ("uuid", "item_type", "provider", "name", "created_at", "updated_at",
            "email", "host", "cwd", "git_branch", "preview", "path")
    return [dict(zip(keys, row)) for row in rows]


def tool_counts(conn: sqlite3.Connection) -> Optional[Counter]:
    """Claude Code tool_use totals across the whole archive (for --stats).
    None on db error — callers fall back to scanning the JSONL files."""
    try:
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
