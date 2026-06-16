#!/usr/bin/env python3
"""Entry point — bulk-export the chat archive to Markdown/HTML.

Thin shim; the implementation lives in ``scrying_at_home.cli.export``. Kept at
the repo root under this exact name (public documented command, invoked by the
integration tests by path). Do not rename it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.cli.export import main

if __name__ == "__main__":
    main()
