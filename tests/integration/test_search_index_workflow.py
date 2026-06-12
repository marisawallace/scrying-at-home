"""
Integration tests for the search index (search_index.py).

The index is a candidate filter: searching with it must produce stdout
byte-identical to a full scan (--no-index). These tests assert that
identity across query modes, then exercise staleness (modified, appended,
deleted, rewritten files) and corruption recovery.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(repo_root, workspace, script, *args):
    return subprocess.run(
        [sys.executable, str(repo_root / script), *args],
        cwd=workspace,
        capture_output=True,
        text=True,
    )


def _search(repo_root, workspace, *args):
    result = _run_cli(repo_root, workspace, "full_text_search_chats_archive.py", *args)
    assert result.returncode == 0, f"Search failed: {result.stderr}"
    return result.stdout


def _pop_scores(stdout):
    entries = json.loads(stdout)
    return [e.pop("total_score") for e in entries], entries


def _assert_index_matches_scan(repo_root, workspace, *args):
    """The core identity check: index-backed output == full-scan output.

    total_score embeds a recency boost computed from datetime.now(), so two
    runs differ in its far decimals; JSON output is compared with a score
    tolerance and everything else byte-exact.
    """
    with_index = _search(repo_root, workspace, *args)
    without_index = _search(repo_root, workspace, *args, "--no-index")
    if "-j" in args:
        index_scores, index_entries = _pop_scores(with_index)
        scan_scores, scan_entries = _pop_scores(without_index)
        assert index_entries == scan_entries, (
            f"index and scan results diverge for args {args!r}"
        )
        assert index_scores == pytest.approx(scan_scores, abs=1e-3), (
            f"index and scan scores diverge for args {args!r}"
        )
    else:
        assert with_index == without_index, (
            f"index and scan output diverge for args {args!r}"
        )
    return with_index


@pytest.fixture
def full_archive_workspace(isolated_workspace, sample_claude_export,
                           sample_chatgpt_export, repo_root):
    """Workspace with claude + chatgpt + claude-code data and an .env whose
    SEARCH_INDEX_DB points inside the workspace."""
    # Claude Code session data
    cc_dir = isolated_workspace / "claude_code_data"
    project_dir = cc_dir / "-home-testuser-projects-my-app"
    project_dir.mkdir(parents=True)
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_claude_code_session.jsonl"
    shutil.copy(fixture, project_dir / "cc-test-session-001.jsonl")

    # Subagent transcript: invisible to search, but its tool_use calls count
    # toward the --stats leaderboard (gather_cc_tool_counts rglobs them).
    subagent_dir = project_dir / "cc-test-session-001" / "subagents"
    subagent_dir.mkdir(parents=True)
    subagent_line = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Inspecting the flobnicator registry"},
            {"type": "tool_use", "name": "Bash", "id": "t1", "input": {}},
            {"type": "tool_use", "name": "Read", "id": "t2", "input": {}},
        ]},
        "uuid": "sa-msg-001", "timestamp": "2026-04-10T15:00:00.000Z",
        "sessionId": "agent-sub-001",
    }
    (subagent_dir / "agent-sub-001.jsonl").write_text(json.dumps(subagent_line) + "\n")

    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)
    repo_env.write_text(
        f"ZIP_SEARCH_DIR={isolated_workspace}\n"
        f"LLM_DATA_DIR={isolated_workspace / 'data' / 'llm_data'}\n"
        f"ARCHIVED_EXPORTS_DIR={isolated_workspace / 'data' / 'archived_exports'}\n"
        f"LOCAL_VIEWS_DIR={isolated_workspace / 'data' / 'local_views'}\n"
        f"SEARCH_INDEX_DB={isolated_workspace / 'search_index.db'}\n"
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}\n"
        f"CHATGPT_EMAIL=chatgpt-test@example.com\n"
    )

    # Sync both providers' export zips into the archive
    shutil.copy(sample_claude_export, isolated_workspace / sample_claude_export.name)
    shutil.copy(sample_chatgpt_export, isolated_workspace / sample_chatgpt_export.name)
    for flag in ("--claude", "--chatgpt"):
        sync = _run_cli(repo_root, isolated_workspace, "sync_local_chats_archive.py", flag)
        assert sync.returncode == 0, f"Setup sync {flag} failed: {sync.stderr}"

    yield isolated_workspace

    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Identity: index output == scan output
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_index_identical_to_scan_across_modes(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    # Multi-word AND query (claude data)
    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "Test Conversation 1" in out
    # Exact phrase
    out = _assert_index_matches_scan(repo_root, ws, "-e", "integration testing", "-j")
    assert "Integration Testing Discussion" in out
    # Cross-provider hit (chatgpt)
    out = _assert_index_matches_scan(repo_root, ws, "ChatGPT", "-j")
    assert "chatgpt-conv-uuid-001" in out
    # Claude Code source filter
    out = _assert_index_matches_scan(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")
    assert "cc-test-session-001" in out
    # All-short-words query: index can't serve it, both runs scan
    _assert_index_matches_scan(repo_root, ws, "a b", "-j")
    # Static list output (colors/highlighting path)
    _assert_index_matches_scan(repo_root, ws, "Python function", "-n")
    # Index db was actually created
    assert (ws / "search_index.db").exists()


@pytest.mark.integration
def test_browse_and_stats_identical_to_scan(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    out = _assert_index_matches_scan(repo_root, ws, "-j")  # browse mode
    entries = json.loads(out)
    # browse covers every item across all three providers
    assert {e["provider"] for e in entries} == {"claude", "chatgpt", "claude-code"}
    assert len(entries) == 5  # 2 claude convs + 1 project + 1 chatgpt + 1 cc

    out = _assert_index_matches_scan(repo_root, ws, "--stats")
    assert "claude-code" in out
    # Subagent transcript tool calls reach the leaderboard...
    assert "Bash" in out

    # ...but subagent transcript text never reaches search results
    out = _assert_index_matches_scan(repo_root, ws, "flobnicator", "-j")
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# Staleness: the index reconciles on every run
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_modified_json_is_reindexed(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    data = json.loads(conv_file.read_text())
    data["chat_messages"][0]["text"] = "Completely fresh xylophone content"
    conv_file.write_text(json.dumps(data, indent=2))

    out = _assert_index_matches_scan(repo_root, ws, "xylophone", "-j")
    assert "conv-uuid-001" in out
    # The replaced text is gone from the index too
    out = _search(repo_root, ws, "-e", "How do I write a Python function", "-j")
    assert "conv-uuid-001" not in out


@pytest.mark.integration
def test_appended_jsonl_line_is_found(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "virtual environment", "-j")  # build index

    session = ws / "claude_code_data/-home-testuser-projects-my-app/cc-test-session-001.jsonl"
    appended = {
        "type": "user",
        "message": {"role": "user", "content": "Now discuss the zanzibar deployment"},
        "uuid": "msg-appended", "timestamp": "2026-04-11T09:00:00.000Z",
        "cwd": "/home/testuser/projects/my-app",
        "sessionId": "cc-test-session-001", "gitBranch": "main",
    }
    with open(session, "a") as f:
        f.write(json.dumps(appended) + "\n")

    out = _assert_index_matches_scan(repo_root, ws, "zanzibar", "-j")
    assert "cc-test-session-001" in out

    # updated_at must track the appended (newer) timestamp in browse mode
    entries = json.loads(_assert_index_matches_scan(repo_root, ws, "-j", "-s", "claude-code"))
    assert entries[0]["updated_at"] == "2026-04-11T09:00:00.000Z"

    # Whole-file re-index, not segment accumulation: the appended file holds
    # exactly one fts_map row, not one-per-append.
    import sqlite3
    conn = sqlite3.connect(ws / "search_index.db")
    rows = conn.execute(
        "SELECT count(*) FROM fts_map m JOIN files f ON f.id = m.file_id "
        "WHERE f.path = ?", (str(session),),
    ).fetchone()[0]
    conn.close()
    assert rows == 1


@pytest.mark.integration
def test_deleted_file_drops_out_of_results(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    out = _search(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out

    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    next(conv_dir.glob("*Test-Conversation-1*")).unlink()

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" not in out


@pytest.mark.integration
def test_rewritten_jsonl_head_change_forces_full_reindex(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "virtual environment", "-j")  # build index

    # Rewrite the session from scratch with a different head and MORE bytes
    # than before: whole-file re-index drops the old rows and reparses.
    session = ws / "claude_code_data/-home-testuser-projects-my-app/cc-test-session-001.jsonl"
    original_size = session.stat().st_size
    lines = [
        {"type": "permission-mode", "permissionMode": "plan", "sessionId": "cc-test-session-001"},
        {"type": "user",
         "message": {"role": "user", "content": "Rebuilt transcript about quasar telemetry"},
         "uuid": "msg-new-001", "timestamp": "2026-05-01T10:00:00.000Z",
         "cwd": "/home/testuser/projects/my-app",
         "sessionId": "cc-test-session-001", "gitBranch": "main",
         "padding": "x" * max(0, original_size)},
    ]
    session.write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert session.stat().st_size > original_size

    out = _assert_index_matches_scan(repo_root, ws, "quasar telemetry", "-j")
    assert "cc-test-session-001" in out
    # Old content must be gone even though the file only ever grew
    out = _assert_index_matches_scan(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")
    assert "cc-test-session-001" not in out


@pytest.mark.integration
def test_rewritten_jsonl_middle_change_forces_full_reindex(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    session = ws / "claude_code_data/-home-testuser-projects-my-app/cc-test-session-001.jsonl"

    # Start from a transcript whose first line alone exceeds the 1KB head
    # window, so a later middle rewrite leaves the head hash unchanged.
    def line(uuid, content, extra=""):
        return json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content + extra},
            "uuid": uuid, "timestamp": "2026-04-10T12:00:00.000Z",
            "cwd": "/home/testuser/projects/my-app",
            "sessionId": "cc-test-session-001", "gitBranch": "main",
        }) + "\n"

    head_line = line("msg-head", "padding head line", "x" * 2000)
    session.write_text(head_line + line("msg-mid", "original middle topic"))
    _search(repo_root, ws, "virtual environment", "-j")  # build index

    # Rewrite: head line byte-identical, middle replaced, file grows — the
    # whole file is re-indexed regardless of which bytes changed.
    session.write_text(head_line + line("msg-mid", "replacement nebula topic", "y" * 100))

    out = _assert_index_matches_scan(repo_root, ws, "nebula", "-s", "claude-code", "-j")
    assert "cc-test-session-001" in out
    out = _assert_index_matches_scan(repo_root, ws, "original middle", "-s", "claude-code", "-j")
    assert "cc-test-session-001" not in out


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_corrupt_archive_file_warns_on_every_run_and_self_heals(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    original = conv_file.read_text()
    conv_file.write_text(original[: len(original) // 2])  # torn mid-write

    # Loud on every run, never tombstoned, and search still succeeds
    for _ in range(2):
        result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "xyzzy", "-j")
        assert result.returncode == 0
        assert str(conv_file) in result.stderr
        assert "could not be read and are missing from results" in result.stderr

    # Results stay identical to a scan while the file is corrupt
    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" not in out

    # Restoring the file heals the index with no manual intervention
    conv_file.write_text(original)
    result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "Python function", "-j")
    assert result.returncode == 0
    assert "missing from results" not in result.stderr
    assert "conv-uuid-001" in result.stdout


@pytest.mark.integration
def test_wrong_shape_json_warns_instead_of_crashing(full_archive_workspace, repo_root):
    # Valid JSON missing required keys (uuid/created_at): both the indexed
    # and scan paths must warn and skip, not crash.
    ws = full_archive_workspace
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    data = json.loads(conv_file.read_text())
    del data["uuid"]
    conv_file.write_text(json.dumps(data))

    for extra in ([], ["--no-index"]):
        result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py",
                          "Python function", "-j", *extra)
        assert result.returncode == 0, result.stderr
        assert "conv-uuid-001" not in result.stdout

@pytest.mark.integration
def test_missing_file_texts_row_rescued_by_scan_fallback(full_archive_workspace, repo_root):
    # Stored texts are the source of truth for scores — but a missing
    # file_texts row must never score a file as empty: it falls back to the
    # real file. Delete one row out-of-band, then assert results stay
    # identical to a scan.
    import sqlite3
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conn = sqlite3.connect(ws / "search_index.db")
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(conv_file),)).fetchone()[0]
    conn.execute("DELETE FROM file_texts WHERE file_id = ?", (fid,))
    conn.commit()
    conn.close()

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out


@pytest.mark.integration
def test_corrupt_file_texts_rescued_by_scan_fallback(full_archive_workspace, repo_root):
    # Non-JSON in file_texts.texts must trigger the same per-file scan rescue
    # as a missing row, not score the file as empty.
    import sqlite3
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conn = sqlite3.connect(ws / "search_index.db")
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(conv_file),)).fetchone()[0]
    conn.execute("UPDATE file_texts SET texts = ? WHERE file_id = ?", ("{not json", fid))
    conn.commit()
    conn.close()

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out


@pytest.mark.integration
def test_zero_text_file_not_treated_as_missing(full_archive_workspace, repo_root):
    # A searchable file that extracts to zero texts gets a `[]` file_texts row,
    # not a missing one: it never matches and never triggers a fallback scan,
    # and its presence keeps results identical to a scan.
    import sqlite3
    ws = full_archive_workspace
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    # No name, no summary, no messages -> extract_text_from_conversation == []
    empty = conv_dir / "empty-conv.json"
    empty.write_text(json.dumps({
        "uuid": "empty-conv-uuid", "created_at": "2026-01-01T00:00:00Z",
        "chat_messages": [],
    }))

    _assert_index_matches_scan(repo_root, ws, "Python function", "-j")

    # The zero-text file is stored as an empty-array row, distinct from missing.
    conn = sqlite3.connect(ws / "search_index.db")
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(empty),)).fetchone()[0]
    stored = conn.execute("SELECT texts FROM file_texts WHERE file_id = ?", (fid,)).fetchone()
    conn.close()
    assert stored is not None and json.loads(stored[0]) == []


@pytest.mark.integration
def test_short_word_query_uses_index_and_matches_scan(full_archive_workspace, repo_root):
    # Sub-3-char words can't use the trigram index; the index path serves them
    # via all_searchable_rows instead of a filesystem scan, and the results
    # must still be identical. "Hi" appears in the chatgpt fixture.
    ws = full_archive_workspace
    _assert_index_matches_scan(repo_root, ws, "Hi", "-j")


@pytest.mark.integration
def test_invalid_utf8_jsonl_skipped_and_warned_on_both_paths(full_archive_workspace, repo_root):
    # A JSONL with an invalid UTF-8 byte must be skipped identically by the
    # index path (strict decode in _read_complete_lines) and the scan path
    # (strict-UTF-8 open in claude_code_parser.parse_jsonl): no rows indexed,
    # the file contributes nothing to results, and both warn.
    ws = full_archive_workspace
    bad = ws / "claude_code_data/-home-testuser-projects-my-app/cc-bad-utf8.jsonl"
    line = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "zeppelin SEARCHMARKER content"},
        "uuid": "msg-badutf8", "timestamp": "2026-04-12T09:00:00.000Z",
        "cwd": "/home/testuser/projects/my-app",
        "sessionId": "cc-bad-utf8", "gitBranch": "main",
    })
    bad.write_bytes(line.encode("utf-8") + b"\xff\xfe invalid\n")

    for extra in ([], ["--no-index"]):
        result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py",
                          "SEARCHMARKER", "-j", *extra)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []
        assert "cc-bad-utf8.jsonl" in result.stderr


@pytest.mark.integration
def test_null_name_conversation_does_not_disable_index(full_archive_workspace, repo_root):
    # An export with "name": null must not crash indexing: name_raw is NOT NULL,
    # so a None there raises IntegrityError, which refresh() misreads as write
    # contention and silently rolls back the whole index every run. Results must
    # stay identical to a scan, and the file must be searchable.
    ws = full_archive_workspace
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    nullnamed = conv_dir / "null-name-conv.json"
    nullnamed.write_text(json.dumps({
        "uuid": "null-name-uuid", "created_at": "2026-01-01T00:00:00Z",
        "name": None,
        "chat_messages": [
            {"sender": "human", "text": "a NULLNAMEMARKER question", "created_at": "2026-01-01T00:00:01Z"},
        ],
    }))

    out = _assert_index_matches_scan(repo_root, ws, "NULLNAMEMARKER", "-j")
    assert "null-name-uuid" in out
    # The index must have indexed it (no silent contention rollback to scan).
    result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "NULLNAMEMARKER", "-j")
    assert "search index busy" not in result.stderr


@pytest.mark.integration
def test_torn_trailing_invalid_utf8_skips_whole_file_on_both_paths(full_archive_workspace, repo_root):
    # Invalid UTF-8 in a torn trailing line (no terminating newline, e.g. a
    # session flushed mid-multibyte-char) must drop the WHOLE file on both
    # paths, not just the index path's complete-line prefix. The scan path's
    # whole-file strict-UTF-8 open fails it; the index path must match.
    ws = full_archive_workspace
    bad = ws / "claude_code_data/-home-testuser-projects-my-app/cc-torn-utf8.jsonl"
    good_line = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "zeppelin TORNMARKER content"},
        "uuid": "msg-torn", "timestamp": "2026-04-12T09:00:00.000Z",
        "cwd": "/home/testuser/projects/my-app",
        "sessionId": "cc-torn-utf8", "gitBranch": "main",
    })
    # Complete line + newline, then a torn trailing line ending mid-char (bad,
    # no trailing newline) — exactly the active-mid-write scenario.
    bad.write_bytes(good_line.encode("utf-8") + b"\n" + b'{"partial": "\xff')

    for extra in ([], ["--no-index"]):
        result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py",
                          "TORNMARKER", "-j", *extra)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []
        assert "cc-torn-utf8.jsonl" in result.stderr

    # --verify must not spuriously fail on this corpus.
    verify = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "TORNMARKER", "--verify")
    assert verify.returncode == 0, verify.stderr


@pytest.mark.integration
def test_non_list_file_texts_rescued_by_scan_fallback(full_archive_workspace, repo_root):
    # Valid JSON that isn't an array (a bare string/dict) must trigger the same
    # per-file scan rescue as a parse error — iterating it in
    # find_matches_in_texts would silently mis-score over characters/keys.
    import sqlite3
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conn = sqlite3.connect(ws / "search_index.db")
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(conv_file),)).fetchone()[0]
    conn.execute("UPDATE file_texts SET texts = ? WHERE file_id = ?",
                 (json.dumps("a bare string, not a list"), fid))
    conn.commit()
    conn.close()

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out


@pytest.mark.integration
def test_renamed_host_reflected_without_reindex(full_archive_workspace, repo_root):
    # The cc scan path reads host from current config; the index froze it at
    # index time. Renaming a host in .env without touching any data file must
    # still surface the NEW host (resolved from path at read time), or the index
    # path diverges from the scan and breaks --here filtering.
    ws = full_archive_workspace
    _search(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")  # build index

    cc_dir = ws / "claude_code_data"
    env_path = repo_root / ".env"
    env = env_path.read_text()
    assert f"CLAUDE_CODE_SOURCES=testhost={cc_dir}" in env
    env_path.write_text(env.replace(
        f"CLAUDE_CODE_SOURCES=testhost={cc_dir}",
        f"CLAUDE_CODE_SOURCES=renamedhost={cc_dir}"))

    out = _assert_index_matches_scan(repo_root, ws, "virtual environment", "-s", "claude-code", "-j")
    entries = json.loads(out)
    assert entries and all(e["extra"]["host"] == "renamedhost" for e in entries)


@pytest.mark.integration
def test_verify_passes_on_healthy_corpus(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "Python function", "--verify")
    assert result.returncode == 0, result.stderr
    assert "VERIFY OK" in result.stdout


@pytest.mark.integration
def test_verify_detects_tampered_stored_texts(full_archive_workspace, repo_root):
    # --verify is only meaningful if it actually catches divergence: tamper a
    # file_texts row so the index path scores different text than the scan path.
    import sqlite3
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    conn = sqlite3.connect(ws / "search_index.db")
    conv_dir = ws / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_file = next(conv_dir.glob("*Test-Conversation-1*"))
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(conv_file),)).fetchone()[0]
    # Valid JSON, but different text than the file on disk holds.
    conn.execute("UPDATE file_texts SET texts = ? WHERE file_id = ?",
                 (json.dumps(["totally different wumpus text"]), fid))
    conn.commit()
    conn.close()

    result = _run_cli(repo_root, ws, "full_text_search_chats_archive.py", "Python function", "--verify")
    assert result.returncode == 1
    assert "VERIFY FAILED" in result.stderr


@pytest.mark.integration
def test_corrupt_index_recovers_with_correct_results(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    db = ws / "search_index.db"
    db.write_text("garbage " * 1000)

    out = _assert_index_matches_scan(repo_root, ws, "Python function", "-j")
    assert "conv-uuid-001" in out


@pytest.mark.integration
def test_view_uuid_lookup_with_and_without_index(full_archive_workspace, repo_root):
    ws = full_archive_workspace
    _search(repo_root, ws, "Python function", "-j")  # build index

    result = _run_cli(repo_root, ws, "view_conversation.py", "conv-uuid-001", "--no-open")
    assert result.returncode == 0, result.stderr
    assert "Found:" in result.stdout

    # Without an index the scan fallback must still find it
    (ws / "search_index.db").unlink()
    result = _run_cli(repo_root, ws, "view_conversation.py", "conv-uuid-001", "--no-open")
    assert result.returncode == 0, result.stderr
    assert "Found:" in result.stdout
