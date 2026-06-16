#!/usr/bin/env python3
"""Entry point — OpenAI Codex session archival hook.

Thin shim; the implementation lives in ``scrying_at_home.sync.codex_sync``. This
file MUST stay at the repo root under this exact name: migration 004 bakes its
absolute path into ~/.codex/hooks.json and discovers the repo via this filename
(REPO_MARKER). Do not rename or move it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.sync.codex_sync import main

if __name__ == "__main__":
    main()
