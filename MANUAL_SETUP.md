```bash
chmod +x sync_local_chats_archive.py
chmod +x full_text_search_chats_archive.py
chmod +x view_conversation.py

cp .env.example .env
```

Then read and edit .env -- all options are explained!

I highly recommend adding aliases to your `.bashrc` or equivalent:

```bash
alias cs="python3 $CODE_HOME/clauding-at-home/full_text_search_chats_archive.py"
alias cs-sync-claude="python3 $CODE_HOME/clauding-at-home/sync_local_chats_archive.py --claude"
alias cs-sync-chatgpt="python3 $CODE_HOME/clauding-at-home/sync_local_chats_archive.py --chatgpt"
```

Make sure you have $EDITOR set. It'll be used to open chats in Markdown (hit v on a search result).

```bash
export VISUAL="code --wait"
export EDITOR="$VISUAL"
```

## Claude Code setup -- more on this in the README.md:

```bash
python3 migrations/002_setup_claude_code_archival.py
```

We don't currently support manual setup for Claude Code archiving. If you'd like this, open an issue!