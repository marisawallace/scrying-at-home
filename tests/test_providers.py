"""
Contract tests for the provider registry (providers.py).

These pin the exact per-provider descriptor values and the outputs of the pure
helpers that the search CLI, picker, viewer, analytics, and exporter read from.
They double as the registry's contract documentation: every value asserted here
is one a downstream characterization test depends on, so this file is where a
provider's display/action facts are nailed down in one place.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import providers


# (id, badge_label, tui_style, ansi_color, analytics_label, source_label,
#  kind, html_supported, account_slot, resume_argv)
_TABLE = [
    ("claude", "CLAUDE.AI", "fg:ansicyan bold", "", "claude.ai", "claude.ai",
     "web", True, "email", ()),
    ("chatgpt", "CHATGPT", "fg:ansigreen bold", "", "chatgpt", "chatgpt.com",
     "web", True, "email", ()),
    ("claude-code", "CLAUDE CODE", "fg:#ff8c00 bold", "\033[38;5;208m",
     "claude-code", "Claude Code", "local-cli", True, "project-slug",
     ("claude", "-r")),
    ("gemini", "GEMINI", "fg:ansiblue bold", "", "gemini", "gemini",
     "web", False, "email", ()),
]


@pytest.mark.parametrize("row", _TABLE, ids=[r[0] for r in _TABLE])
def test_descriptor_values(row):
    (pid, badge, tui, ansi, analytics_label, source_label, kind,
     html, account_slot, resume_argv) = row
    p = providers.get(pid)
    assert p is not None
    assert p.id == pid
    assert p.badge_label == badge
    assert p.tui_style == tui
    assert p.ansi_color == ansi
    assert p.analytics_label == analytics_label
    assert p.source_label == source_label
    assert p.kind == kind
    assert p.html_supported is html
    assert p.account_slot == account_slot
    assert p.resume_argv == resume_argv


def test_get_unknown_returns_none():
    assert providers.get("nope") is None
    assert providers.get("") is None


# --- resume_cli_args / resume_shell -------------------------------------------

def test_resume_cli_args_claude_code():
    assert providers.resume_cli_args("claude-code", "sid-1") == ["claude", "-r", "sid-1"]


def test_resume_shell_claude_code():
    assert providers.resume_shell("claude-code", "sid-1") == "claude -r sid-1"


@pytest.mark.parametrize("provider", ["claude", "chatgpt", "gemini", "nope"])
def test_resume_empty_for_non_local_cli(provider):
    # Web/unknown providers have no resume CLI.
    assert providers.resume_cli_args(provider, "x") == []
    assert providers.resume_shell(provider, "x") == ""


# --- provider_url -------------------------------------------------------------

def test_url_claude_conversation():
    assert providers.provider_url("claude", "conversation", "u1") == "https://claude.ai/chat/u1"


def test_url_claude_project():
    assert providers.provider_url("claude", "project", "p1") == "https://claude.ai/project/p1"


def test_url_chatgpt():
    assert providers.provider_url("chatgpt", "conversation", "c1") == "https://chatgpt.com/c/c1"


def test_url_claude_code_with_host():
    assert (providers.provider_url("claude-code", "conversation", "s1",
                                   cwd="/home/u/app", host="boxA")
            == "[boxA] pushd /home/u/app && claude -r s1")


def test_url_claude_code_without_host():
    assert (providers.provider_url("claude-code", "conversation", "s1",
                                   cwd="/home/u/app")
            == "pushd /home/u/app && claude -r s1")


def test_url_claude_code_quotes_spaced_cwd():
    assert (providers.provider_url("claude-code", "conversation", "s1",
                                   cwd="/home/u/my app")
            == "pushd '/home/u/my app' && claude -r s1")


def test_url_claude_code_default_cwd_quoted_tilde():
    # No cwd given -> defaults to "~", which shlex.quote renders as '~'.
    assert (providers.provider_url("claude-code", "conversation", "s1")
            == "pushd '~' && claude -r s1")


def test_url_unknown_provider_sentinel():
    # Pinned: unknown providers return the sentinel rather than raising; the
    # picker's own guard refuses to open it.
    assert providers.provider_url("gemini", "conversation", "g1") == "Unknown provider: gemini"
