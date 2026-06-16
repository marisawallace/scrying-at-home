#!/usr/bin/env python3
"""Entry point — sync chat-export zips into the local archive.

Thin shim; the implementation lives in ``scrying_at_home.sync.local_chats``. This
file stays at the repo root under this exact name because shell aliases
(``setup.py``, the ``-sync-claude`` / ``-sync-chatgpt`` aliases) and the
integration tests invoke it by path. Do not rename it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.sync.local_chats import main

if __name__ == "__main__":
    main()
