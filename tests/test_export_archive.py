"""
Unit tests for export_archive.py — the pure planning/index core behind the
bulk Markdown export. Exercised with fabricated SearchResult objects; no I/O.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.cli import export as ex
from scrying_at_home.search import engine as fts


def _result(provider="claude", name="Hello World", created_at="2026-01-02T09:00:00Z",
            email="me@example.com", uuid="u1", extra=None, type="conversation",
            filepath=Path("/tmp/x")):
    return fts.SearchResult(
        type=type, uuid=uuid, name=name, created_at=created_at,
        updated_at=created_at, email=email, provider=provider,
        filepath=filepath, matches=[], total_score=0.0, extra=extra,
    )


def test_export_group_email_vs_host():
    assert ex.export_group(_result("claude", email="a@b.com")) == "a@b.com"
    cc = _result("claude-code", extra={"host": "laptop"})
    assert ex.export_group(cc) == "laptop"
    # Bucket C guard: codex is local-cli, so it groups by host like claude-code.
    cx = _result("codex", extra={"host": "desktop"}, email="")
    assert ex.export_group(cx) == "desktop"


def test_plan_path_layout_and_naming():
    [(r, rel)] = ex.plan_exports([_result()])
    assert rel == Path("claude/me@example.com/conversations/2026-01-02_Hello-World.md")


def test_plan_splits_conversations_and_projects():
    results = [
        _result(uuid="c", type="conversation"),
        _result(uuid="p", type="project"),
    ]
    dirs = {r.uuid: rel.parent.as_posix() for r, rel in ex.plan_exports(results)}
    assert dirs["c"] == "claude/me@example.com/conversations"
    assert dirs["p"] == "claude/me@example.com/projects"


def test_plan_dedupes_collisions_stably():
    a = _result(uuid="a")
    b = _result(uuid="b")  # same provider/group/date/name -> collision
    paths = {r.uuid: rel for r, rel in ex.plan_exports([a, b])}
    assert paths["a"] == Path("claude/me@example.com/conversations/2026-01-02_Hello-World.md")
    assert paths["b"] == Path("claude/me@example.com/conversations/2026-01-02_Hello-World-2.md")


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
    assert dirs == {
        "claude/a@b.com/conversations",
        "claude-code/laptop/conversations",
        "claude-code/desktop/conversations",
    }


def test_build_index_groups_and_links():
    planned = ex.plan_exports([
        _result("claude", name="First", uuid="1"),
        _result("claude-code", name="Second", extra={"host": "laptop"}, uuid="2"),
    ])
    index = ex.build_index(planned, today="2026-06-10")
    assert "# LLM Archive Export" in index
    assert "2 item(s)" in index
    assert "## claude.ai / me@example.com" in index
    assert "## claude-code / laptop" in index
    # links are relative posix paths
    assert "(claude/me@example.com/conversations/2026-01-02_First.md)" in index


def test_build_index_tags_projects():
    planned = ex.plan_exports([_result(name="My Project", type="project")])
    index = ex.build_index(planned, today="2026-06-10")
    assert "[My Project](claude/me@example.com/projects/2026-01-02_My-Project.md) *(project)*" in index


def test_run_export_indexes_only_written_files(tmp_path, monkeypatch):
    good = _result(name="Good", uuid="g", filepath=Path("/tmp/good.json"))
    bad = _result(name="Bad", uuid="b", filepath=Path("/tmp/bad.json"))

    def fake_render(provider, filepath, fmt, item_type="conversation"):
        if filepath.name == "bad.json":
            raise ValueError("corrupt file")
        return "# rendered"

    monkeypatch.setattr(ex, "render_conversation", fake_render)
    written = ex.run_export(tmp_path, [good, bad], "markdown", dry_run=False)

    assert written == 1
    assert (tmp_path / "claude/me@example.com/conversations/2026-01-02_Good.md").exists()
    index = (tmp_path / "index.md").read_text()
    assert "Good" in index
    assert "Bad" not in index  # failed render must not leave a dead link
