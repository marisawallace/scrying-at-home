"""Guard: the seven root entry-point shims must keep existing under their exact
filenames — shell aliases (setup.py) and installed Stop hooks (migrations 002/004,
which also discover the repo via these basenames as REPO_MARKER) bake these paths.
Each must stay a thin shim delegating to a package ``main()``. See
plans/module-refactor.md.
"""
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (root shim filename, package module that provides main)
ENTRY_SHIMS = [
    ("full_text_search_chats_archive.py", "scrying_at_home.search.engine"),
    ("view_conversation.py", "scrying_at_home.view.render"),
    ("sync_local_chats_archive.py", "scrying_at_home.sync.local_chats"),
    ("export_archive.py", "scrying_at_home.cli.export"),
    ("setup.py", "scrying_at_home.cli.setup"),
    ("claude_code_hook.py", "scrying_at_home.sync.claude_code_hook"),
    ("codex_sync.py", "scrying_at_home.sync.codex_sync"),
]


@pytest.mark.parametrize("filename,module", ENTRY_SHIMS)
def test_entry_shim_exists_and_delegates(filename, module):
    shim = REPO_ROOT / filename
    assert shim.is_file(), f"entry shim {filename} must stay at the repo root"
    text = shim.read_text()
    assert module in text, f"{filename} must delegate to {module}"
    mod = importlib.import_module(module)
    assert callable(getattr(mod, "main", None)), f"{module}.main() must exist"


def test_root_holds_only_shims_and_bootstrap():
    """The repo root holds the 7 entry shims + vendor_loader, nothing else .py."""
    allowed = {f for f, _ in ENTRY_SHIMS} | {"vendor_loader.py"}
    actual = {p.name for p in REPO_ROOT.glob("*.py")}
    assert actual == allowed, f"unexpected root .py files: {actual ^ allowed}"
