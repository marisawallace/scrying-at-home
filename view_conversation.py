#!/usr/bin/env python3
"""Entry point — view a conversation as Markdown or HTML.

Thin shim; the implementation lives in ``scrying_at_home.view.render``. This file
stays at the repo root under this exact name (aliased / ENTRY_SCRIPTS, and the
picker + integration tests invoke it by path). Do not rename it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.view.render import main

if __name__ == "__main__":
    main()
