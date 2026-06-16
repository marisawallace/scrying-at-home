"""
Permanent guard: no JSONL fixture in tests/fixtures/ may leak PII.

This is the durable backstop behind the one-off audit/redaction tooling
(fixture_pii_audit.py / sanitize_codex_fixture.py). It is *allowlist*-based:
it asserts the high-signal invariants from fixture_pii_audit.guard_violations
(emails only on reserved example.* domains, home/Users paths only among vetted
placeholders, no credential patterns, timezone only the neutral placeholder).
Because it encodes an allowlist rather than a denylist, this committed test
contains NO real identifiers, yet it still fails CI the moment a future fixture
introduces a real email, a stray /home/<someone>, a secret, or a location
timezone -- the exact blind spot that let a real home path reach the public repo
once before.
"""

import sys
from pathlib import Path

import pytest

# Add project root to path (mirrors the other test modules).
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import fixture_pii_audit as fa

FIXTURE_DIR = Path(__file__).parent / "fixtures"
JSONL_FIXTURES = sorted(FIXTURE_DIR.glob("*.jsonl"))


def test_fixtures_present():
    """Sanity: the guard is actually scanning something."""
    assert JSONL_FIXTURES, f"no .jsonl fixtures found under {FIXTURE_DIR}"


@pytest.mark.parametrize("fixture", JSONL_FIXTURES, ids=lambda p: p.name)
def test_fixture_has_no_pii(fixture):
    records = fa.read_records(fixture)
    violations = fa.guard_violations(records)
    assert not violations, (
        f"{fixture.name} contains likely PII:\n  "
        + "\n  ".join(violations)
        + f"\n\nInspect with:  python fixture_pii_audit.py {fixture}"
        + f"\nRedact with:   python sanitize_codex_fixture.py {fixture}"
    )
