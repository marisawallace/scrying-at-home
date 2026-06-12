"""
Integration tests for sync workflow (sync_local_chats_archive.py).

These tests exercise the full sync pipeline with real filesystem operations.
"""
import json
import shutil

import pytest


@pytest.mark.integration
def test_fresh_claude_import(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test importing Claude conversations into an empty archive."""
    # Setup: Copy export zip to workspace
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    # Execute: Run sync script
    result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )

    # Debug output
    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    # Verify: Script succeeded
    assert result.returncode == 0, f"Sync failed: {result.stderr}"

    # Verify: Directory structure created
    conv_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/conversations"
    proj_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/projects"
    assert conv_dir.exists(), "Conversations directory not created"
    assert proj_dir.exists(), "Projects directory not created"

    # Verify: Correct number of files
    conv_files = list(conv_dir.glob("*.json"))
    proj_files = list(proj_dir.glob("*.json"))
    assert len(conv_files) == 2, f"Expected 2 conversations, found {len(conv_files)}"
    assert len(proj_files) == 1, f"Expected 1 project, found {len(proj_files)}"

    # Verify: Conversation content is correct
    conv_1_path = conv_dir / "2025-01-01_Test-Conversation-1.json"
    conv_2_path = conv_dir / "2025-01-05_Integration-Testing-Discussion.json"

    assert conv_1_path.exists(), f"Expected file not found: {conv_1_path.name}"
    assert conv_2_path.exists(), f"Expected file not found: {conv_2_path.name}"

    conv_1 = json.loads(conv_1_path.read_text())
    assert conv_1["uuid"] == "conv-uuid-001"
    assert conv_1["name"] == "Test Conversation 1"
    assert len(conv_1["chat_messages"]) == 2
    assert conv_1["account"]["uuid"] == "test-user-uuid-001"

    # Verify: Project content is correct
    proj_path = proj_dir / "2025-01-03_Test-Project.json"
    assert proj_path.exists(), f"Expected project file not found: {proj_path.name}"

    proj = json.loads(proj_path.read_text())
    assert proj["uuid"] == "proj-uuid-001"
    assert len(proj["docs"]) == 1
    assert proj["docs"][0]["filename"] == "example.py"

    # Verify: Zip file was archived
    archived_zip = isolated_workspace / "data/archived_exports/claude/claude-test@example.com/data-2025-01-05.zip"
    assert archived_zip.exists(), "Zip file not archived"


@pytest.mark.integration
def test_conversation_update(prepopulated_archive, sample_claude_export, run_cli, test_env_file):
    """Test updating an existing conversation with same UUID."""
    # Setup: Workspace already has old version of conv-uuid-001
    workspace = prepopulated_archive

    # Verify old file exists
    conv_dir = workspace / "data/llm_data/claude/claude-test@example.com/conversations"
    old_files = list(conv_dir.glob("*.json"))
    assert len(old_files) == 1, "Prepopulated archive should have 1 conversation"

    old_conv = json.loads(old_files[0].read_text())
    assert old_conv["name"] == "Old Version of Test Conversation"

    # Setup: Copy new export zip
    zip_dest = workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    # Execute: Run sync script
    result = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=workspace,
    )

    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    assert result.returncode == 0, f"Sync failed: {result.stderr}"

    # Verify: Old file was deleted, new files created
    conv_files = list(conv_dir.glob("*.json"))
    assert len(conv_files) == 2, f"Expected 2 conversations after update, found {len(conv_files)}"

    # Verify: Old filename is gone
    old_filenames = [f.name for f in conv_files]
    assert "2025-01-01_Old-Version-of-Test-Conversation.json" not in old_filenames

    # Verify: New version has updated content
    new_conv_path = conv_dir / "2025-01-01_Test-Conversation-1.json"
    assert new_conv_path.exists(), "Updated conversation not found"

    new_conv = json.loads(new_conv_path.read_text())
    assert new_conv["uuid"] == "conv-uuid-001", "UUID should match"
    assert new_conv["name"] == "Test Conversation 1", "Name should be updated"
    assert new_conv["summary"] == "A test conversation about Python programming"


@pytest.mark.integration
def test_chatgpt_import(isolated_workspace, sample_chatgpt_export, test_env_file, run_cli):
    """Test importing ChatGPT conversations with .env configuration."""
    # Setup: Copy export zip
    zip_dest = isolated_workspace / sample_chatgpt_export.name
    shutil.copy(sample_chatgpt_export, zip_dest)

    # Verify .env exists
    assert test_env_file.exists(), ".env file not created"

    # Execute: Run sync script
    result = run_cli(
        "sync_local_chats_archive.py", "--chatgpt",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nSTDOUT:\n{result.stdout}")
    print(f"\nSTDERR:\n{result.stderr}")

    assert result.returncode == 0, f"Sync failed: {result.stderr}"

    # Verify: Directory structure created
    conv_dir = isolated_workspace / "data/llm_data/chatgpt/chatgpt-test@example.com/conversations"
    assert conv_dir.exists(), "ChatGPT conversations directory not created"

    # Verify: Conversation imported
    conv_files = list(conv_dir.glob("*.json"))
    assert len(conv_files) == 1, f"Expected 1 conversation, found {len(conv_files)}"

    # Verify: Content is correct
    conv = json.loads(conv_files[0].read_text())
    assert conv["uuid"] == "chatgpt-conv-uuid-001"
    assert conv["name"] == "ChatGPT Test Conversation"


@pytest.mark.integration
def test_filename_collision_handling(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test that filename collisions are resolved with numeric suffixes."""
    # Setup: Copy export zip
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    # Run sync once
    result1 = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )
    assert result1.returncode == 0

    # Manually create a duplicate conversation with same date/name but different UUID
    conv_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/conversations"
    duplicate_conv = {
        "uuid": "conv-uuid-999",  # Different UUID
        "name": "Test Conversation 1",  # Same name
        "summary": "Duplicate for collision testing",
        "created_at": "2025-01-01T10:00:00.000000Z",  # Same date
        "updated_at": "2025-01-01T12:00:00.000000Z",
        "account": {"uuid": "test-user-uuid-001"},
        "chat_messages": []
    }

    # Manually write it with same filename pattern
    duplicate_path = conv_dir / "2025-01-01_Test-Conversation-1-1.json"
    duplicate_path.write_text(json.dumps(duplicate_conv, indent=2))

    # Run sync again (should not overwrite the duplicate)
    result2 = run_cli(
        "sync_local_chats_archive.py", "--claude",
        config=test_env_file, cwd=isolated_workspace,
    )

    print(f"\nSecond sync STDOUT:\n{result2.stdout}")

    # Verify: Both files still exist
    conv_files = sorted(conv_dir.glob("2025-01-01_Test-Conversation-1*.json"))
    assert len(conv_files) >= 2, "Collision should result in numeric suffix"

    # Verify: Both UUIDs are present
    uuids = {json.loads(f.read_text())["uuid"] for f in conv_files if "Test-Conversation-1" in f.name}
    assert "conv-uuid-001" in uuids
    assert "conv-uuid-999" in uuids


@pytest.mark.integration
def test_multiple_syncs_idempotent(isolated_workspace, sample_claude_export, run_cli, test_env_file):
    """Test that running sync multiple times with same data is idempotent."""
    # Setup
    zip_dest = isolated_workspace / "data-2025-01-05.zip"
    shutil.copy(sample_claude_export, zip_dest)

    # Run sync three times
    for i in range(3):
        # Need to restore the zip file since it gets archived
        if i > 0:
            archived = isolated_workspace / "data/archived_exports/claude/claude-test@example.com/data-2025-01-05.zip"
            shutil.copy(archived, zip_dest)

        result = run_cli(
            "sync_local_chats_archive.py", "--claude",
            config=test_env_file, cwd=isolated_workspace,
        )
        assert result.returncode == 0, f"Sync {i+1} failed"

    # Verify: Still have exactly the right number of files
    conv_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/conversations"
    proj_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/projects"

    conv_files = list(conv_dir.glob("*.json"))
    proj_files = list(proj_dir.glob("*.json"))

    assert len(conv_files) == 2, f"Multiple syncs created duplicates: {len(conv_files)} conversations"
    assert len(proj_files) == 1, f"Multiple syncs created duplicates: {len(proj_files)} projects"

    # Verify: Content hasn't been corrupted
    conv = json.loads(conv_files[0].read_text())
    assert "uuid" in conv
    assert "chat_messages" in conv
    assert len(conv["chat_messages"]) > 0
