"""Terminal ANSI escape vocabulary + a strip helper.

One home for the colour codes the search CLI, ``setup.py``, and the
data-structure checker were each re-declaring. Exposed both as module-level
constants and as a ``Colors`` namespace (the search CLI reads ``Colors.X``).
Stdlib only.

Naming note: ``GREEN``/``RED``/``YELLOW``/… are the standard (3x) codes; the
``BRIGHT_*`` set is the 9x variants. ``setup.py`` and the data-structure checker
historically named their *bright* codes plain ``GREEN``/``RED``/…, so they import
the ``BRIGHT_*`` names aliased — keeping their on-screen colour identical.
"""
from __future__ import annotations

import re

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Foreground colors (standard)
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"

# Bright variants
BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_BLUE = "\033[94m"
BRIGHT_MAGENTA = "\033[95m"
BRIGHT_CYAN = "\033[96m"

# 256-color orange — the claude-code badge / resume-line colour.
ORANGE = "\033[38;5;208m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    """Remove ANSI SGR escape sequences from a string."""
    return _ANSI_RE.sub("", s)


class Colors:
    """Namespace view of the constants above (the search CLI reads ``Colors.X``)."""
    RESET = RESET
    BOLD = BOLD
    DIM = DIM
    RED = RED
    GREEN = GREEN
    YELLOW = YELLOW
    BLUE = BLUE
    MAGENTA = MAGENTA
    CYAN = CYAN
    WHITE = WHITE
    BRIGHT_RED = BRIGHT_RED
    BRIGHT_GREEN = BRIGHT_GREEN
    BRIGHT_YELLOW = BRIGHT_YELLOW
    BRIGHT_BLUE = BRIGHT_BLUE
    BRIGHT_MAGENTA = BRIGHT_MAGENTA
    BRIGHT_CYAN = BRIGHT_CYAN
    ORANGE = ORANGE
