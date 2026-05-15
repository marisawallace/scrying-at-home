"""
Pytest configuration and shared fixtures for integration tests.
"""
import json
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Any

import pytest


def pytest_addoption(parser):
    """Add custom command-line options."""
    parser.addoption(
        "--keep-workspaces",
        action="store_true",
        default=False,
        help="Keep test workspaces after tests complete (for inspection)"
    )


@pytest.fixture
def isolated_workspace(tmp_path, request):
    """
    Create an isolated test workspace with all required directories.

    Returns the path to the workspace and logs it to the terminal for inspection.

    Use --keep-workspaces flag to preserve workspaces after test completion.
    """
    keep_workspaces = request.config.getoption("--keep-workspaces")

    if keep_workspaces:
        # Use a persistent location when --keep-workspaces is set
        import tempfile
        import time
        persistent_base = Path("/tmp/pytest-workspaces")
        persistent_base.mkdir(exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        workspace = persistent_base / f"{request.node.name}-{timestamp}"
        workspace.mkdir(parents=True)
    else:
        # Use pytest's auto-cleanup tmp_path
        workspace = tmp_path / "workspace"
        workspace.mkdir()

    # Create standard directory structure
    (workspace / "data").mkdir()
    (workspace / "data" / "llm_data").mkdir()
    (workspace / "data" / "archived_exports").mkdir()
    (workspace / "data" / "local_views").mkdir()

    # Log workspace location for manual inspection
    print("\n" + "="*80)
    print(f"TEST WORKSPACE: {workspace}")
    print(f"Test: {request.node.name}")
    if keep_workspaces:
        print("⚠️  WORKSPACE WILL BE PRESERVED (--keep-workspaces flag is set)")
    print("="*80 + "\n")

    yield workspace

    # Log completion
    print("\n" + "-"*80)
    print(f"Test completed: {request.node.name}")
    if keep_workspaces:
        print(f"✓ Workspace preserved at: {workspace}")
    else:
        print(f"Workspace will be cleaned up: {workspace}")
    print("-"*80 + "\n")


@pytest.fixture
def fixtures_dir():
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_claude_export(fixtures_dir, tmp_path):
    """
    Create a valid Claude export zip file with sample conversations and projects.

    Returns the path to the zip file.
    """
    # Create sample data structure
    users_data = [
        {
            "uuid": "test-user-uuid-001",
            "email_address": "claude-test@example.com",
            "full_name": "Test User"
        }
    ]

    conversations_data = [
        {
            "uuid": "conv-uuid-001",
            "name": "Test Conversation 1",
            "summary": "A test conversation about Python programming",
            "created_at": "2025-01-01T10:00:00.000000Z",
            "updated_at": "2025-01-01T11:00:00.000000Z",
            "account": {
                "uuid": "test-user-uuid-001"
            },
            "chat_messages": [
                {
                    "uuid": "msg-uuid-001",
                    "text": "How do I write a Python function?",
                    "content": [],
                    "sender": "human",
                    "created_at": "2025-01-01T10:00:00.000000Z",
                    "attachments": [],
                    "files": []
                },
                {
                    "uuid": "msg-uuid-002",
                    "text": "Here's how to write a Python function with def keyword.",
                    "content": [],
                    "sender": "assistant",
                    "created_at": "2025-01-01T10:01:00.000000Z",
                    "attachments": [],
                    "files": []
                }
            ]
        },
        {
            "uuid": "conv-uuid-002",
            "name": "Integration Testing Discussion",
            "summary": "Discussion about integration testing strategies",
            "created_at": "2025-01-05T14:30:00.000000Z",
            "updated_at": "2025-01-05T15:45:00.000000Z",
            "account": {
                "uuid": "test-user-uuid-001"
            },
            "chat_messages": [
                {
                    "uuid": "msg-uuid-003",
                    "text": "What's the best approach for integration testing?",
                    "content": [],
                    "sender": "human",
                    "created_at": "2025-01-05T14:30:00.000000Z",
                    "attachments": [],
                    "files": []
                },
                {
                    "uuid": "msg-uuid-004",
                    "text": "Integration testing should use **isolated workspaces** and real filesystem operations.\n\n## Key points\n\n- isolated workspaces\n- real filesystem operations\n\n```python\ndef test_example():\n    assert True\n```",
                    "content": [],
                    "sender": "assistant",
                    "created_at": "2025-01-05T14:35:00.000000Z",
                    "attachments": [],
                    "files": []
                }
            ]
        }
    ]

    projects_data = [
        {
            "uuid": "proj-uuid-001",
            "name": "Test Project",
            "description": "A sample project for testing",
            "created_at": "2025-01-03T09:00:00.000000Z",
            "updated_at": "2025-01-03T09:30:00.000000Z",
            "creator": {
                "uuid": "test-user-uuid-001"
            },
            "docs": [
                {
                    "uuid": "doc-uuid-001",
                    "filename": "example.py",
                    "content": "def hello_world():\n    print('Hello, World!')\n",
                    "created_at": "2025-01-03T09:00:00.000000Z"
                }
            ]
        }
    ]

    # Create zip file
    zip_path = tmp_path / "data-2025-01-05.zip"
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("users.json", json.dumps(users_data, indent=2))
        zf.writestr("conversations.json", json.dumps(conversations_data, indent=2))
        zf.writestr("projects.json", json.dumps(projects_data, indent=2))

    return zip_path


@pytest.fixture
def sample_chatgpt_export(tmp_path):
    """
    Create a valid ChatGPT export zip file with sample conversations.

    Returns the path to the zip file.
    """
    # User data (new format)
    user_data = {
        "id": "user-chatgpt-test-001",
        "email": "chatgpt-test@example.com",
        "chatgpt_plus_user": False,
        "phone_number": None
    }

    # ChatGPT conversations in real export format (mapping of nodes; root has
    # message: None; each child carries message.content.parts).
    conversations_data = [
        {
            "id": "chatgpt-conv-uuid-001",
            "title": "ChatGPT Test Conversation",
            "create_time": 1704103200.0,  # 2025-01-01T10:00:00Z
            "update_time": 1704106800.0,  # 2025-01-01T11:00:00Z
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["a"],
                },
                "a": {
                    "id": "a",
                    "message": {
                        "id": "a",
                        "author": {"role": "user"},
                        "create_time": 1704103200.0,
                        "content": {"content_type": "text", "parts": ["What is ChatGPT?"]},
                    },
                    "parent": "root",
                    "children": ["b"],
                },
                "b": {
                    "id": "b",
                    "message": {
                        "id": "b",
                        "author": {"role": "assistant"},
                        "create_time": 1704103260.0,
                        "content": {
                            "content_type": "text",
                            "parts": ["ChatGPT is an AI assistant trained by OpenAI."],
                        },
                    },
                    "parent": "a",
                    "children": [],
                },
            },
        }
    ]

    # ChatGPT export has hex-based filename (64 hex chars + date + hex)
    zip_path = tmp_path / "a1b2c3d4e5f67890123456789012345678901234567890123456789012345678-2025-01-05-12-00-00-abcdef.zip"
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("user.json", json.dumps(user_data, indent=2))
        zf.writestr("conversations.json", json.dumps(conversations_data, indent=2))

    return zip_path


@pytest.fixture
def test_env_file(repo_root, request):
    """
    Temporarily replace the repo's .env file with a test version.

    This ensures the sync script looks for zip files in the test workspace
    instead of the user's real ZIP_SEARCH_DIR.

    This fixture requires that the test has an isolated_workspace fixture
    to know where to point ZIP_SEARCH_DIR.

    Returns the path to the test .env file.
    """
    repo_env = repo_root / ".env"
    backup_env = repo_root / ".env.backup"

    # Get the isolated_workspace from the test's fixtures
    workspace = request.getfixturevalue('isolated_workspace')

    # Backup existing .env if it exists
    if repo_env.exists():
        shutil.copy(repo_env, backup_env)

    # Create test .env in repo root with paths pointing to workspace
    env_content = f"""# Test configuration
ZIP_SEARCH_DIR={workspace}
DATA_DIR={workspace / "data" / "llm_data"}
ARCHIVED_EXPORTS_DIR={workspace / "data" / "archived_exports"}
LOCAL_VIEWS_DIR={workspace / "data" / "local_views"}
"""
    repo_env.write_text(env_content)

    yield repo_env

    # Restore original .env
    if backup_env.exists():
        shutil.move(backup_env, repo_env)
    else:
        repo_env.unlink(missing_ok=True)


@pytest.fixture
def prepopulated_archive(isolated_workspace):
    """
    Create a workspace with pre-existing conversation data for update testing.

    Returns the workspace path with existing data.
    """
    # Create existing conversation with known UUID
    conv_dir = isolated_workspace / "data/llm_data/claude/claude-test@example.com/conversations"
    conv_dir.mkdir(parents=True)

    old_conversation = {
        "uuid": "conv-uuid-001",  # Same UUID as sample_claude_export
        "name": "Old Version of Test Conversation",
        "summary": "This is the old version that should be replaced",
        "created_at": "2025-01-01T10:00:00.000000Z",
        "updated_at": "2025-01-01T10:30:00.000000Z",  # Earlier update time
        "account": {
            "uuid": "test-user-uuid-001"
        },
        "chat_messages": [
            {
                "uuid": "old-msg-uuid",
                "text": "This is old content",
                "content": [],
                "sender": "human",
                "created_at": "2025-01-01T10:00:00.000000Z",
                "attachments": [],
                "files": []
            }
        ]
    }

    old_conv_path = conv_dir / "2025-01-01_Old-Version-of-Test-Conversation.json"
    old_conv_path.write_text(json.dumps(old_conversation, indent=2))

    return isolated_workspace


@pytest.fixture
def repo_root():
    """Return the path to the repository root."""
    return Path(__file__).parent.parent
