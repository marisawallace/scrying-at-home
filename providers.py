"""
Provider/source registry: the single home for "what a provider is".

Each supported chat source — claude.ai, ChatGPT, Claude Code, (Gemini), and
soon OpenAI Codex — has one `Provider` descriptor here, plus a few pure
functions that read it. Before this module, provider identity (display labels,
colours, the resume command, URL schemes, whether HTML view is supported) was
spelled out as `if provider == "..."` branches scattered across the search CLI,
the picker, the viewer, analytics, and the exporter. Centralising it means a
new provider slots in by adding ONE descriptor entry (+ a parser module),
rather than threading a branch through every layer.

Leaf-module discipline (mirrors claude_code_parser): this module imports only
the stdlib and is imported directly by everything else, INCLUDING search_index
(which must not import full_text_search_chats_archive — it runs as __main__).
So every function here takes PRIMITIVES (provider id, uuid, cwd, host,
item_type), never a SearchResult. Display constants that also live elsewhere
(e.g. the claude-code ANSI orange, == full_text_search_chats_archive.Colors.ORANGE)
are duplicated here as literals rather than imported, to keep this a leaf.

This module carries DISPLAY/ACTION facts only — never text extraction. That
separation is load-bearing: search_index.SCHEMA_SOURCE_FILES hashes the
extractor modules so an extraction change invalidates the index, and this
module is deliberately NOT in that list (a label edit must not force a
multi-hundred-MB reindex).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Provider:
    """Everything the UI/CLI layers need to know about one chat source.

    The three label fields are deliberately distinct — the same provider is
    rendered differently in different contexts, and each rendering is pinned by
    a characterization test, so they must not be collapsed into one field.
    """
    id: str               # canonical id, also the SearchResult.provider value
    badge_label: str      # picker + print_results header, e.g. "CLAUDE CODE"
    tui_style: str        # picker prompt_toolkit style, e.g. "fg:#ff8c00 bold"
    ansi_color: str       # print_results badge colour override; "" == keep the
                          # type-derived colour (cyan conv / magenta project)
    analytics_label: str  # analytics + export PROVIDER_LABELS, e.g. "claude.ai"
    source_label: str     # viewer _SOURCE_LABELS / HTML topbar, e.g. "Claude Code"
    kind: str             # "web" (has a URL, opened in a browser) |
                          # "local-cli" (has cwd/host, resumed via a CLI)
    html_supported: bool  # may the conversation be rendered to HTML locally?
    browser_openable: bool  # is there a thread URL to open in a browser? A
                          # distinct axis from html_supported: a local-cli
                          # transcript (claude-code, codex) renders to HTML but
                          # has no browsable URL; gemini has neither.
    account_slot: str     # meaning of the items.email column / SearchResult.email:
                          # "email" (web account) | "project-slug" (local-cli)
    resume_argv: tuple[str, ...] = field(default=())
                          # leading argv of the resume command (local-cli only),
                          # e.g. ("claude", "-r"); the session id is appended.
                          # Empty for web providers (no resume CLI).


# The registry. Ordering is cosmetic. Adding a provider = adding one entry.
_PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        id="claude",
        badge_label="CLAUDE.AI",
        tui_style="fg:ansicyan bold",
        ansi_color="",
        analytics_label="claude.ai",
        source_label="claude.ai",
        kind="web",
        html_supported=True,
        browser_openable=True,
        account_slot="email",
    ),
    "chatgpt": Provider(
        id="chatgpt",
        badge_label="CHATGPT",
        tui_style="fg:ansigreen bold",
        ansi_color="",
        analytics_label="chatgpt",
        source_label="chatgpt.com",
        kind="web",
        html_supported=True,
        browser_openable=True,
        account_slot="email",
    ),
    "claude-code": Provider(
        id="claude-code",
        badge_label="CLAUDE CODE",
        tui_style="fg:#ff8c00 bold",
        ansi_color="\033[38;5;208m",  # == full_text_search_chats_archive.Colors.ORANGE
        analytics_label="claude-code",
        source_label="Claude Code",
        kind="local-cli",
        html_supported=True,
        browser_openable=False,
        account_slot="project-slug",
        resume_argv=("claude", "-r"),
    ),
    # Known but unsupported: we surface Gemini results other tooling may have
    # produced (labels/colour), but there is no ingest and no resumable thread
    # URL, so html_supported is False and there is no resume_argv. See
    # the Gemini deferral note in the repo's planning docs.
    "gemini": Provider(
        id="gemini",
        badge_label="GEMINI",
        tui_style="fg:ansiblue bold",
        ansi_color="",
        analytics_label="gemini",
        source_label="gemini",
        kind="web",
        html_supported=False,
        browser_openable=False,
        account_slot="email",
    ),
}


def get(provider_id: str) -> Optional[Provider]:
    """The descriptor for `provider_id`, or None for an unknown provider.

    Callers keep their existing fallbacks for the None case (analytics/viewer
    pass the raw id through; the picker falls back to the result type), so an
    unrecognised provider degrades gracefully rather than raising.
    """
    return _PROVIDERS.get(provider_id)


def all_providers() -> dict[str, Provider]:
    """All registered providers, keyed by id (a copy; callers must not mutate).

    Lets a layer derive its own context-specific label map in one comprehension
    (e.g. {id: p.analytics_label}) instead of hand-maintaining a parallel dict.
    """
    return dict(_PROVIDERS)


def resume_cli_args(provider: str, session_id: str) -> list[str]:
    """The argv to resume a local-CLI session, e.g. ["claude", "-r", "<id>"].

    Empty list for web (or unknown) providers, which have no resume CLI. This
    is the one provider-variant token shared by every resume-command site; the
    surrounding `cd`/`pushd`/`[host]` wrappers stay at the call sites.
    """
    p = _PROVIDERS.get(provider)
    if p is None or not p.resume_argv:
        return []
    return [*p.resume_argv, session_id]


def resume_shell(provider: str, session_id: str) -> str:
    """The resume command as a shell string, e.g. "claude -r <id>".

    Shell-quoted argv join; for the current providers the tokens need no
    quoting, so this is the bare command. "" for web/unknown providers.
    """
    return " ".join(shlex.quote(a) for a in resume_cli_args(provider, session_id))


def provider_url(provider: str, item_type: str, uuid: str,
                 cwd: str = "~", host: str = "") -> str:
    """The browser URL (web) or resume command (local-cli) for one item.

    Replaces the body of SearchResult.get_provider_url; switches on the
    provider id internally and returns the exact strings every call site and
    JSON consumer already depends on, including the "Unknown provider: <id>"
    sentinel for anything not in the registry.
    """
    if provider == "claude":
        if item_type == "conversation":
            return f"https://claude.ai/chat/{uuid}"
        return f"https://claude.ai/project/{uuid}"
    if provider == "chatgpt":
        return f"https://chatgpt.com/c/{uuid}"
    if provider == "claude-code":
        prefix = f"[{host}] " if host else ""
        return f"{prefix}pushd {shlex.quote(cwd)} && {resume_shell(provider, uuid)}"
    return f"Unknown provider: {provider}"
