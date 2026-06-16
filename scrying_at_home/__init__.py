"""scrying_at_home — internal library code for the scrying-at-home tool.

The repo root holds the user-facing entry-point scripts (the public surface);
everything they lean on lives here under ``scrying_at_home`` as importable
modules. This package is the home for project internals that may be reorganized
between releases — only the root entry-point filenames are a stable contract.

The import package name matches the reserved PyPI distribution (scrying-at-home);
the bare ``scrying`` name is taken by an unrelated project.

``scrying_at_home.common`` is the stdlib-only leaf layer: no first-party imports, so any
other module can depend on it without risking an import cycle.
"""
