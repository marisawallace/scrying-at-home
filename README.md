![Demo gif showing searching in the terminal](demo.gif)

I wanted offline full-text search and ownership over *all* my LLM chats. So I made this.

Terminal UI, hit enter to directly resume a chat. Will open your browser or `cd` and `claude --resume`.

**Status**: core features stable, actively in use and under development. If you're using this, please tell me!

## Features

- **Multi-provider**: Claude, ChatGPT, Claude Code.
- **Multi-account** per provider
- **Made for cloud sync**: put `clauding-at-home/data/` in Dropbox/MEGA/etc. Search your full LLM history on all your machines.
- **Multi-host** for Claude Code. `laptop` and `desktop` chats retain separate host paths. Sync & search everything on every device.
- **Smart search ranking**
- **Local view**: copy chats to Markdown or HTML, open in `$EDITOR`
- **Bulk export**: `export_archive.py` dumps your whole archive to a dated tree of Markdown files
- **Non-destructive**: preserves a chat even if you deleted it on the website. Export/sync the last 30 days only and it'll preserve your older chats. 
- **Export backup**: automatic archive of your data export zipfiles
- **UUID tracking**: Correctly handles conversation renames
- **Simple**: just a folder of python scripts. Works with system python.
- **Completely offline**

## Setup

```
git clone git@github.com:marisawallace/clauding-at-home.git
cd clauding-at-home
chmod +x sync_local_chats_archive.py
chmod +x full_text_search_chats_archive.py
chmod +x view_conversation.py

cp .env.example .env
# Then read and edit .env -- all options are explained!

# Claude Code setup -- more on this below:
python3 migrations/002_setup_claude_code_archival.py
```

I highly recommend adding aliases to your `.bashrc` or equivalent:

```
alias ccs="cd $CODE_HOME/clauding-at-home/"
alias cs="python3 $CODE_HOME/clauding-at-home/full_text_search_chats_archive.py"
alias csscl="python3 $CODE_HOME/clauding-at-home/sync_local_chats_archive.py --claude"
alias cssch="python3 $CODE_HOME/clauding-at-home/sync_local_chats_archive.py --chatgpt"
alias csv="python3 $CODE_HOME/clauding-at-home/view_conversation.py"
alias csvh="python3 $CODE_HOME/clauding-at-home/view_conversation.py --format html"
```

Make sure you have $EDITOR set.

```
export VISUAL=code
export EDITOR="$VISUAL"
```

### Export Your Chats

#### Claude.ai
1. [https://claude.ai/settings/data-privacy-controls](https://claude.ai/settings/data-privacy-controls)
2. Click "Export data"
3. Download the .zip file
4. Run `your-alias`, `csscl`, or `python3 sync_local_chats_archive.py --claude`

#### ChatGPT
1. [https://chatgpt.com/#settings/DataControls](https://chatgpt.com/#settings/DataControls)
2. Click "Export data"
3. Download the .zip file
4. Run `your-alias`, `cssch`, or `python3 sync_local_chats_archive.py --chatgpt`

The sync script will:
- Find all export zip files matching the provider's pattern
- Extract and organize conversations/projects by provider and user email
- Update existing conversations (matched by UUID)
- Preserve locally archived chats that were deleted from the provider
- Handle duplicate filenames with numeric suffixes
- Move processed zip files to `data/archived_exports/{provider}/`

The sync script includes multiple safety mechanisms:

- **Dual UUID verification**: Matches both conversation UUID and account UUID before updates
- **Cross-account protection**: Won't delete files if account UUIDs don't match
- **Collision detection**: Logs warnings if UUID conflicts are detected across accounts
- **Non-destructive by design**: Preserves files that don't match current export
- **Validation checks**: Verifies export format before processing

Then everything should just work!


#### Claude Code

Claude Code writes a JSONL transcript per session under `~/.claude/projects/`. These are local to your machine-- not synced to the cloud.

`claude_code_hook.py` in this repo can archive all those sessions for search, sync, and markdown editing.

Setup is one command:

```
python3 migrations/002_setup_claude_code_archival.py
```

Which adds hooks in your `~/.claude/settings.json` to call `claude_code_hook.py`. Sessions are archived based on `CLAUDE_CODE_HOST` and `CLAUDE_CODE_SOURCES`, which must be set in `.env`. The migration sets these for you.


**Assumption**: Claude Code JSONL transcripts are immutable append-only logs.

The line-count-based sync depends on this. If this changes, archives could diverge from `~/.claude/projects/` — the hook writes to `claude_code_anomalies.log` as a canary.

## Usage (if you set up based aliases)

```
# Enter to resume, v to open markdown in `$EDITOR`, h to open HTML in browser.
# q, Esc, or Ctrl-C to exit
cs "hi claude"

# Open the top 3 results for "books" in `$EDITOR`
cs books -o 3

# JSON output
cs books -j > results.json

# Browse everything, newest first (no query)
cs

# For fun, analytics over your archive!
cs --stats
cs --stats -s claude-code

# Bulk-export the whole archive
python3 export_archive.py ~/Obsidian/llm-archive

# Preview what would be exported, writing nothing
python3 export_archive.py -s claude-code --dry-run
```

---


## Example Directory Structure

`llm_data/`, `archived_exports/`, `local_views/`, and all `claude-code/<hostname>` locations are independent. Change them in `.env`.

Here's the conventional structure:

```
clauding-at-home/
├── data/                           # Sync this entire folder (e.g. with MEGA)
│   ├── llm_data/                   # Organized chat archives
│   │   ├── claude/
│   │   │   └── user@example.com/
│   │   │       ├── conversations/
│   │   │       │   └── YYYY-MM-DD_Title.json
│   │   │       ├── projects/
│   │   │       │   └── YYYY-MM-DD_Project.json
│   │   │       └── user.json
│   │   ├── chatgpt/
│   │   │   └── user@example.com/
│   │   │       ├── conversations/
│   │   │       │   └── YYYY-MM-DD_Title.json
│   │   │       └── user.json
│   │   └── claude-code/            # Claude Code session archives
│   │       └── <hostname>/         # one subdir per machine
│   │           └── <project-slug>/
│   │               └── <session-id>.jsonl
│   ├── archived_exports/           # Processed export zip files
│   │   ├── claude/
│   │   │   └── data-YYYY-MM-DD-*.zip
│   │   └── chatgpt/
│   │       └── [hex]-YYYY-MM-DD-*.zip
│   └── local_views/                # Generated Markdown/HTML views
│       ├── claude/
│       │   ├── {uuid}.md
│       │   └── {uuid}.html
│       └── chatgpt/
│           ├── {uuid}.md
│           └── {uuid}.html
├── migrations/                     # Idempotent!
│   ├── 001_consolidate_data_dirs.py
│   └── 002_setup_claude_code_archival.py
├── sync_local_chats_archive.py     # Import and sync exports
├── claude_code_hook.py             # Claude Code Stop/SessionEnd hook
└── full_text_search_chats_archive.py  # Search conversations
```

## Search index

Search runs on an SQLite FTS5 index that's built automatically on the first run and refreshed on each search: every changed file is detected by its mtime/ctime/size. The index stores the extracted texts of each file, so a search scores and snippets straight from the index without re-reading the matched files. The index is a pure accelerator: the *set* of results, every score, and every snippet are identical to a full scan. (The one exception: results with *exactly equal* total scores may appear in a different relative order, because the scan path's tie order follows filesystem directory order, which the index can't reconstruct.)

For debugging, we support `--no-index` and `--verify` (which diffs searches with and without the index).

The index rebuilds itself if deleted, corrupted, or outdated, including automatically whenever the extraction code changes.

Default location is `~/.cache/clauding-at-home/index.db`. You can change this in `.env`.


## Known Limitations

### Conversation forks (Claude.ai)

The official Claude.ai data export **does not fully preserve forked conversations**. Specifically:

- **Human messages from all branches** are included in the export (as consecutive same-sender entries in `chat_messages`).
- **Assistant responses from non-selected branches are missing.** Only the response from the branch you last had selected is exported.

This means search results may not include text from assistant responses in branches you didn't select. There is no workaround within this tool since the data simply isn't present in the export.

**Workarounds:**
- Before exporting, revisit conversations with important forks and switch to each branch you care about (the export appears to capture whichever branch is active).

**Claude Code forks include all text in both search and markdown generation.**


### Home/End keys in macOS Terminal.app

Interactive search accepts Home/End to jump to the first/last result, but the stock macOS Terminal.app does not send the standard escape sequences for those keys by default — it scrolls the scrollback instead. Use `g` / `G` (vim-style aliases) to jump to the top/bottom, or switch to iTerm2 / WezTerm / Ghostty where Home/End work as expected.


## Requirements

- **Python**: 3.7 or higher
- **Dependencies**: None (uses standard library only)

## Testing

To run the test suite:

```bash
# Option 1: Virtual environment (works on all platforms)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements-test.txt
pytest

# Option 2: System package manager
# Debian/Ubuntu: sudo apt install python3-pytest
# Fedora: sudo dnf install python3-pytest
# Arch: sudo pacman -S python-pytest
# macOS: brew install pytest

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/integration/test_sync_workflow.py
```

See [tests/README.md](tests/README.md) for detailed testing documentation, including test structure, fixtures, and debugging tips.

## Contributing

This tool is designed to be extensible. To add support for a new provider:

1. Create a new `Provider` subclass in `sync_local_chats_archive.py`
2. Implement the required methods (`name()`, `find_exports()`, `extract_data()`, `validate()`)
3. Add provider-specific URL generation to `SearchResult.get_provider_url()`
4. Update documentation with export format details
