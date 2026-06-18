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

import os
import re
import sys

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


def color_enabled(stream=None) -> bool:
    """Whether to emit colour: honour ``NO_COLOR`` and require a real TTY.

    Defaults to stdout. Pass ``sys.stderr`` when styling text bound for it.
    """
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, *codes: str, stream=None) -> str:
    """Wrap ``text`` in the given SGR ``codes`` (then RESET), TTY permitting.

    The building block for the semantic helpers below; reuse it for ad-hoc
    styling elsewhere so the ``NO_COLOR``/TTY guard stays in one place.
    """
    if not codes or not color_enabled(stream):
        return text
    return "".join(codes) + text + RESET


# --- Semantic styles -------------------------------------------------------
# Prefer these over raw codes at call sites so the program's palette stays
# consistent and tweakable from one place.

def muted(text: str, *, stream=None) -> str:
    """De-emphasised status/detail lines (dim grey)."""
    return paint(text, DIM, stream=stream)


def warning(text: str, *, stream=None) -> str:
    """Attention-worthy notices the user should not miss (orange)."""
    return paint(text, ORANGE, stream=stream)


def success(text: str, *, stream=None) -> str:
    """Positive outcomes (green)."""
    return paint(text, GREEN, stream=stream)


def error(text: str, *, stream=None) -> str:
    """Failures (bright red)."""
    return paint(text, BRIGHT_RED, stream=stream)


def emphasis(text: str, *, stream=None) -> str:
    """Foreground detail worth highlighting (bold)."""
    return paint(text, BOLD, stream=stream)


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
