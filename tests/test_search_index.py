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
        file_id=1, head_hash="h", head_len=100, tail_hash="t"):
    return si.IndexedFile(
        id=file_id, path=path, source=source, mtime_ns=mtime_ns,
        ctime_ns=mtime_ns if ctime_ns is None else ctime_ns, size=size,
        indexed_bytes=size if indexed_bytes is None else indexed_bytes,
        head_hash=head_hash, head_len=head_len, tail_hash=tail_hash,
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
    assert not plan.maybe_appended


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
    # mtime rewrite with a fresh ctime is caught and reparsed, not skipped.
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, ctime_ns=2, size=100)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, ctime_ns=1, size=100)],
    )
    assert ([f.path for f, _ in plan.maybe_appended] == ["/s.jsonl"]
            or [f.path for f, _ in plan.rewritten] == ["/s.jsonl"])


def test_diff_grown_jsonl_is_append_candidate():
    plan = si.diff_index(
        [_fs("/s.jsonl", source=si.SOURCE_CC, mtime_ns=2, size=200)],
        [_ix("/s.jsonl", source=si.SOURCE_CC, mtime_ns=1, size=100)],
    )
    assert [f.path for f, _ in plan.maybe_appended] == ["/s.jsonl"]
    assert not plan.rewritten


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
    assert [f.path for f, _ in plan.maybe_appended] == ["/s.jsonl"]


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
# merge_cc_item_meta
# ---------------------------------------------------------------------------

def _cc_meta(**kw):
    base = {
        "uuid": "", "item_type": "conversation", "name": "(untitled)",
        "created_at": "", "updated_at": "", "host": "h", "cwd": "",
        "git_branch": "", "preview": "", "has_preview": False,
    }
    base.update(kw)
    return base


def test_merge_fills_unset_fields_from_tail():
    existing = _cc_meta()
    tail = _cc_meta(uuid="s1", name="First prompt", created_at="t1",
                    updated_at="t2", cwd="/repo", git_branch="main",
                    preview="First prompt", has_preview=True)
    merged = si.merge_cc_item_meta(existing, tail)
    assert merged["uuid"] == "s1"
    assert merged["name"] == "First prompt"
    assert merged["created_at"] == "t1"
    assert merged["updated_at"] == "t2"
    assert merged["cwd"] == "/repo"
    assert merged["preview"] == "First prompt"


def test_merge_keeps_first_occurrence_fields():
    existing = _cc_meta(uuid="s1", name="Original", created_at="t1",
                        updated_at="t2", cwd="/repo", git_branch="main",
                        preview="p", has_preview=True)
    tail = _cc_meta(uuid="other", name="Later prompt", created_at="t8",
                    updated_at="t9", cwd="/elsewhere", git_branch="dev",
                    preview="q", has_preview=True)
    merged = si.merge_cc_item_meta(existing, tail)
    assert merged["uuid"] == "s1"
    assert merged["name"] == "Original"
    assert merged["created_at"] == "t1"
    assert merged["updated_at"] == "t9"  # only updated_at tracks the tail
    assert merged["cwd"] == "/repo"
    assert merged["preview"] == "p"


def test_merge_keeps_updated_at_when_tail_has_no_timestamp():
    existing = _cc_meta(updated_at="t2")
    merged = si.merge_cc_item_meta(existing, _cc_meta(updated_at=""))
    assert merged["updated_at"] == "t2"


# ---------------------------------------------------------------------------
# _read_complete_lines
# ---------------------------------------------------------------------------

def test_read_complete_lines_excludes_torn_tail(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a": 1}\n{"torn": ')
    lines, end, _head = si._read_complete_lines(str(f), 0)
    assert lines == ['{"a": 1}']
    assert end == len(b'{"a": 1}\n')  # torn tail stays unindexed


def test_read_complete_lines_resumes_from_offset(tmp_path):
    f = tmp_path / "s.jsonl"
    first = b'{"a": 1}\n'
    f.write_bytes(first + b'{"b": 2}\n')
    lines, end, _head = si._read_complete_lines(str(f), len(first))
    assert lines == ['{"b": 2}']
    assert end == f.stat().st_size


def test_read_complete_lines_only_splits_on_newline(tmp_path):
    # U+2028 (and \x85 etc.) are legal inside JSON strings; str.splitlines()
    # would break on them and shred the line into unparseable fragments.
    f = tmp_path / "s.jsonl"
    text_with_ls = "line\u2028separator"
    f.write_text(json.dumps({"text": text_with_ls}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    lines, _end, _head = si._read_complete_lines(str(f), 0)
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
# tail hash — append-vs-rewrite detection for the indexed region
# ---------------------------------------------------------------------------

def test_tail_hash_file_matches_bytes(tmp_path):
    region = b"x" * 3000
    f = tmp_path / "s.jsonl"
    f.write_bytes(region + b"unindexed torn tail")
    assert si._tail_hash(str(f), len(region)) == si._tail_hash_bytes(region)


def test_tail_hash_of_short_region(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b"short\n")
    assert si._tail_hash(str(f), 6) == si._tail_hash_bytes(b"short\n")
    assert si._tail_hash(str(f), 0) == si._tail_hash_bytes(b"")


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
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes, head_hash, head_len, tail_hash) "
        "VALUES (?, ?, 1, 1, 1, 1, '', 0, '')", (path, source))
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
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes, head_hash, head_len, tail_hash) "
        "VALUES ('/a.json', 'llm', 1, 1, 1, 1, '', 0, '')")
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


def test_lookup_uuid_roundtrip(tmp_path):
    conn = si.open_index(tmp_path / "index.db")
    cur = conn.execute(
        "INSERT INTO files(path, source, mtime_ns, ctime_ns, size, indexed_bytes, head_hash, head_len, tail_hash) "
        "VALUES ('/a.json', 'llm', 1, 1, 1, 1, '', 0, '')")
    si._insert_item_row(conn, cur.lastrowid, "claude", "me@example.com", {
        "uuid": "u-123", "item_type": "conversation", "name": "n",
        "created_at": "c", "updated_at": "u", "host": "", "cwd": "",
        "git_branch": "", "preview": "p", "has_preview": True,
    })
    assert si.lookup_uuid(conn, "u-123") == (Path("/a.json"), "claude")
    assert si.lookup_uuid(conn, "missing") is None
    conn.close()
