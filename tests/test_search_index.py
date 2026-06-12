"""
Unit tests for search_index.py pure functions and db helpers.
"""
import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import search_index as si


# ---------------------------------------------------------------------------
# build_fts_query
# ---------------------------------------------------------------------------

def test_fts_query_multi_word():
    assert si.build_fts_query("Machine Learning", exact=False) == '"machine" AND "learning"'


def test_fts_query_exact_phrase():
    assert si.build_fts_query("Machine Learning", exact=True) == '"machine learning"'


def test_fts_query_doubles_embedded_quotes():
    assert si.build_fts_query('say "hello"', exact=True) == '"say ""hello"""'
    assert si.build_fts_query('"quoted"', exact=False) == '"""quoted"""'


def test_fts_query_drops_short_words_but_keeps_long():
    # Words under 3 chars produce no trigrams; dropping them widens the
    # candidate set, which the rescoring pass then narrows.
    assert si.build_fts_query("go to production", exact=False) == '"production"'


def test_fts_query_unusable_when_all_words_short():
    assert si.build_fts_query("a b", exact=False) is None
    assert si.build_fts_query("ab", exact=True) is None


def test_fts_query_empty():
    assert si.build_fts_query("", exact=False) is None
    assert si.build_fts_query("   ", exact=False) is None


# ---------------------------------------------------------------------------
# render_progress_bar
# ---------------------------------------------------------------------------

def test_progress_bar_empty():
    assert si.render_progress_bar(0, 10, width=10) == "[----------] 0/10 (0%)"


def test_progress_bar_full():
    assert si.render_progress_bar(10, 10, width=10) == "[##########] 10/10 (100%)"


def test_progress_bar_half():
    assert si.render_progress_bar(5, 10, width=10) == "[#####-----] 5/10 (50%)"


def test_progress_bar_zero_total_reads_as_complete():
    assert si.render_progress_bar(0, 0, width=10) == "[##########] 0/0 (100%)"


def test_progress_bar_clamps_overshoot():
    assert si.render_progress_bar(15, 10, width=10) == "[##########] 15/10 (100%)"


# ---------------------------------------------------------------------------
# searchable_body / preview_from_texts
# ---------------------------------------------------------------------------

def test_searchable_body_joins_and_lowers():
    assert si.searchable_body(["Hello", "", "World"]) == "hello\nworld"


def test_preview_uses_first_nonempty_text():
    preview, has_preview = si.preview_from_texts(["", "First\ntext here", "Second"])
    assert preview == "First text here"
    assert has_preview


def test_preview_truncates_to_200_chars():
    preview, _ = si.preview_from_texts(["x" * 300])
    assert preview == "x" * 200 + "..."


def test_preview_absent_when_no_texts():
    assert si.preview_from_texts([]) == ("", False)
    assert si.preview_from_texts([""]) == ("", False)


# ---------------------------------------------------------------------------
# diff_index
# ---------------------------------------------------------------------------

def _fs(path, source=si.SOURCE_LLM, mtime_ns=1, ctime_ns=None, size=100, **kw):
    return si.FileStat(path=path, source=source, mtime_ns=mtime_ns,
                       ctime_ns=mtime_ns if ctime_ns is None else ctime_ns,
                       size=size, **kw)


def _ix(path, source=si.SOURCE_LLM, mtime_ns=1, ctime_ns=None, size=100, indexed_bytes=None,
        file_id=1):
    return si.IndexedFile(
        id=file_id, path=path, source=source, mtime_ns=mtime_ns,
        ctime_ns=mtime_ns if ctime_ns is None else ctime_ns, size=size,
        indexed_bytes=size if indexed_bytes is None else indexed_bytes,
    )


def test_diff_noop_when_unchanged():
    plan = si.diff_index([_fs("/a.json")], [_ix("/a.json")])
    assert plan.is_noop()


def test_diff_new_and_removed():
    plan = si.diff_index([_fs("/new.json")], [_ix("/old.json")])
    assert [f.path for f in plan.new] == ["/new.json"]
    assert [f.path for f in plan.removed] == ["/old.json"]


def test_diff_changed_json_is_rewritten():
    plan = si.diff_index([_fs("/a.json", mtime_ns=2)], [_ix("/a.json", mtime_ns=1)])
    assert [f.path for f, _ in plan.rewritten] == ["/a.json"]


def test_diff_ctime_only_change_is_rewritten():
    # mtime and size byte-identical, but ctime moved: a copy/sync restored the
    # source mtime while its write stamped a fresh ctime. Must reindex, not skip.
    plan = si.diff_index(
        [_fs("/a.json", mtime_ns=1, ctime_ns=2, size=100)],
        [_ix("/a.json", mtime_ns=1, ctime_ns=1, size=100)],
    )
    assert [f.path for f, _ in plan.rewritten] == ["/a.json"]


def test_diff_ctime_only_change_reindexes_jsonl():
    # Same guarantee for append-only transcripts: an unchanged-size, restored-
    # mtime rewrite with a fresh ctime is caught and reindexed, not skipped.
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, ctime_ns=2, size=100)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, ctime_ns=1, size=100)],
    )
    assert [f.path for f, _ in plan.rewritten] == ["/s.jsonl"]


def test_diff_grown_jsonl_is_rewritten():
    # No append fast path: a grown transcript re-indexes whole.
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, mtime_ns=2, size=200)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, size=100)],
    )
    assert [f.path for f, _ in plan.rewritten] == ["/s.jsonl"]


def test_diff_shrunk_jsonl_is_rewritten():
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, mtime_ns=2, size=50)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, size=100)],
    )
    assert [f.path for f, _ in plan.rewritten] == ["/s.jsonl"]


def test_diff_torn_tail_reparsed_despite_unchanged_stat():
    # Last run saw a partial trailing line: indexed_bytes < size with the
    # same stat. The tail must be revisited.
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, size=100)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, size=100, indexed_bytes=80)],
    )
    assert [f.path for f, _ in plan.rewritten] == ["/s.jsonl"]


def test_diff_fully_indexed_jsonl_is_noop():
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC)],
        [_ix("/s.jsonl", source=si.SOURCE_CC)],
    )
    assert plan.is_noop()


# ---------------------------------------------------------------------------
# make_llm_item_meta — parity with search_item()
# ---------------------------------------------------------------------------

def test_llm_meta_untitled_fallback_and_updated_from_last_message():
    data = {
        "uuid": "u1",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z",
        "chat_messages": [
            {"created_at": "2025-01-03T00:00:00Z"},
            {"created_at": "2025-01-04T00:00:00Z"},
        ],
    }
    meta = si.make_llm_item_meta(data, "conversation", ["hello"])
    assert meta["name"] == "(untitled)"
    assert meta["updated_at"] == "2025-01-04T00:00:00Z"  # last message wins
    assert meta["preview"] == "hello"
    assert meta["has_preview"]


def test_llm_meta_project_keeps_top_level_updated_at():
    data = {"uuid": "p1", "name": "Proj", "created_at": "c", "updated_at": "u"}
    meta = si.make_llm_item_meta(data, "project", [])
    assert meta["updated_at"] == "u"
    assert not meta["has_preview"]


# ---------------------------------------------------------------------------
# _read_complete_lines
# ---------------------------------------------------------------------------

def test_read_complete_lines_excludes_torn_tail(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a": 1}\n{"torn": ')
    lines, end = si._read_complete_lines(str(f))
    assert lines == ['{"a": 1}']
    assert end == len(b'{"a": 1}\n')  # torn tail stays unindexed


def test_read_complete_lines_reads_whole_file(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    lines, end = si._read_complete_lines(str(f))
    assert lines == ['{"a": 1}', '{"b": 2}']
    assert end == f.stat().st_size


def test_read_complete_lines_only_splits_on_newline(tmp_path):
    # U+2028 (and \x85 etc.) are legal inside JSON strings; str.splitlines()
    # would break on them and shred the line into unparseable fragments.
    f = tmp_path / "s.jsonl"
    text_with_ls = "line\u2028separator"
    f.write_text(json.dumps({"text": text_with_ls}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    lines, _end = si._read_complete_lines(str(f))
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == text_with_ls


# ---------------------------------------------------------------------------
# schema_identity — extractor source changes must invalidate the index
# ---------------------------------------------------------------------------

def test_schema_identity_is_deterministic_and_in_range():
    v = si.schema_identity()
    assert v == si.schema_identity()
    assert 0 < v <= 0x7FFFFFFF  # fits PRAGMA user_version; never 0 (= fresh db)


def test_schema_identity_changes_when_source_changes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        for name in si.SCHEMA_SOURCE_FILES:
            (d / name).write_text("def extract(): return ['original']\n")
    (b / "claude_code_parser.py").write_text("def extract(): return ['changed']\n")
    assert si.schema_identity(a) != si.schema_identity(b)


def test_open_index_rebuilds_when_identity_differs(tmp_path):
    # Simulates an index built by older extractor source: its stored
    # user_version no longer matches schema_identity(), forcing a rebuild.
    db = tmp_path / "index.db"
    conn = si.open_index(db)
    conn.execute("INSERT INTO fts(body) VALUES ('stale')")
    stale = si.schema_identity() ^ 1
    conn.execute(f"PRAGMA user_version = {stale}")
    conn.commit()
    conn.close()
    conn = si.open_index(db)
    assert conn.execute("SELECT count(*) FROM fts").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# open_index / candidate_paths superset behavior (real sqlite, tmp db)
# ---------------------------------------------------------------------------

def test_open_index_bootstraps_and_reopens(tmp_path):
    db = tmp_path / "idx" / "index.db"
    conn = si.open_index(db)
    assert conn is not None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == si.schema_identity()
    conn.close()
    conn = si.open_index(db)  # reopen existing
    assert conn is not None
    conn.close()


def test_open_index_rebuilds_corrupt_db(tmp_path):
    db = tmp_path / "index.db"
    db.write_text("this is not a sqlite database, not even close padding padding")
    conn = si.open_index(db)
    assert conn is not None
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    conn.close()


def test_open_index_rebuilds_on_schema_version_bump(tmp_path):
    db = tmp_path / "index.db"
    conn = si.open_index(db)
    conn.execute("INSERT INTO fts(body) VALUES ('stale')")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    conn = si.open_index(db)
    assert conn is not None
    assert conn.execute("SELECT count(*) FROM fts").fetchone()[0] == 0
    conn.close()


def _index_body(conn, path, body, source=si.SOURCE_LLM):
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes) "
        "VALUES (?, ?, 1, 1, 1, 1)", (path, source))
    si._insert_fts_segment(conn, cur.lastrowid, si.searchable_body([body]))


def test_candidates_match_substrings_case_insensitively(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_body(conn, "/a.json", "Discussing SQLite indexes today")
    _index_body(conn, "/b.json", "nothing relevant")
    q = si.build_fts_query("sqlite INDEX", exact=False)
    assert si.candidate_paths(conn, q, si.SOURCE_LLM) == {"/a.json"}
    conn.close()


def test_candidates_are_superset_for_cross_text_and(tmp_path):
    # Words in different texts of the same file: the scan path's per-text
    # AND would reject it, but the file-level index must still surface it
    # as a candidate (rescoring filters it out).
    conn = si.open_index(tmp_path / "index.db")
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes) "
        "VALUES ('/a.json', 'llm', 1, 1, 1, 1)")
    si._insert_fts_segment(conn, cur.lastrowid,
                           si.searchable_body(["alpha text", "bravo text"]))
    q = si.build_fts_query("alpha bravo", exact=False)
    assert si.candidate_paths(conn, q, si.SOURCE_LLM) == {"/a.json"}
    conn.close()


def test_candidates_respect_source_filter(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_body(conn, "/a.json", "needle here", source=si.SOURCE_LLM)
    _index_body(conn, "/b.jsonl", "needle here", source=si.SOURCE_CC)
    q = si.build_fts_query("needle", exact=False)
    assert si.candidate_paths(conn, q, si.SOURCE_LLM) == {"/a.json"}
    assert si.candidate_paths(conn, q, si.SOURCE_CC) == {"/b.jsonl"}
    conn.close()


def _index_full(conn, path, texts, source=si.SOURCE_LLM, name="n", name_raw="n",
                with_texts=True):
    """Insert a complete searchable file: files + fts + item (+ file_texts
    unless with_texts is False, to simulate a missing row)."""
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes) "
        "VALUES (?, ?, 1, 1, 1, 1)", (path, source))
    fid = cur.lastrowid
    si._insert_fts_segment(conn, fid, si.searchable_body(texts))
    if with_texts:
        si._insert_file_texts(conn, fid, texts)
    si._insert_item_row(conn, fid, "claude", "me@example.com", {
        "uuid": "u-" + path, "item_type": "conversation", "name": name,
        "name_raw": name_raw, "created_at": "c", "updated_at": "u", "host": "",
        "cwd": "", "git_branch": "", "preview": "p", "has_preview": True,
    })
    return fid


def test_candidate_rows_shape_and_texts(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_full(conn, "/a.json", ["needle in here", "second text"], name_raw="Title")
    q = si.build_fts_query("needle", exact=False)
    rows = si.candidate_rows(conn, q, si.SOURCE_LLM)
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == "/a.json"
    assert json.loads(row["texts"]) == ["needle in here", "second text"]
    assert row["name_raw"] == "Title"
    assert row["uuid"] == "u-/a.json"
    assert row["provider"] == "claude"
    conn.close()


def test_candidate_rows_missing_file_texts_surfaces_none(tmp_path):
    # LEFT JOIN: a file with no file_texts row appears with texts=None (the
    # rescore fallback signal), never dropped from the candidate list.
    conn = si.open_index(tmp_path / "index.db")
    _index_full(conn, "/a.json", ["needle here"], with_texts=False)
    q = si.build_fts_query("needle", exact=False)
    rows = si.candidate_rows(conn, q, si.SOURCE_LLM)
    assert len(rows) == 1
    assert rows[0]["texts"] is None
    conn.close()


def test_all_searchable_rows_includes_zero_text_file(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_full(conn, "/a.json", ["alpha"])
    _index_full(conn, "/b.json", [])  # zero texts: no fts row, but a row here
    rows = si.all_searchable_rows(conn, si.SOURCE_LLM)
    paths = {r["path"]: r for r in rows}
    assert set(paths) == {"/a.json", "/b.json"}
    assert json.loads(paths["/b.json"]["texts"]) == []
    conn.close()


def test_all_searchable_rows_respects_source(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_full(conn, "/a.json", ["x"], source=si.SOURCE_LLM)
    _index_full(conn, "/b.jsonl", ["y"], source=si.SOURCE_CC)
    assert {r["path"] for r in si.all_searchable_rows(conn, si.SOURCE_LLM)} == {"/a.json"}
    assert {r["path"] for r in si.all_searchable_rows(conn, si.SOURCE_CC)} == {"/b.jsonl"}
    conn.close()


def test_read_snapshot_commits_and_nests(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    _index_full(conn, "/a.json", ["needle"])
    conn.commit()  # close the implicit write transaction from the inserts
    # A fresh read snapshot opens and commits a transaction cleanly.
    assert not conn.in_transaction
    with si.read_snapshot(conn) as c:
        assert c.in_transaction
        rows = si.all_searchable_rows(conn, si.SOURCE_LLM)
        assert len(rows) == 1
    assert not conn.in_transaction
    # Nesting is harmless: an already-open transaction is left for the outer
    # caller to close.
    conn.execute("BEGIN")
    with si.read_snapshot(conn):
        assert conn.in_transaction
    assert conn.in_transaction  # inner helper did not close the outer txn
    conn.commit()
    conn.close()


def test_lookup_uuid_roundtrip(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes) "
        "VALUES ('/a.json', 'llm', 1, 1, 1, 1)")
    si._insert_item_row(conn, cur.lastrowid, "claude", "me@example.com", {
        "uuid": "u-123", "item_type": "conversation", "name": "n",
        "created_at": "c", "updated_at": "u", "host": "", "cwd": "",
        "git_branch": "", "preview": "p", "has_preview": True,
    })
    assert si.lookup_uuid(conn, "u-123") == (Path("/a.json"), "claude")
    assert si.lookup_uuid(conn, "missing") is None
    conn.close()


# ---------------------------------------------------------------------------
# refresh — concurrency hardening (Part B)
# ---------------------------------------------------------------------------

def test_refresh_integrity_error_does_not_delete_db(tmp_path, monkeypatch):
    # A racing process already inserted the new file's path row; our INSERT
    # trips the UNIQUE constraint. That is contention, never corruption: the
    # db must survive and refresh must return None (caller falls back to scan).
    db = tmp_path / "index.db"
    conn = si.open_index(db)

    cc_dir = tmp_path / "cc" / "-proj"
    cc_dir.mkdir(parents=True)
    session = cc_dir / "s.jsonl"
    session.write_text(json.dumps({
        "type": "user", "message": {"role": "user", "content": "hello"},
        "uuid": "u1", "timestamp": "2026-01-01T00:00:00.000Z",
        "sessionId": "s", "cwd": "/proj", "gitBranch": "main",
    }) + "\n")

    # Pre-insert a conflicting files.path row (as a racing process would).
    conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes) "
        "VALUES (?, 'claude-code', 999, 999, 999, 0)", (str(session),))
    conn.commit()
    # Force diff_index to plan a fresh INSERT for that same path despite the
    # row already existing, guaranteeing the UNIQUE collision in reconcile.
    monkeypatch.setattr(si, "load_indexed_files", lambda conn: [])

    result = si.refresh(conn, tmp_path / "no-llm", [("testhost", tmp_path / "cc")],
                        lambda data, item_type, provider: [],
                        lambda data, item_type, provider: "")
    assert result is None
    assert db.exists()
    conn.close()


# ---------------------------------------------------------------------------
# scan_disk — characterization tests
#
# These pin the classification of the os.scandir implementation. Behaviors
# preserved deliberately:
#   - dotfiles match ('.hidden.json' is picked up, matching Path.glob('*'))
#   - matching is case-sensitive ('X.JSON' / 'B.JSONL' are NOT picked up)
#   - any '*.jsonl' whose parent is a depth-1 dir is SOURCE_CC (incl. dirs
#     like 'subagents'); everything deeper or at the root is SOURCE_CC_TOOLS
#
# Symlink handling for the Claude Code walk is no-follow (security boundary):
#   - a symlinked project directory is NOT scanned (its files are absent), so
#     a real dir reachable only through a symlink is not double-indexed
#   - a symlinked '*.jsonl' file is skipped, even if its target is valid
#   - a file reachable only through a symlinked subdir is absent
#   - a broken symlink ending in .jsonl/.json is skipped
# The LLM (claude/chatgpt) walk still follows symlinks for user dirs and *.json
# files; a broken-symlink .json there is skipped on stat.
# ---------------------------------------------------------------------------


def _rec(fs, base):
    """Reduce a FileStat to its classification tuple plus a base-relative path,
    dropping stat fields that depend on the filesystem."""
    return (fs.source, str(Path(fs.path).relative_to(base)),
            fs.provider, fs.email, fs.item_type, fs.host)


def _build_corpus(tmp_path):
    data = tmp_path / "data"
    cc = tmp_path / "cc"

    # --- LLM tree ---
    conv = data / "claude" / "alice@example.com" / "conversations"
    proj = data / "claude" / "alice@example.com" / "projects"
    conv.mkdir(parents=True)
    proj.mkdir(parents=True)
    (conv / "c1.json").write_text("{}")
    (conv / ".hidden.json").write_text("{}")   # dotfile -> matched
    (conv / "X.JSON").write_text("{}")          # wrong case -> not matched
    (conv / "notes.txt").write_text("x")        # wrong suffix -> not matched
    (proj / "p1.json").write_text("{}")
    # a stray file (not a dir) at user level, and a .json at the wrong depth
    (data / "claude" / "stray.txt").write_text("x")
    (data / "claude" / "alice@example.com" / "loose.json").write_text("{}")  # wrong depth
    # second provider with only conversations
    cg = data / "chatgpt" / "bob@example.com" / "conversations"
    cg.mkdir(parents=True)
    (cg / "g1.json").write_text("{}")

    # --- Claude Code tree ---
    (cc / "proj1").mkdir(parents=True)
    (cc / "proj1" / "a.jsonl").write_text("")        # depth-2 -> SOURCE_CC
    (cc / "proj1" / ".hidden.jsonl").write_text("")  # dotfile -> SOURCE_CC
    (cc / "proj1" / "B.JSONL").write_text("")        # wrong case -> skipped
    (cc / "proj1" / "skip.txt").write_text("")       # wrong suffix -> skipped
    (cc / "root.jsonl").write_text("")               # root level -> CC_TOOLS
    (cc / "subagents").mkdir()
    (cc / "subagents" / "deep.jsonl").write_text("") # depth-2 -> SOURCE_CC
    (cc / "proj1" / "nested").mkdir()
    (cc / "proj1" / "nested" / "d3.jsonl").write_text("")  # depth-3 -> CC_TOOLS

    # real project dir -> scanned
    (cc / "realproj").mkdir()
    (cc / "realproj" / "s.jsonl").write_text("")
    # symlinked project dir -> NOT followed, its file must be ABSENT (no
    # double-index of realproj/s.jsonl under the symlink name)
    (cc / "linkproj").symlink_to("realproj", target_is_directory=True)

    # symlinked deeper dir below a project: its file must be ABSENT (not
    # followed, and below depth 1 so the depth-2 pass misses it too)
    (cc / "othersrc").mkdir()
    (cc / "othersrc" / "hidden.jsonl").write_text("")
    (cc / "proj1" / "linksub").symlink_to("../othersrc", target_is_directory=True)

    # symlinked *.jsonl file with a VALID target -> skipped (no-follow)
    (cc / "proj1" / "linkfile.jsonl").symlink_to("a.jsonl")

    # broken symlinks ending in the right suffix -> skipped
    (cc / "proj1" / "broken.jsonl").symlink_to("/nonexistent/x.jsonl")
    (conv / "broken.json").symlink_to("/nonexistent/y.json")

    return data, cc


def test_scan_disk_classification(tmp_path):
    data, cc = _build_corpus(tmp_path)
    stats = si.scan_disk(data, [("host1", cc)])

    got = {_rec(fs, tmp_path) for fs in stats}
    expected = {
        # LLM
        (si.SOURCE_LLM, "data/claude/alice@example.com/conversations/c1.json",
         "claude", "alice@example.com", "conversation", ""),
        (si.SOURCE_LLM, "data/claude/alice@example.com/conversations/.hidden.json",
         "claude", "alice@example.com", "conversation", ""),
        (si.SOURCE_LLM, "data/claude/alice@example.com/projects/p1.json",
         "claude", "alice@example.com", "project", ""),
        (si.SOURCE_LLM, "data/chatgpt/bob@example.com/conversations/g1.json",
         "chatgpt", "bob@example.com", "conversation", ""),
        # CC searchable (depth-2 real dirs; symlinked dirs are not followed)
        (si.SOURCE_CC, "cc/proj1/a.jsonl", "", "proj1", "", "host1"),
        (si.SOURCE_CC, "cc/proj1/.hidden.jsonl", "", "proj1", "", "host1"),
        (si.SOURCE_CC, "cc/subagents/deep.jsonl", "", "subagents", "", "host1"),
        (si.SOURCE_CC, "cc/realproj/s.jsonl", "", "realproj", "", "host1"),
        (si.SOURCE_CC, "cc/othersrc/hidden.jsonl", "", "othersrc", "", "host1"),
        # CC tools-only (root level + deeper than depth 2)
        (si.SOURCE_CC_TOOLS, "cc/root.jsonl", "", "", "", "host1"),
        (si.SOURCE_CC_TOOLS, "cc/proj1/nested/d3.jsonl", "", "", "", "host1"),
    }
    assert got == expected


def test_scan_disk_stat_fields_match_os_stat(tmp_path):
    import os
    data, cc = _build_corpus(tmp_path)
    stats = si.scan_disk(data, [("host1", cc)])
    for fs in stats:
        st = os.stat(fs.path)  # follows symlinks, like the scan
        assert fs.mtime_ns == st.st_mtime_ns
        assert fs.ctime_ns == st.st_ctime_ns
        assert fs.size == st.st_size


def test_scan_disk_missing_dirs_are_silent(tmp_path):
    # No data dir and no cc dir on disk -> empty result, no crash.
    stats = si.scan_disk(tmp_path / "nope", [("host1", tmp_path / "alsonope")])
    assert stats == []
