"""
Characterization tests for interactive_picker.act_on_choice / _provider_label
and SearchResult.get_provider_url.

These pin the per-provider RESUME behavior (the exact argv + cwd handed to the
`claude` CLI, the foreign-host print-only fallback, the browser-open path) and
the provider label/colour and URL-scheme mappings — all shared code an upcoming
provider-registry refactor will centralize. The index↔scan parity nets are
blind to this code (both paths run it identically), so it is pinned directly.

Pure units: subprocess.run and webbrowser.open are monkeypatched; nothing is
actually launched.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrying_at_home.view import picker as ip
from scrying_at_home.search.engine import SearchResult


def make_result(provider="claude-code", uuid="cc-test-session-001",
                type="conversation", extra=None, **over) -> SearchResult:
    base = dict(
        type=type, uuid=uuid, name="n",
        created_at="2026-04-10T14:00:00Z", updated_at="2026-04-10T14:01:05Z",
        email="", provider=provider, filepath=Path("x.jsonl"),
        matches=[], total_score=1.0, extra=extra, model="",
    )
    base.update(over)
    return SearchResult(**base)


class _FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode


# ---------------------------------------------------------------------------
# act_on_choice — claude-code resume
# ---------------------------------------------------------------------------

def test_act_cc_same_host_execs_claude(tmp_path, monkeypatch):
    calls = {}

    def fake_run(argv, cwd=None, **kw):
        calls["argv"] = argv
        calls["cwd"] = cwd
        return _FakeProc(7)

    monkeypatch.setattr(ip.subprocess, "run", fake_run)
    result = make_result(extra={"cwd": str(tmp_path), "git_branch": "main",
                                "host": "boxA"})

    rc = ip.act_on_choice(result, current_host="boxA", demo=False)

    assert calls["argv"] == ["claude", "-r", "cc-test-session-001"]
    assert calls["cwd"] == str(tmp_path)
    assert rc == 7  # child's returncode is propagated


def test_act_cc_different_host_prints_no_exec(capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess.run must not be called for a foreign host")

    monkeypatch.setattr(ip.subprocess, "run", boom)
    result = make_result(extra={"cwd": "/some/dir", "host": "boxB"})

    rc = ip.act_on_choice(result, current_host="boxA", demo=False)

    assert rc == 0
    out = capsys.readouterr()
    assert "This session lives on host 'boxB'" in out.err
    assert "pushd /some/dir && claude -r cc-test-session-001" in out.out


def test_act_cc_foreign_host_quotes_cwd(capsys, monkeypatch):
    monkeypatch.setattr(ip.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = make_result(extra={"cwd": "/some dir", "host": "boxB"})

    ip.act_on_choice(result, current_host="boxA", demo=False)

    assert "pushd '/some dir' && claude -r cc-test-session-001" in capsys.readouterr().out


def test_act_cc_demo_bypasses_host_guard(capsys, monkeypatch):
    # demo=True skips the foreign-host early return, so a missing cwd is reached
    # and reported — proving the `not demo and ...` guard was bypassed.
    monkeypatch.setattr(ip.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = make_result(extra={"cwd": "/definitely/not/here", "host": "boxB"})

    rc = ip.act_on_choice(result, current_host="boxA", demo=True)

    assert rc == 1
    assert "Working directory no longer exists" in capsys.readouterr().err


def test_act_cc_missing_cwd_returns_1(capsys, monkeypatch):
    monkeypatch.setattr(ip.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = make_result(extra={"cwd": "/definitely/not/here", "host": "boxA"})

    rc = ip.act_on_choice(result, current_host="boxA", demo=False)

    assert rc == 1
    out = capsys.readouterr()
    assert "Working directory no longer exists: /definitely/not/here" in out.err
    assert "Resume command: claude -r cc-test-session-001" in out.out


def test_act_cc_claude_not_on_path_returns_1(tmp_path, capsys, monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ip.subprocess, "run", fake_run)
    result = make_result(extra={"cwd": str(tmp_path), "host": "boxA"})

    rc = ip.act_on_choice(result, current_host="boxA", demo=False)

    assert rc == 1
    assert "`claude` CLI not found on PATH" in capsys.readouterr().err


def test_act_cc_keyboardinterrupt_returns_130(tmp_path, monkeypatch):
    def fake_run(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(ip.subprocess, "run", fake_run)
    result = make_result(extra={"cwd": str(tmp_path), "host": "boxA"})

    assert ip.act_on_choice(result, current_host="boxA", demo=False) == 130


# ---------------------------------------------------------------------------
# act_on_choice — web providers
# ---------------------------------------------------------------------------

def test_act_claude_opens_browser(capsys, monkeypatch):
    opened = {}
    monkeypatch.setattr(ip.webbrowser, "open",
                        lambda url: opened.setdefault("url", url) or True)
    result = make_result(provider="claude", uuid="u1")

    rc = ip.act_on_choice(result, current_host="boxA")

    assert rc == 0
    assert opened["url"] == "https://claude.ai/chat/u1"
    assert "Opening https://claude.ai/chat/u1" in capsys.readouterr().out


def test_act_chatgpt_opens_browser(capsys, monkeypatch):
    opened = {}
    monkeypatch.setattr(ip.webbrowser, "open",
                        lambda url: opened.setdefault("url", url) or True)
    rc = ip.act_on_choice(make_result(provider="chatgpt", uuid="c1"),
                          current_host="boxA")
    assert rc == 0
    assert opened["url"] == "https://chatgpt.com/c/c1"
    assert "Opening https://chatgpt.com/c/c1" in capsys.readouterr().out


def test_act_unsupported_provider_refuses_no_browser(capsys, monkeypatch):
    # gemini has no resumable URL; act_on_choice must refuse with exit 1 rather
    # than hand webbrowser the "Unknown provider: …" sentinel get_provider_url
    # returns for it.
    def boom(url):
        raise AssertionError("must not open a browser for an unsupported provider")

    monkeypatch.setattr(ip.webbrowser, "open", boom)
    rc = ip.act_on_choice(make_result(provider="gemini", uuid="g1"),
                          current_host="boxA")
    assert rc == 1
    assert "No open action for provider 'gemini'" in capsys.readouterr().err


def test_act_browser_failure_returns_1(capsys, monkeypatch):
    def boom(url):
        raise RuntimeError("no display")

    monkeypatch.setattr(ip.webbrowser, "open", boom)
    rc = ip.act_on_choice(make_result(provider="claude", uuid="u1"),
                          current_host="boxA")
    assert rc == 1
    assert "Could not open browser:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _provider_label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,type_,expected", [
    ("claude-code", "conversation", ("CLAUDE CODE", "fg:#ff8c00 bold")),
    ("chatgpt", "conversation", ("CHATGPT", "fg:ansigreen bold")),
    ("claude", "conversation", ("CLAUDE.AI", "fg:ansicyan bold")),
    ("gemini", "conversation", ("GEMINI", "fg:ansiblue bold")),
    # Unknown provider falls back to the uppercased item type.
    ("weird", "conversation", ("CONVERSATION", "fg:ansimagenta bold")),
])
def test_provider_label(provider, type_, expected):
    assert ip._provider_label(make_result(provider=provider, type=type_)) == expected


# ---------------------------------------------------------------------------
# SearchResult.get_provider_url — URL / resume-command schemes
# ---------------------------------------------------------------------------

def test_url_claude_conversation():
    assert make_result(provider="claude", type="conversation",
                       uuid="u1").get_provider_url() == "https://claude.ai/chat/u1"


def test_url_claude_project():
    assert make_result(provider="claude", type="project",
                       uuid="p1").get_provider_url() == "https://claude.ai/project/p1"


def test_url_chatgpt_conversation():
    assert make_result(provider="chatgpt", type="conversation",
                       uuid="c1").get_provider_url() == "https://chatgpt.com/c/c1"


def test_url_claude_code_with_host():
    r = make_result(provider="claude-code", uuid="s1",
                    extra={"cwd": "/home/u/app", "host": "boxA"})
    assert r.get_provider_url() == "[boxA] pushd /home/u/app && claude -r s1"


def test_url_claude_code_no_host():
    r = make_result(provider="claude-code", uuid="s1", extra={"cwd": "/home/u/app"})
    assert r.get_provider_url() == "pushd /home/u/app && claude -r s1"


def test_url_claude_code_quotes_cwd():
    r = make_result(provider="claude-code", uuid="s1", extra={"cwd": "/home/u/my app"})
    assert r.get_provider_url() == "pushd '/home/u/my app' && claude -r s1"


def test_url_claude_code_default_cwd_when_no_extra():
    # extra=None → cwd defaults to "~", and shlex.quote("~") quotes it ('~' is
    # not a shlex-safe char), so the literal tilde is single-quoted.
    r = make_result(provider="claude-code", uuid="s1", extra=None)
    assert r.get_provider_url() == "pushd '~' && claude -r s1"


def test_url_unknown_provider():
    # Review-on-purpose pin: the upcoming provider-registry refactor may
    # legitimately change how unknown providers are handled.
    assert make_result(provider="gemini",
                       uuid="g1").get_provider_url() == "Unknown provider: gemini"
