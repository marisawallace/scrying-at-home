"""
Integration tests for search workflow (full_text_search_chats_archive.py).

These tests exercise the search functionality with real data.
"""
import json
import shutil

import pytest


@pytest.mark.integration
def test_search_exact_phrase_match(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test searching for an exact phrase in conversations."""
    # Setup: Import conversations first
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0, "Setup sync failed"

    # Execute: Search for phrase that exists in test data
    result = run_cli(
        "full_text_search_chats_archive.py", "Python function",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nSearch STDOUT:\n{result.stdout}")
    print(f"\nSearch STDERR:\n{result.stderr}")

    # Verify: Search succeeded
    assert result.returncode == 0, f"Search failed: {result.stderr}"

    # Verify: Output contains expected conversation
    assert "Test Conversation 1" in result.stdout, "Expected conversation not in results"
    assert "Python" in result.stdout, "Search term not highlighted in results"


@pytest.mark.integration
def test_search_json_output(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test search with JSON output format."""
    # Setup: Import conversations
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0

    # Execute: Search with JSON output
    result = run_cli(
        "full_text_search_chats_archive.py", "integration testing", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nJSON Search STDOUT:\n{result.stdout}")

    assert result.returncode == 0, f"Search failed: {result.stderr}"

    # Verify: Output is valid JSON
    try:
        search_results = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"Output is not valid JSON: {e}\nOutput: {result.stdout}")

    # Verify: Results have expected structure
    assert isinstance(search_results, list), "JSON output should be a list"
    assert len(search_results) > 0, "Should have at least one result"

    # Verify: Each result has required fields
    first_result = search_results[0]
    assert "uuid" in first_result
    assert "name" in first_result
    assert "total_score" in first_result  # Field is actually called total_score
    assert "matches" in first_result
    assert "type" in first_result  # Field is actually called type (not provider)

    # Verify: Correct conversation found
    result_uuids = {r["uuid"] for r in search_results}
    assert "conv-uuid-002" in result_uuids, "Integration Testing conversation should be found"


@pytest.mark.integration
def test_search_cross_provider(isolated_workspace, sample_claude_export, sample_chatgpt_export,
                                test_env_file, run_cli):
    """Test searching across both Claude and ChatGPT conversations."""
    # Setup: Import both Claude and ChatGPT conversations
    claude_zip = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, claude_zip)

    chatgpt_zip = isolated_workspace / sample_chatgpt_export.name
    shutil.copy(sample_chatgpt_export, chatgpt_zip)

    # Sync Claude
    sync_claude = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_claude.returncode == 0

    # Sync ChatGPT
    sync_chatgpt = run_cli(
        "sync_local_chats_archive.py", "--chatgpt",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_chatgpt.returncode == 0

    # Execute: Search for term that appears in ChatGPT conversation
    result = run_cli(
        "full_text_search_chats_archive.py", "ChatGPT", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nCross-provider search:\n{result.stdout}")

    assert result.returncode == 0

    # Verify: Results include ChatGPT conversation
    search_results = json.loads(result.stdout)
    # Note: Search results include a filepath which contains the provider name
    # Extract provider from filepath or check the type field
    providers = set()
    for r in search_results:
        filepath = r.get("filepath", "")
        if "/claude/" in filepath:
            providers.add("claude")
        elif "/chatgpt/" in filepath:
            providers.add("chatgpt")

    assert "chatgpt" in providers, "ChatGPT results should be included"

    # Verify: Can find ChatGPT conversation
    chatgpt_results = [r for r in search_results if "/chatgpt/" in r.get("filepath", "")]
    assert len(chatgpt_results) > 0, "Should find ChatGPT conversations"


@pytest.mark.integration
def test_search_finds_chatgpt_message_body(isolated_workspace, sample_chatgpt_export,
                                            test_env_file, run_cli):
    """Search must index ChatGPT message bodies (mapping format), not just titles.

    Regression: ChatGPT exports use a `mapping` of nodes with
    message.content.parts; the original extractor only walked Claude's
    chat_messages array, so message text was invisible to search.
    """
    chatgpt_zip = isolated_workspace / sample_chatgpt_export.name
    shutil.copy(sample_chatgpt_export, chatgpt_zip)

    sync_chatgpt = run_cli(
        "sync_local_chats_archive.py", "--chatgpt",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_chatgpt.returncode == 0, sync_chatgpt.stderr

    # "trained by OpenAI" appears only inside the assistant message parts —
    # NOT in the title. If the extractor ignores `mapping`, this finds nothing.
    result = run_cli(
        "full_text_search_chats_archive.py", "-e", "trained by OpenAI", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert result.returncode == 0, result.stderr

    search_results = json.loads(result.stdout)
    chatgpt_hits = [r for r in search_results if "/chatgpt/" in r.get("filepath", "")]
    assert len(chatgpt_hits) > 0, (
        "Expected to find the phrase inside a ChatGPT message body. "
        f"Got: {search_results}"
    )


@pytest.mark.integration
def test_search_no_results(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test search with query that has no matches."""
    # Setup: Import conversations
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0

    # Execute: Search for non-existent term
    result = run_cli(
        "full_text_search_chats_archive.py", "xyzabc123nonexistentterm",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nNo results search:\n{result.stdout}")

    # Verify: Returns 0 (success) even with no results
    assert result.returncode == 0

    # Verify: Output indicates no results
    assert "0 results" in result.stdout.lower() or "no results" in result.stdout.lower() or result.stdout.strip() == ""


@pytest.mark.integration
def test_search_multi_term_default(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test that default search finds conversations containing all words individually (AND logic)."""
    # Setup: Import conversations first
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0, "Setup sync failed"

    # "write" and "keyword" both appear in conv-uuid-001 but NOT as a contiguous phrase
    # "write" is in "How do I write a Python function?"
    # "keyword" is in "Here's how to write a Python function with def keyword."
    result = run_cli(
        "full_text_search_chats_archive.py", "write keyword",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nMulti-term search STDOUT:\n{result.stdout}")
    print(f"\nMulti-term search STDERR:\n{result.stderr}")

    assert result.returncode == 0, f"Search failed: {result.stderr}"
    assert "Test Conversation 1" in result.stdout, "Expected conversation not found with multi-term default search"


@pytest.mark.integration
def test_search_exact_flag(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test that -e/--exact flag restores exact-phrase behavior."""
    # Setup: Import conversations first
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0, "Setup sync failed"

    # With -e, "write keyword" should NOT match (not a contiguous phrase)
    result_no_match = run_cli(
        "full_text_search_chats_archive.py", "-e", "write keyword",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nExact non-match STDOUT:\n{result_no_match.stdout}")

    assert result_no_match.returncode == 0, f"Search failed: {result_no_match.stderr}"
    assert "Test Conversation 1" not in result_no_match.stdout, \
        "Exact search should not find conversation when phrase is non-contiguous"

    # With -e, "Python function" SHOULD match (it IS a contiguous phrase)
    result_match = run_cli(
        "full_text_search_chats_archive.py", "-e", "Python function",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nExact match STDOUT:\n{result_match.stdout}")

    assert result_match.returncode == 0, f"Search failed: {result_match.stderr}"
    assert "Test Conversation 1" in result_match.stdout, \
        "Exact search should find conversation when phrase exists verbatim"


@pytest.mark.integration
def test_search_no_query_browses_all_newest_first(isolated_workspace, sample_claude_export,
                                                  run_cli, test_env_file):
    """With no query, every item is returned ordered by updated_at descending."""
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0

    # No positional query — browse mode.
    result = run_cli(
        "full_text_search_chats_archive.py", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert result.returncode == 0, f"Browse failed: {result.stderr}"

    search_results = json.loads(result.stdout)

    # All three items in the fixture (2 conversations + 1 project) appear.
    names = {r["name"] for r in search_results}
    assert {"Test Conversation 1", "Integration Testing Discussion", "Test Project"} <= names

    # Ordered most-recent-first by updated_at.
    updated = [r["updated_at"] for r in search_results]
    assert updated == sorted(updated, reverse=True), "Browse results should be newest-first"

    # Each item carries exactly one preview match scored 0.
    for r in search_results:
        assert r["match_count"] == 1
        assert r["matches"][0]["score"] == 0.0


@pytest.mark.integration
def test_search_uuid_lookup_single_and_multiple(isolated_workspace, sample_claude_export,
                                                run_cli, test_env_file):
    """--uuid looks conversations up directly by id: one uuid yields just that
    item, a comma-separated list yields exactly that set, and an unknown uuid
    yields nothing."""
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0, "Setup sync failed"

    # Single uuid -> exactly that one conversation, no query needed.
    single = run_cli(
        "full_text_search_chats_archive.py", "--uuid", "conv-uuid-002", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert single.returncode == 0, single.stderr
    single_results = json.loads(single.stdout)
    assert [r["uuid"] for r in single_results] == ["conv-uuid-002"]

    # Comma-separated list -> exactly that set (order-independent), case-insensitive.
    multi = run_cli(
        "full_text_search_chats_archive.py", "--uuid", "CONV-UUID-001,conv-uuid-002", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert multi.returncode == 0, multi.stderr
    multi_results = json.loads(multi.stdout)
    assert {r["uuid"] for r in multi_results} == {"conv-uuid-001", "conv-uuid-002"}

    # Unknown uuid -> empty result set (renders "No results found" in the picker/list).
    miss = run_cli(
        "full_text_search_chats_archive.py", "--uuid", "no-such-uuid", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert miss.returncode == 0, miss.stderr
    assert json.loads(miss.stdout) == []


@pytest.mark.integration
def test_search_uuid_rejects_stats(isolated_workspace, run_cli, test_env_file):
    """--uuid and --stats are mutually exclusive: --stats reports over the whole
    archive, so narrowing it to specific conversations is rejected up front."""
    result = run_cli(
        "full_text_search_chats_archive.py", "--uuid", "conv-uuid-002", "--stats",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert result.returncode == 1
    assert "--uuid cannot be combined with --stats" in result.stderr


@pytest.mark.integration
def test_search_scoring_accuracy(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test that search scoring ranks results correctly."""
    # Setup: Import conversations
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    sync_result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert sync_result.returncode == 0

    # Execute: Search with JSON to get scores
    result = run_cli(
        "full_text_search_chats_archive.py", "integration testing", "-j",
        config=test_env_file, cwd=isolated_workspace,
    )

    assert result.returncode == 0

    search_results = json.loads(result.stdout)

    # Verify: Results are sorted by score (descending)
    scores = [r["total_score"] for r in search_results]
    assert scores == sorted(scores, reverse=True), "Results should be sorted by score descending"

    # Verify: Conversation with "integration testing" in title has highest score
    top_result = search_results[0]
    assert "Integration Testing" in top_result["name"], \
        "Conversation with search term in title should rank highest"
