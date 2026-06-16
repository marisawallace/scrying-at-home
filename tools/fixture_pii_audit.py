#!/usr/bin/env python3
"""
Multi-angle PII auditor for JSONL session-transcript fixtures.

Before a recorded Codex/Claude Code session ships in the public repo it must be
free of identifying / personal information. A human cannot read 400 KB of JSONL
by eye, so this module attacks the problem from several independent angles and
prints a short, scannable report (plus an optional human-readable digest):

  A. structural profile  -- record/payload type counts + byte budget, so the
                            reviewer knows what the file actually contains.
  B. denylist sweep      -- scans for *real* personal identifiers. The needles
                            are supplied at runtime (--denylist FILE / env), and
                            are NEVER hardcoded here, so committing this module
                            leaks nothing. Exits non-zero on any hit.
  C. regex PII battery    -- emails, IPs, home/Users paths, urls, secrets/keys,
                            timezones, etc., scanned over *decoded* string values
                            (not raw JSON, which avoids \\n@... escape artifacts),
                            then filtered through a positive allowlist of known
                            -safe placeholders so only *novel* values surface.
  D. entropy/secret scan  -- flags high-entropy tokens not explained by uuids /
                            commit hashes / the "<encrypted>" placeholder.
  E. digest (--digest)    -- flattens every legible string (commands, command
                            output, messages, patch diffs, reasoning summaries,
                            memory citations) into prose with line refs.

The regex/allowlist constants and `guard_violations()` are imported by
tests/test_fixture_pii_guard.py so there is a single source of truth, and so the
permanent CI guard can be allowlist-based (committing no real identifiers).

Usage:
  python fixture_pii_audit.py [FIXTURE]                  # passes A-D summary
  python fixture_pii_audit.py [FIXTURE] --digest         # human-readable E
  python fixture_pii_audit.py [FIXTURE] --show 68        # one record, full
  python fixture_pii_audit.py [FIXTURE] --denylist FILE  # add Pass B needles
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

from scrying_at_home.config.paths import REPO_ROOT

DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "sample_codex_session_with_tools.jsonl"
)

# The neutral timezone the redactor normalizes to; the guard allows only this.
PLACEHOLDER_TIMEZONE = "America/New_York"

# --- positive allowlist: values that are vetted, non-personal placeholders ----
# Home-directory usernames that are known repo placeholders / test data.
# "u" appears only as a dummy cwd ("/home/u/app") inside the repo's own unit
# tests; "user"/"testuser" are the normalized session + fixture placeholders.
ALLOWED_HOME_USERS = frozenset({"user", "testuser", "u"})
ALLOWED_MAC_USERS = frozenset({"me"})
# Reserved (RFC 2606) and vetted dummy email domains.
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "example.edu", "b.com"}
)
# Specific non-personal email-shaped literals (e.g. the git ssh remote).
ALLOWED_EMAIL_LITERALS = frozenset({"git@github.com"})
# Public hosts that are fine to appear in urls (docs links, remotes).
ALLOWED_URL_HOSTS = frozenset(
    {"github.com", "claude.ai", "chatgpt.com", "openai.com", "anthropic.com"}
)

PII_PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "ipv6": r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b",
    "mac": r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b",
    "home_path": r"/home/[A-Za-z0-9_.\-]+",
    "users_path": r"/Users/[A-Za-z0-9_.\-]+",
    "windows_path": r"[Cc]:\\Users\\[A-Za-z0-9_.\-]+",
    "url": r"https?://[^\s\"'<>)\]}]+",
    "secret_assign": (
        r"(?i)(?:bearer|token|api[_\-]?key|secret|password|passwd|access[_\-]?key)"
        r"\s*[=:]\s*[^\s\"',]{8,}"
    ),
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "openai_key": r"\bsk-[A-Za-z0-9]{20,}\b",
    "jwt": r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
    "timezone": (
        r"\b(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific)"
        r"/[A-Za-z_]+(?:/[A-Za-z_]+)?"
    ),
}

# Patterns whose any-match is a hard failure for the permanent guard.
HARD_SECRET_LABELS = ("aws_key", "openai_key", "jwt")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


# ----------------------------------------------------------------------------
# Functional core: reading + string extraction
# ----------------------------------------------------------------------------
def read_records(path: Path) -> list[tuple[int, dict]]:
    """Parse the fixture into (line_number, record) pairs.

    Split only on '\\n' -- the data embeds unicode line separators (U+2028 etc.)
    inside string values, which str.splitlines() would wrongly break on.
    """
    out: list[tuple[int, dict]] = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if line.strip():
                out.append((i, json.loads(line)))
    return out


def iter_strings(obj: object) -> Iterator[str]:
    """Yield every string *value* leaf (not keys) in a decoded JSON object."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


def record_text(record: dict) -> str:
    """All decoded string leaves of a record joined for regex scanning."""
    return "\n".join(iter_strings(record))


# ----------------------------------------------------------------------------
# Pass A: structural profile
# ----------------------------------------------------------------------------
def structural_profile(records: list[tuple[int, dict]]) -> str:
    type_counts: Counter[tuple] = Counter()
    byte_budget: Counter[tuple] = Counter()
    for _, d in records:
        pl = d.get("payload", {})
        pt = pl.get("type") if isinstance(pl, dict) else None
        role = pl.get("role") if isinstance(pl, dict) else None
        key = (d.get("type"), pt, role)
        type_counts[key] += 1
        byte_budget[(d.get("type"), pt)] += len(json.dumps(d))

    lines = [f"records: {len(records)}", "", "counts by (record / payload / role):"]
    for (rt, pt, role), c in type_counts.most_common():
        lines.append(f"  {c:4d}  {rt} / {pt}" + (f" / role={role}" if role else ""))
    lines += ["", "byte budget by (record / payload):"]
    total = sum(byte_budget.values()) or 1
    for (rt, pt), b in byte_budget.most_common():
        lines.append(f"  {b:8d}  ({b / total * 100:4.1f}%)  {rt} / {pt}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Pass B: runtime denylist sweep (no real identifiers committed)
# ----------------------------------------------------------------------------
def load_denylist(explicit: Path | None) -> list[str]:
    """Read denylist needles from --denylist, $FIXTURE_PII_DENYLIST, or a local
    .fixture-denylist file. One needle per line; '#' comments and blanks ignored.
    Absent sources are fine -- Pass B simply reports it could not run."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("FIXTURE_PII_DENYLIST")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT / ".fixture-denylist")
    for c in candidates:
        if c and c.exists():
            needles = []
            for ln in c.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    needles.append(ln)
            return needles
    return []


def denylist_hits(records: list[tuple[int, dict]], needles: Iterable[str]) -> list[tuple[str, int]]:
    pats = [(n, re.compile(re.escape(n), re.IGNORECASE)) for n in needles]
    hits: list[tuple[str, int]] = []
    for line, d in records:
        text = record_text(d)
        for needle, pat in pats:
            if pat.search(text):
                hits.append((needle, line))
    return hits


# ----------------------------------------------------------------------------
# Pass C: regex PII battery + allowlist
# ----------------------------------------------------------------------------
def _path_user(value: str, prefix: str) -> str:
    return value[len(prefix):].split("/")[0]


def is_allowlisted(label: str, value: str) -> bool:
    """True if a regex hit is a known-safe placeholder (not novel PII)."""
    if label == "email":
        v = value.lower()
        if v in ALLOWED_EMAIL_LITERALS:
            return True
        domain = v.rsplit("@", 1)[-1]
        return domain in ALLOWED_EMAIL_DOMAINS
    if label == "home_path":
        return _path_user(value, "/home/") in ALLOWED_HOME_USERS
    if label == "users_path":
        return _path_user(value, "/Users/") in ALLOWED_MAC_USERS
    if label == "url":
        host = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0].lower()
        return host in ALLOWED_URL_HOSTS
    if label == "timezone":
        return value == PLACEHOLDER_TIMEZONE
    if label == "ipv4":
        return value in {"127.0.0.1", "0.0.0.0", "255.255.255.255"} or value.startswith(
            ("10.", "192.168.", "172.16.")
        )
    return False  # secrets, windows paths, ipv6, mac, etc. are never auto-safe


def regex_findings(records: list[tuple[int, dict]]) -> dict[str, list[tuple[str, int, int]]]:
    """label -> sorted list of (value, count, first_line) over decoded text."""
    compiled = {lbl: re.compile(p) for lbl, p in PII_PATTERNS.items()}
    seen: dict[str, dict[str, list[int]]] = {lbl: {} for lbl in compiled}
    for line, d in records:
        text = record_text(d)
        for lbl, pat in compiled.items():
            for m in pat.findall(text):
                seen[lbl].setdefault(m, []).append(line)
    out: dict[str, list[tuple[str, int, int]]] = {}
    for lbl, vals in seen.items():
        out[lbl] = sorted(
            ((v, len(ls), min(ls)) for v, ls in vals.items()),
            key=lambda t: (-t[1], t[0]),
        )
    return out


def novel_findings(findings: dict[str, list[tuple[str, int, int]]]) -> dict[str, list[tuple[str, int, int]]]:
    return {
        lbl: [(v, c, fl) for (v, c, fl) in items if not is_allowlisted(lbl, v)]
        for lbl, items in findings.items()
    }


# ----------------------------------------------------------------------------
# Pass D: entropy / secret scan
# ----------------------------------------------------------------------------
def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def secret_candidates(
    records: list[tuple[int, dict]], min_len: int = 24, min_entropy: float = 3.6
) -> list[tuple[str, int, float]]:
    """High-entropy tokens not explained by uuids / hashes / placeholders."""
    token_re = re.compile(r"[A-Za-z0-9+/=_\-]{%d,}" % min_len)
    found: dict[str, int] = {}
    for line, d in records:
        for tok in token_re.findall(record_text(d)):
            if UUID_RE.match(tok) or HEX40_RE.match(tok):
                continue
            if shannon_entropy(tok) >= min_entropy:
                found.setdefault(tok, line)
    return sorted(((t, l, shannon_entropy(t)) for t, l in found.items()), key=lambda x: -x[2])


# ----------------------------------------------------------------------------
# Permanent guard: allowlist-based, returns human-readable violations
# ----------------------------------------------------------------------------
def guard_violations(records: list[tuple[int, dict]]) -> list[str]:
    """High-signal invariants for tests/fixtures/. Empty list == clean."""
    findings = regex_findings(records)
    violations: list[str] = []
    for lbl in ("email", "home_path", "users_path", "windows_path", "timezone", *HARD_SECRET_LABELS):
        for value, count, first_line in findings.get(lbl, []):
            if not is_allowlisted(lbl, value):
                violations.append(f"[{lbl}] {value!r} (x{count}, first line {first_line})")
    return violations


# ----------------------------------------------------------------------------
# Pass E: human-readable digest
# ----------------------------------------------------------------------------
def _clip(s: str, limit: int) -> str:
    s = str(s)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + f"  …[+{len(s) - limit} chars]"


def _content_text(content: object) -> str:
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") or c.get("input_text") or json.dumps(c))
            else:
                parts.append(str(c))
        return " ".join(parts)
    return str(content)


def legible_block(line: int, record: dict, limit: int) -> str:
    pl = record.get("payload", {})
    pt = pl.get("type") if isinstance(pl, dict) else None
    rt = record.get("type")
    head = f"--- line {line}: {rt} / {pt} ---"

    def body() -> str:
        if rt == "session_meta":
            g = pl.get("git", {})
            return (
                f"id={pl.get('id')} cwd={pl.get('cwd')} cli={pl.get('cli_version')} "
                f"provider={pl.get('model_provider')}\n"
                f"git: branch={g.get('branch')} commit={g.get('commit_hash')} url={g.get('repository_url')}\n"
                f"base_instructions: {_clip(pl.get('base_instructions', {}).get('text', ''), limit)}"
            )
        if rt == "turn_context":
            return (
                f"cwd={pl.get('cwd')} tz={pl.get('timezone')} date={pl.get('current_date')} "
                f"model={pl.get('model')} approval={pl.get('approval_policy')}"
            )
        if pt == "message":
            return f"[{pl.get('role')}] {_clip(_content_text(pl.get('content')), limit)}"
        if pt in ("user_message", "agent_message"):
            extra = ""
            if pl.get("memory_citation"):
                extra = f"\n  memory_citation: {_clip(pl.get('memory_citation'), limit)}"
            return _clip(pl.get("message") or pl.get("text") or "", limit) + extra
        if pt == "function_call":
            try:
                args = json.loads(pl.get("arguments", "{}"))
            except (ValueError, TypeError):
                args = pl.get("arguments")
            cmd = args.get("cmd") or args.get("command") if isinstance(args, dict) else args
            wd = args.get("workdir") if isinstance(args, dict) else None
            return f"[{pl.get('name')}] cwd={wd}\n  $ {_clip(cmd, limit)}"
        if pt in ("function_call_output", "custom_tool_call_output"):
            return _clip(pl.get("output"), limit)
        if pt == "custom_tool_call":
            return f"[{pl.get('name')}] status={pl.get('status')}\n{_clip(pl.get('input'), limit)}"
        if pt == "reasoning":
            summ = _content_text(pl.get("summary") or [])
            return _clip(summ, limit) if summ.strip() else "(no summary; encrypted_content=<encrypted>)"
        if pt == "patch_apply_end":
            changes = pl.get("changes") or {}
            paths = list(changes) if isinstance(changes, dict) else []
            diffs = "\n".join(
                f"  {p}: {_clip(c.get('unified_diff', ''), limit // 2)}"
                for p, c in (changes.items() if isinstance(changes, dict) else [])
            )
            return f"status={pl.get('status')} success={pl.get('success')} files={len(paths)}\n{diffs}"
        if pt == "token_count":
            info = pl.get("info", {}).get("total_token_usage", {})
            rl = pl.get("rate_limits", {})
            return f"total_tokens={info.get('total_tokens')} plan={rl.get('plan_type')}"
        return _clip(json.dumps(pl), limit)

    return head + "\n" + body()


def digest(records: list[tuple[int, dict]], limit: int, show: int | None) -> str:
    if show is not None:
        for line, d in records:
            if line == show:
                return legible_block(line, d, limit=0)
        return f"(no record on line {show})"
    return "\n\n".join(legible_block(line, d, limit) for line, d in records)


# ----------------------------------------------------------------------------
# Imperative shell
# ----------------------------------------------------------------------------
def run_report(records: list[tuple[int, dict]], denylist: list[str]) -> int:
    print("=" * 72)
    print("PASS A — STRUCTURAL PROFILE")
    print("=" * 72)
    print(structural_profile(records))

    print("\n" + "=" * 72)
    print("PASS B — DENYLIST SWEEP (real identifiers; must be zero)")
    print("=" * 72)
    if not denylist:
        print("  (no denylist provided — supply --denylist FILE, $FIXTURE_PII_DENYLIST,")
        print("   or a local .fixture-denylist to run this angle)")
        b_failed = False
    else:
        hits = denylist_hits(records, denylist)
        print(f"  {len(denylist)} needles checked")
        for needle, line in hits:
            print(f"  !! HIT  {needle!r} on line {line}")
        if not hits:
            print("  clean — zero hits")
        b_failed = bool(hits)

    print("\n" + "=" * 72)
    print("PASS C — REGEX PII BATTERY (novel values only; allowlisted hidden)")
    print("=" * 72)
    findings = regex_findings(records)
    novel = novel_findings(findings)
    any_novel = False
    for lbl in PII_PATTERNS:
        items = novel.get(lbl, [])
        allowed_n = len(findings[lbl]) - len(items)
        if not items:
            tag = f"(all {allowed_n} allowlisted)" if allowed_n else "none"
            print(f"\n  [{lbl}] {tag}")
            continue
        any_novel = True
        print(f"\n  [{lbl}] {len(items)} novel value(s)" + (f"; {allowed_n} allowlisted" if allowed_n else ""))
        for value, count, first_line in items[:20]:
            print(f"      x{count:<3d} line {first_line:<4d} {_clip(value, 110)}")

    print("\n" + "=" * 72)
    print("PASS D — ENTROPY / SECRET SCAN")
    print("=" * 72)
    cands = secret_candidates(records)
    if not cands:
        print("  none — no high-entropy tokens beyond uuids/hashes/placeholders")
    for tok, line, ent in cands[:20]:
        print(f"  entropy={ent:.2f}  line {line}  {_clip(tok, 80)}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"  denylist hits : {'FAIL' if denylist and b_failed else 'clean' if denylist else 'not run'}")
    print(f"  novel regex   : {'review needed' if any_novel else 'clean'}")
    print(f"  secret scan   : {'review needed' if cands else 'clean'}")
    return 1 if (denylist and b_failed) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Multi-angle PII auditor for JSONL fixtures.")
    ap.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--digest", action="store_true", help="print the human-readable flatten (Pass E)")
    ap.add_argument("--full", action="store_true", help="do not truncate digest bodies")
    ap.add_argument("--show", type=int, metavar="LINE", help="dump one record's full content")
    ap.add_argument("--denylist", type=Path, help="file of real-identifier needles for Pass B")
    args = ap.parse_args(argv)

    records = read_records(args.fixture)

    if args.show is not None:
        print(digest(records, limit=0, show=args.show))
        return 0
    if args.digest:
        print(digest(records, limit=0 if args.full else 700, show=None))
        return 0
    return run_report(records, load_denylist(args.denylist))


if __name__ == "__main__":
    sys.exit(main())
