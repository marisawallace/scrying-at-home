#!/usr/bin/env python3
"""
One-off redactor that sanitizes the Codex session fixture for the public repo.

Run once against tests/fixtures/sample_codex_session_with_tools.jsonl. It is
committed for provenance: it documents exactly which transformations were
applied. All edits are *textual* (targeted string/regex replacement on the raw
file) so the diff contains only the intended bytes and nothing is reformatted.

Transformations (see plan: only residual signals are touched; the file was
already path/repo-normalized and the natural-language + command surface was
human-reviewed and found clean):

  1. Linkable ids -> char-flipped.  Every UUID (session id, turn id), the git
     commit hash, and every `call_*` tool id has a few characters flipped to
     other characters from the same alphabet, keeping shape/length valid. Each
     unique id maps to ONE replacement applied everywhere it occurs, so the
     function_call <-> output <-> patch pairings stay intact while the ids can
     no longer be linked back to the real OpenAI session. The flip is derived
     deterministically from the id (sha256-seeded) so the result is reproducible.

  2. Timezone -> neutral.  "America/Denver" -> "America/New_York" (removes a
     location signal; format-preserving).

  3. Rate limits -> neutralized.  On every token_count record:
     used_percent -> 0.0, resets_at -> 0, plan_type "free" -> null (removes
     account-tier + real usage-window info). window_minutes is generic, kept.

Timestamps are intentionally left as-is.

Idempotency: refuses to run if the fixture no longer shows the pre-redaction
markers (so an accidental second run cannot re-flip already-flipped ids).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import sys
from pathlib import Path

from tools.fixture_pii_audit import DEFAULT_FIXTURE, PLACEHOLDER_TIMEZONE, read_records

ORIGINAL_TIMEZONE = "America/Denver"
PLACEHOLDER_HOME = "/home/user"
HEX = "0123456789abcdef"
BASE62 = string.ascii_letters + string.digits
RESETS_SENTINEL = 0


# ----------------------------------------------------------------------------
# Functional core
# ----------------------------------------------------------------------------
def _seeded_rng(token: str) -> random.Random:
    """Stable per-token RNG (process-hash-independent, so reproducible)."""
    seed = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
    return random.Random(seed)


def flip_chars(s: str, alphabet: str, n: int, rng: random.Random, protect_prefix: int = 0) -> str:
    """Flip up to n positions of `s` whose char is in `alphabet` to a *different*
    char from `alphabet`. Positions before `protect_prefix` are never touched."""
    positions = [i for i in range(protect_prefix, len(s)) if s[i] in alphabet]
    if not positions:
        return s
    chosen = rng.sample(positions, min(n, len(positions)))
    chars = list(s)
    for i in chosen:
        alt = rng.choice([c for c in alphabet if c != chars[i]])
        chars[i] = alt
    return "".join(chars)


def make_replacement(token: str, rng: random.Random) -> str:
    """Char-flip one id, dispatching on its shape (uuid / 40-hex / call_*)."""
    if token.startswith("call_"):
        return flip_chars(token, BASE62, n=4, rng=rng, protect_prefix=5)
    if re.fullmatch(r"[0-9a-fA-F]{40}", token):
        return flip_chars(token.lower(), HEX, n=4, rng=rng)
    # UUID-shaped (dashes are not in HEX, so they are skipped automatically).
    return flip_chars(token.lower(), HEX, n=3, rng=rng)


def gather_ids(records: list[tuple[int, dict]]) -> list[str]:
    """All linkable id strings: session id, turn ids, commit hash, call ids."""
    ids: set[str] = set()
    for _, d in records:
        pl = d.get("payload", {})
        if not isinstance(pl, dict):
            continue
        if d.get("type") == "session_meta":
            if pl.get("id"):
                ids.add(pl["id"])
            commit = pl.get("git", {}).get("commit_hash")
            if commit:
                ids.add(commit)
        if pl.get("turn_id"):
            ids.add(pl["turn_id"])
        if pl.get("call_id"):
            ids.add(pl["call_id"])
    return sorted(ids)


def build_id_replacements(ids: list[str]) -> dict[str, str]:
    """One unique char-flipped replacement per id, collision-free."""
    taken: set[str] = set(ids)
    mapping: dict[str, str] = {}
    for token in ids:
        rng = _seeded_rng(token)
        new = make_replacement(token, rng)
        while new in taken or new == token:
            new = make_replacement(token + "!", rng)  # perturb until unique
        mapping[token] = new
        taken.add(new)
    return mapping


def apply_textual(text: str, id_map: dict[str, str], real_home: str | None = None) -> str:
    """Apply every transformation textually to the whole-file string.

    `real_home` (the operator's $HOME, supplied at runtime so it is never
    hardcoded here) is normalized to PLACEHOLDER_HOME. It is a no-op for a
    fixture that is already path-normalized.
    """
    # 0. real home dir -> placeholder (sourced from the environment, not literal)
    if real_home and real_home not in ("/", "") and real_home != PLACEHOLDER_HOME:
        text = text.replace(real_home, PLACEHOLDER_HOME)
    # 1. ids — replace longest first so no id is a prefix-substring of another.
    for old in sorted(id_map, key=len, reverse=True):
        text = text.replace(old, id_map[old])
    # 2. timezone
    text = text.replace(ORIGINAL_TIMEZONE, PLACEHOLDER_TIMEZONE)
    # 3. rate limits (keys are unique to rate_limits, so file-wide subs are safe)
    text = re.sub(r'"used_percent":\s*[0-9.]+', '"used_percent": 0.0', text)
    text = re.sub(r'"resets_at":\s*[0-9]+', f'"resets_at": {RESETS_SENTINEL}', text)
    text = text.replace('"plan_type": "free"', '"plan_type": null')
    return text


# ----------------------------------------------------------------------------
# Imperative shell
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    path = Path(argv[0]) if argv else DEFAULT_FIXTURE
    text = path.read_text(encoding="utf-8")
    real_home = os.environ.get("HOME")
    home_present = bool(real_home and real_home != PLACEHOLDER_HOME and real_home in text)

    if ORIGINAL_TIMEZONE not in text and '"plan_type": "free"' not in text and not home_present:
        print("Refusing to run: fixture has no pre-redaction markers (already sanitized?).")
        return 2

    records = read_records(path)
    ids = gather_ids(records)
    id_map = build_id_replacements(ids)

    new_text = apply_textual(text, id_map, real_home)
    # Sanity: every line still parses, record count preserved.
    new_lines = [ln for ln in new_text.split("\n") if ln.strip()]
    for ln in new_lines:
        json.loads(ln)
    assert len(new_lines) == len(records), "record count changed!"

    path.write_text(new_text, encoding="utf-8")

    print(f"Sanitized {path}")
    print(f"  ids char-flipped : {len(id_map)} "
          f"(1 session, {sum(1 for k in id_map if k.startswith('call_'))} call_*, "
          f"rest uuid/commit)")
    if home_present:
        print(f"  home dir         : {real_home} -> {PLACEHOLDER_HOME}")
    print(f"  timezone         : {ORIGINAL_TIMEZONE} -> {PLACEHOLDER_TIMEZONE}")
    print(f"  rate_limits      : used_percent/resets_at zeroed, plan_type -> null")
    print("\n  sample id flips:")
    for old in list(id_map)[:4]:
        print(f"    {old}  ->  {id_map[old]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
