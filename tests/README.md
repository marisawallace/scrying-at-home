# Integration Tests

Robust automated integration tests for the claude-search project. These tests exercise end-to-end functionality with actual data on the filesystem.

## Setup

Install pytest if you haven't already:

```bash
# Option 1: Virtual environment (works on all platforms)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements-test.txt

# Option 2: System package manager
# Debian/Ubuntu: sudo apt install python3-pytest
# Fedora: sudo dnf install python3-pytest
# Arch: sudo pacman -S python-pytest
# macOS: brew install pytest
```

## Running Tests

### Run all integration tests

```bash
pytest
```

### Run specific test file

```bash
pytest tests/integration/test_sync_workflow.py
pytest tests/integration/test_search_workflow.py
pytest tests/integration/test_view_workflow.py
```

### Run specific test

```bash
pytest tests/integration/test_sync_workflow.py::test_fresh_claude_import
```

### Run with verbose output

```bash
pytest -v
```

### Run tests matching a pattern

```bash
pytest -k "claude"  # Run only tests with "claude" in the name
pytest -k "search"  # Run only search-related tests
```

## Test Workspace Inspection

Each test creates an isolated temporary workspace. The location is **printed to the terminal** when the test runs:

```
================================================================================
TEST WORKSPACE: /tmp/pytest-of-user/pytest-123/test_fresh_claude_import0/workspace
Test: test_fresh_claude_import
================================================================================
```

You can `cd` to this directory during or after a test run to inspect:
- Input data (zip files)
- Generated data structure (data/, archived_exports/, local_views/)
- Script outputs
- HTML/Markdown views (can open in browser!)

### Keeping Workspaces After Tests

**By default**, pytest automatically cleans up temporary directories after tests complete.

**To preserve workspaces for inspection** (e.g., to open HTML files in browser):

```bash
# Keep all test workspaces
pytest --keep-workspaces

# Keep workspace for specific test
pytest tests/integration/test_view_workflow.py::test_view_html_format --keep-workspaces -v
```

Preserved workspaces are stored in `/tmp/pytest-workspaces/` with timestamps:
```
/tmp/pytest-workspaces/test_view_html_format-20260107-173045/
```

**Alternative:** Use pytest's `--basetemp` option:
```bash
pytest --basetemp=/tmp/my-test-workspace
```

This keeps workspaces in a fixed location but overwrites them on each run.

## Test Structure

```
tests/
├── conftest.py                    # Shared pytest fixtures
├── fixtures/                      # Test data (programmatically generated)
│   └── README.md
└── integration/                   # Integration test suites
    ├── test_sync_workflow.py      # Tests for sync_local_chats_archive.py
    ├── test_search_workflow.py    # Tests for full_text_search_chats_archive.py
    └── test_view_workflow.py      # Tests for view_conversation.py
```

## Available Fixtures

Fixtures are defined in `conftest.py`:

- **`isolated_workspace`**: Clean temporary workspace with directory structure
- **`sample_claude_export`**: Valid Claude export zip with conversations and projects
- **`sample_chatgpt_export`**: Valid ChatGPT export zip
- **`prepopulated_archive`**: Workspace with existing conversations (for update testing)
- **`test_env_file`**: Writes a test `.env` *inside the workspace* and returns its path
- **`run_cli`**: Runs an entry-point script as a subprocess, pinned to a test config via
  `--config` (keyword-only, required). Always use this instead of calling `subprocess.run`
  directly, so the script never reads the real `repo_root/.env`.
- **`repo_root`**: Path to repository root
- **`fixtures_dir`**: Path to fixtures directory

> **Safety:** the entry-point scripts take `--config PATH`; tests point every subprocess at
> a workspace-local `.env` through `run_cli`. The real `repo_root/.env` is never read,
> written, copied, or moved by any test — an interrupted run leaves it untouched. Do not
> reintroduce `repo_root / ".env"` swaps in fixtures.

## Test Coverage

### Sync Workflow (`test_sync_workflow.py`)

- ✅ Fresh import into empty archive
- ✅ Updating existing conversations (UUID-based)
- ✅ ChatGPT import with .env configuration
- ✅ Filename collision handling
- ✅ Multiple sync idempotency

### Search Workflow (`test_search_workflow.py`)

- ✅ Exact phrase matching
- ✅ JSON output format
- ✅ Cross-provider search (Claude + ChatGPT)
- ✅ No results handling
- ✅ Score ranking accuracy

### View Workflow (`test_view_workflow.py`)

- ✅ Markdown format conversion
- ✅ HTML format conversion
- ✅ Nonexistent conversation error handling
- ✅ View caching behavior
- ✅ Project viewing

## Adding New Tests

1. Create test function in appropriate file:
   ```python
   @pytest.mark.integration
   def test_my_new_feature(isolated_workspace, sample_claude_export, run_cli, test_env_file):
       # Setup
       # ...

       # Execute (run_cli adds --config so the real repo .env is never touched)
       result = run_cli(
           "sync_local_chats_archive.py", "--claude",
           config=test_env_file, cwd=isolated_workspace,
       )

       # Verify
       assert result.returncode == 0
   ```

2. Use fixtures to set up test data
3. Run actual scripts through `run_cli(...)` (never raw `subprocess.run`) — forgetting
   `config=` raises `TypeError` immediately rather than silently reading the real `.env`
4. Verify filesystem state and outputs
5. Temporary workspace is automatically cleaned up

## Debugging Failed Tests

When a test fails:

1. Check the printed workspace path
2. `cd` to the workspace directory
3. Inspect the generated files
4. Re-run the script manually to reproduce
5. Check stdout/stderr printed in test output

Example:
```bash
cd /tmp/pytest-of-user/pytest-123/test_fresh_claude_import0/workspace
ls -la data/
cat data/claude/*/conversations/*.json
./sync_local_chats_archive.py --claude
```

## Best Practices

- Each test should be independent (no shared state)
- Use descriptive test names that explain what's being tested
- Print stdout/stderr in tests for debugging
- Test both success and failure cases
- Verify filesystem state, not just return codes
- Test with realistic data that mirrors actual usage
