"""
Unit tests for export_archive.py — the pure planning/index core behind the
bulk Markdown export. Exercised with fabricated SearchResult objects; no I/O.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import export_archive as ex
import full_text_search_chats_archive as fts


def _result(provider="claude", name="Hello World", created_at="2026-01-02T09:00:00Z",
            email="me@example.com", uuid="u1", extra=None):
    return fts.SearchResult(
        type="conversation", uuid=uuid, name=name, created_at=created_at,
        updated_at=created_at, email=email, provider=provider,
        filepath=Path("/tmp/x"), matches=[], total_score=0.0, extra=extra,
    )


def test_export_group_email_vs_host():
    assert ex.export_group(_result("claude", email="a@b.com")) == "a@b.com"
    cc = _result("claude-code", extra={"host": "laptop"})
    assert ex.export_group(cc) == "laptop"


def test_plan_path_layout_and_naming():
    [(r, rel)] = ex.plan_exports([_result()])
    assert rel == Path("claude/me@example.com/2026-01-02_Hello-World.md")


def test_plan_dedupes_collisions_stably():
    a = _result(uuid="a")
    b = _result(uuid="b")  # same provider/group/date/name -> collision
    paths = {r.uuid: rel for r, rel in ex.plan_exports([a, b])}
    assert paths["a"] == Path("claude/me@example.com/2026-01-02_Hello-World.md")
    assert paths["b"] == Path("claude/me@example.com/2026-01-02_Hello-World-2.md")


def test_plan_respects_extension():
    [(_r, rel)] = ex.plan_exports([_result()], extension="html")
    assert rel.suffix == ".html"


def test_plan_separates_providers_and_hosts():
    results = [
        _result("claude", email="a@b.com", uuid="1"),
        _result("claude-code", extra={"host": "laptop"}, uuid="2"),
        _result("claude-code", extra={"host": "desktop"}, uuid="3"),
    ]
    dirs = {rel.parent.as_posix() for _, rel in ex.plan_exports(results)}
    assert dirs == {"claude/a@b.com", "claude-code/laptop", "claude-code/desktop"}


def test_build_index_groups_and_links():
    planned = ex.plan_exports([
        _result("claude", name="First", uuid="1"),
        _result("claude-code", name="Second", extra={"host": "laptop"}, uuid="2"),
    ])
    index = ex.build_index(planned, today="2026-06-10")
    assert "# LLM Archive Export" in index
    assert "2 conversation(s)" in index
    assert "## claude.ai / me@example.com" in index
    assert "## claude-code / laptop" in index
    # links are relative posix paths
    assert "(claude/me@example.com/2026-01-02_First.md)" in index
