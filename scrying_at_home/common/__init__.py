"""Stdlib-only shared leaves: timestamps, text, constants, ANSI.

Every module here imports only the standard library, so the rest of the project
can depend on it freely. These are the single homes for cross-cutting primitives
(ISO-timestamp handling, name/identity normalization, shared string constants,
terminal colours) that were previously re-implemented across several scripts.
"""
