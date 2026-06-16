"""
Demo-mode post-processing for search results.

Undocumented support for regenerating README demo GIFs from live data while
obfuscating personal info. Activated only when one of the DEMO_* keys is set
in the project's .env; otherwise this module is a no-op.

.env keys:
  DEMO_SEARCH_OMIT_LIST   Comma-separated terms; results containing any term
                          (case-insensitive, anywhere in name/email/matches)
                          are dropped entirely.
  DEMO_HOSTNAMES          Comma-separated `real=fake` pairs; substring-replaced
                          across result fields.
  DEMO_EMAILS             Comma-separated `real=fake` pairs; substring-replaced
                          across result fields.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple


def _parse_pairs(raw: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        real, fake = entry.split("=", 1)
        real, fake = real.strip(), fake.strip()
        if real:
            pairs.append((real, fake))
    # Replace longer keys first so e.g. "foo.bar" wins over "foo".
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def _parse_omit(raw: str) -> List[str]:
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def _redact(text: str, pairs: Iterable[Tuple[str, str]]) -> str:
    if not text:
        return text
    for real, fake in pairs:
        # Case-insensitive substring replace.
        text = re.sub(re.escape(real), fake, text, flags=re.IGNORECASE)
    return text


def _contains_any(haystacks: Iterable[str], needles: Iterable[str]) -> bool:
    blob = "\n".join(h for h in haystacks if h).lower()
    return any(n in blob for n in needles)


def maybe_apply(results: list, config: dict) -> list:
    """Filter and redact `results` in place per DEMO_* keys in `config`.

    `config` is the project's .env-derived dict. Returns the (possibly
    shortened) list. No-op when no DEMO_* keys set.
    """
    omit = _parse_omit(config.get("DEMO_SEARCH_OMIT_LIST", ""))
    hostnames = _parse_pairs(config.get("DEMO_HOSTNAMES", ""))
    emails = _parse_pairs(config.get("DEMO_EMAILS", ""))

    if not (omit or hostnames or emails):
        return results

    pairs = emails + hostnames  # emails first: they're more specific

    kept = []
    for r in results:
        match_texts = [m.text for m in r.matches]
        if omit and _contains_any([r.name, r.email] + match_texts, omit):
            continue

        if pairs:
            r.name = _redact(r.name, pairs)
            r.email = _redact(r.email, pairs)
            for m in r.matches:
                m.text = _redact(m.text, pairs)
            if r.extra:
                for k, v in list(r.extra.items()):
                    if isinstance(v, str):
                        r.extra[k] = _redact(v, pairs)

        kept.append(r)

    return kept
