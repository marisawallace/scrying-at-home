"""
Interactive arrow-key picker for search results.

After full_text_search_chats_archive.py finds results, the picker lets the user
arrow up/down through them and press Enter to resume/open the selected entry —
no mouse, no copy-paste.

Action on Enter:
  - claude-code on current host: chdir to cwd, exec `claude -r <uuid>`.
  - claude-code on a different host: print the resume command (cannot be run
    locally — left for the user to ssh and run).
  - claude.ai / chatgpt: open the URL in a browser.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import webbrowser
from typing import List

import vendor_loader  # noqa: F401 — side-effect: prepend vendor/ to sys.path
from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.layout.containers import ScrollOffsets


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _provider_label(result) -> tuple[str, str]:
    """Returns (label, prompt_toolkit style string)."""
    if result.provider == "claude-code":
        return "CLAUDE CODE", "fg:#ff8c00 bold"
    if result.provider == "chatgpt":
        return "CHATGPT", "fg:ansigreen bold"
    if result.provider == "claude":
        return "CLAUDE.AI", "fg:ansicyan bold"
    if result.provider == "gemini":
        return "GEMINI", "fg:ansiblue bold"
    return result.type.upper(), "fg:ansimagenta bold"


def _highlight_query(text: str, query: str, exact: bool) -> FormattedText:
    """Return FormattedText with query terms highlighted."""
    terms = [query] if exact else [w for w in query.split() if w]
    if not terms:
        return FormattedText([("", text)])

    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    out: list[tuple[str, str]] = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            out.append(("", text[last : m.start()]))
        out.append(("fg:ansiyellow bold", m.group()))
        last = m.end()
    if last < len(text):
        out.append(("", text[last:]))
    return FormattedText(out)


def _render_result(result, query: str, exact: bool, selected: bool, current_host: str, demo: bool = False) -> tuple[FormattedText, int]:
    """
    Render one result as a multi-line FormattedText block.
    Returns (fragments, line_count) so the picker can drive scroll-offset.
    """
    fragments: list[tuple[str, str]] = []
    line_count = 0

    # Mark the selected entry's title as the focus point so the Window scrolls
    # to keep it visible. Combined with scroll_offsets.bottom = block height,
    # the entire block stays on-screen.
    if selected:
        fragments.append(("[SetCursorPosition]", ""))

    # Caret on the title line only; subsequent lines align under it with a
    # blank 2-col gutter.
    title_gutter_style = "fg:ansibrightgreen bold"
    body_gutter = "  "

    def line(parts, *, is_title=False):
        nonlocal line_count
        if is_title and selected:
            fragments.append((title_gutter_style, "▶ "))
        else:
            fragments.append(("", body_gutter))
        fragments.extend(parts)
        fragments.append(("", "\n"))
        line_count += 1

    label, label_style = _provider_label(result)
    line([(label_style, f"[{label}] "), ("bold", result.name)], is_title=True)

    if result.provider == "claude-code":
        extra = result.extra or {}
        host = extra.get("host", "")
        meta = [("fg:#888888", f"Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]}")]
        if host:
            meta.append(("fg:#888888", " | "))
            host_style = "fg:#ff8c00" if demo or (current_host and host == current_host) else "fg:#888888"
            meta.append((host_style, host))
        line(meta)
    else:
        line([("fg:#888888", f"Created: {result.created_at[:10]} | Updated: {result.updated_at[:10]} | {result.email}")])

    if result.provider == "claude-code":
        extra = result.extra or {}
        cwd = os.path.expanduser(extra.get("cwd", "~"))
        line([("fg:#888888", cwd)])

    line([("fg:#888888", f"Score: {result.total_score:.1f} | Matches: {len(result.matches)}")])

    if result.matches:
        line([])  # blank gap before snippets
        for j, match in enumerate(result.matches[:2], 1):
            parts: list[tuple[str, str]] = [("fg:#888888", f"  {j}. ")]
            for style, chunk in _highlight_query(match.text, query, exact):
                parts.append((style, chunk))
            line(parts)
        if len(result.matches) > 2:
            line([("fg:#888888", f"  ... and {len(result.matches) - 2} more match(es)")])

    return FormattedText(fragments), line_count


class ResultPicker:
    def __init__(self, results: list, query: str, exact: bool, current_host: str, demo: bool = False):
        # Best-first ordering: cursor starts on the best match.
        self.results = list(results)
        self.query = query
        self.exact = exact
        self.current_host = current_host
        self.demo = demo
        self.index = 0
        self.action = None  # "resume" | "view" | "view-html" | None
        self._selected_block_height = 1
        # Cache the unselected render of each result. The selected one is
        # re-rendered on every frame (only one entry, cheap); everything else
        # is reused across keystrokes so navigation is O(1) instead of O(n).
        self._unselected_cache: list[tuple[FormattedText, int] | None] = [None] * len(self.results)

    def _get_unselected(self, i):
        cached = self._unselected_cache[i]
        if cached is None:
            cached = _render_result(self.results[i], self.query, self.exact, False, self.current_host, self.demo)
            self._unselected_cache[i] = cached
        return cached

    def _render(self):
        blocks: list[tuple[str, str]] = []
        self._selected_block_height = 1
        for i, r in enumerate(self.results):
            if i == self.index:
                block, height = _render_result(r, self.query, self.exact, True, self.current_host, self.demo)
                self._selected_block_height = height
            else:
                block, _ = self._get_unselected(i)
            blocks.extend(block)
            if i < len(self.results) - 1:
                # Two blank lines between results.
                blocks.append(("", "\n\n"))
        blocks.append(("", "\n"))
        blocks.append(
            ("fg:ansibrightblack",
             f"  ↑/↓ navigate  •  Enter resume/open  •  v view  •  h html  •  q/Esc cancel  •  {self.index + 1}/{len(self.results)}\n")
        )
        return FormattedText(blocks)

    def run(self):
        if not self.results:
            return None

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _(event):
            self.index = (self.index - 1) % len(self.results)

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self.index = (self.index + 1) % len(self.results)

        @kb.add("home")
        @kb.add("g")
        def _(event):
            self.index = 0

        @kb.add("end")
        @kb.add("G")
        def _(event):
            self.index = len(self.results) - 1

        @kb.add("pageup")
        def _(event):
            self.index = max(0, self.index - 5)

        @kb.add("pagedown")
        def _(event):
            self.index = min(len(self.results) - 1, self.index + 5)

        @kb.add("enter")
        def _(event):
            self.action = "resume"
            event.app.exit(result=self.results[self.index])

        @kb.add("v")
        def _(event):
            self.action = "view"
            event.app.exit(result=self.results[self.index])

        @kb.add("h")
        def _(event):
            # HTML rendering is supported for the claude.ai, chatgpt, and
            # claude-code providers; ignore the key for others rather than
            # exiting.
            if self.results[self.index].provider in ("claude", "chatgpt", "claude-code"):
                self.action = "view-html"
                event.app.exit(result=self.results[self.index])

        @kb.add("q")
        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            self.action = None
            event.app.exit(result=None)

        # show_cursor must be True for prompt_toolkit to honor [SetCursorPosition]
        # for scroll tracking. always_hide_cursor on the Window keeps the actual
        # terminal cursor invisible, while scroll-to-cursor still works.
        control = FormattedTextControl(self._render, focusable=True, show_cursor=True)
        # scroll_offsets.bottom keeps `block_height - 1` lines visible below the
        # cursor (which sits at the selected title) — i.e., the entire selected
        # block stays on-screen even when scrolling near the bottom of the list.
        window = Window(
            content=control,
            always_hide_cursor=True,
            wrap_lines=True,
            scroll_offsets=ScrollOffsets(
                top=0,
                bottom=lambda: max(0, self._selected_block_height + 2),
            ),
        )
        layout = Layout(HSplit([window]))

        app = Application(
            layout=layout,
            key_bindings=kb,
            full_screen=True,
            mouse_support=False,
        )
        # Default 0.1s wait after ESC (to disambiguate from arrow-key
        # sequences like ESC[A -- necessary for up/down nav).
        app.ttimeoutlen = 0.1
        return app.run()


def act_on_choice(result, current_host: str, demo: bool = False) -> int:
    """Take the action for the chosen result. Returns process exit code (0 on success)."""
    if result.provider == "claude-code":
        extra = result.extra or {}
        cwd = os.path.expanduser(extra.get("cwd", "~"))
        host = extra.get("host", "")

        if not demo and current_host and host and host != current_host:
            # Different host — can't resume here. Print the command for the user.
            print(f"\nThis session lives on host '{host}'. Resume there with:\n", file=sys.stderr)
            print(f"  pushd {shlex.quote(cwd)} && claude -r {result.uuid}\n")
            return 0

        if not os.path.isdir(cwd):
            print(f"\nWorking directory no longer exists: {cwd}", file=sys.stderr)
            print(f"Resume command: claude -r {result.uuid}")
            return 1

        # Run claude as a child in `cwd` rather than chdir-ing this process.
        # The parent shell's working directory is unaffected, so when `claude`
        # exits the user is back where they ran the search — same end-state as
        # `pushd ... && claude -r ... && popd`, without needing the shell at all.
        try:
            proc = subprocess.run(["claude", "-r", result.uuid], cwd=cwd)
            return proc.returncode
        except FileNotFoundError:
            print("Error: `claude` CLI not found on PATH.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130

    # claude.ai / chatgpt / gemini — open the URL.
    url = result.get_provider_url()
    print(f"Opening {url}")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Could not open browser: {e}", file=sys.stderr)
        return 1
    return 0


def view_choice(result, fmt: str = "markdown") -> int:
    """Launch view_conversation on the chosen result, then return."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "view_conversation.py")
    cmd = [sys.executable, script, result.uuid, "--format", fmt]
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except FileNotFoundError:
        print("Error: view_conversation.py not found.", file=sys.stderr)
        return 1


def pick_and_act(results: list, query: str, exact: bool, current_host: str, demo: bool = False) -> int:
    if not results:
        print("No results to pick from.")
        return 0
    picker = ResultPicker(results, query, exact, current_host, demo)
    while True:
        chosen = picker.run()
        if chosen is None:
            return 0
        if picker.action == "view":
            view_choice(chosen)
            # Re-enter the picker so the user can keep browsing.
            continue
        if picker.action == "view-html":
            view_choice(chosen, fmt="html")
            # Re-enter the picker so the user can keep browsing.
            continue
        # action == "resume": Enter — fire the resume/open action and exit.
        return act_on_choice(chosen, current_host, demo)
