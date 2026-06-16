#!/usr/bin/env python3
"""Entry point — first-run setup wizard (aliases, .env, editor, migrations).

Thin shim; the implementation lives in ``scrying_at_home.cli.setup``. Kept at the
repo root under this exact name — documented and run as ``python3 setup.py``.
Do not rename it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrying_at_home.cli.setup import main

if __name__ == "__main__":
    main()
