#!/usr/bin/env python3
"""Entry point — Claude Code session archival hook.

Thin shim; the implementation lives in ``scrying_at_home.sync.claude_code_hook``.
This file MUST stay at the repo root under this exact name: migration 002 bakes
its absolute path into ~/.claude/settings.json and discovers the repo via this
filename (REPO_MARKER). Do not rename or move it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.sync.claude_code_hook import main

if __name__ == "__main__":
    main()
