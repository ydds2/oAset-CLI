"""Full-screen message viewer (P2-4) with a built-in pager (follow-up).

Block-granularity selection in the chat log is deliberate (a Rich-rendered
widget has no character grid), so "copy three lines out of this stack trace"
had no path: the user had to copy the whole block. The viewer suspends the
TUI, hands the terminal back to the OS, and pages through the message with
single-key controls — space/j next, b/k previous, g/G first/last, q/Esc quit —
which restores NATIVE character-level selection for exactly the part the user
wants. Hosts without an interactive console get the fallback: the full text
plus a temp-file path the user can open with anything.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

_KEY_ACTIONS = {
    " ": "next", "\r": "next", "\n": "next", "j": "next",
    "b": "prev", "k": "prev",
    "g": "first", "G": "last",
    "q": "quit", "Q": "quit", "\x1b": "quit",
}


def _wait_for_enter() -> None:
    """Blocking read from the real stdin (separated out so tests can stub it)."""
    try:
        input()
    except EOFError:
        pass


def render_view(title: str, text: str) -> str:
    """The exact payload printed to the terminal (pure, for tests)."""
    from oaset.i18n import t

    rule = "─" * 60
    header = f"{rule}\n{title}\n{t('view_hint')}\n{rule}"
    return f"{header}\n{text}\n{rule}"


def paginate_lines(text: str, height: int) -> list[str]:
    """Split text into pages of at most `height` lines (pure, testable)."""
    height = max(1, height)
    lines = text.splitlines() or [""]
    return ["\n".join(lines[i:i + height])
            for i in range(0, len(lines), height)]


def pager_footer(index: int, total: int) -> str:
    from oaset.i18n import t

    # a single page has no "next": the footer must not advertise keys that
    # do something else (Enter returns to the chat when there's nothing to page)
    hint = t("pager_hint") if total > 1 else t("pager_hint_single")
    return f"\n── {index + 1}/{total} ── {hint}"


def run_pager(pages: list[str], *, read_key: Callable[[], str],
              write: Callable[[str], None]) -> None:
    """The paging loop, injected with I/O so tests can drive it purely.

    Renders the current page, then applies one key action per step:
    next/prev/first/last move the clamped cursor, quit returns. On a
    single-page document Enter ("next") means "done" — the hint above says
    so, and a key that does nothing reads as a broken viewer."""
    index = 0
    total = max(1, len(pages))
    while True:
        write(pages[index] + pager_footer(index, total))
        action = read_key()
        if action == "quit":
            return
        if action == "next":
            if index >= total - 1:
                return
            index = min(index + 1, total - 1)
        elif action == "prev":
            index = max(index - 1, 0)
        elif action == "first":
            index = 0
        elif action == "last":
            index = total - 1
        # unknown actions ("ignore") just re-render the same page


def _read_key_interactive() -> str:
    """One key press, normalised to an action — blocking, cross-platform.

    Windows: ``msvcrt.getwch`` reads the console directly (arrow/function keys
    arrive as a two-read prefix + code pair). POSIX: cbreak mode + a one-char
    read, terminal settings drained afterwards."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "prev", "K": "prev", "P": "next", "M": "next",
                    "G": "first", "O": "last"}.get(code, "ignore")
        return _KEY_ACTIONS.get(ch, _KEY_ACTIONS.get(ch.lower(), "ignore"))
    import select as _select
    import termios  # type: ignore[attr-defined]  # POSIX-only in typeshed
    import tty  # type: ignore[attr-defined]

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)  # type: ignore[attr-defined]
    try:
        tty.setcbreak(fd)  # type: ignore[attr-defined]
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # An ESC can be Esc (quit) OR the first byte of an arrow-key
            # sequence — quitting on ↑/↓ made scrolling close the viewer.
            # Peek briefly: a following "[" + letter is an escape sequence.
            more, _, _ = _select.select([sys.stdin], [], [], 0.03)
            if not more:
                return "quit"
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                code = sys.stdin.read(1)
                return {"A": "prev", "B": "next"}.get(code, "ignore")
            return "ignore"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)  # type: ignore[attr-defined]
    return _KEY_ACTIONS.get(ch, _KEY_ACTIONS.get(ch.lower(), "ignore"))


def _interactive_stdin() -> bool:
    """True when a real console is attached (tests/headless fall back)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


async def view_text(app, title: str, text: str) -> None:
    """Suspend the TUI and page through `text` with native selection.

    The paging loop BLOCKS on keyboard reads, so it runs on a worker thread —
    run directly on the event loop it would freeze the whole TUI (streaming,
    timers, Esc) for as long as the user stays in the pager."""
    payload = render_view(title, text)
    with app.suspend():
        if _interactive_stdin():
            height = max(10, shutil.get_terminal_size().lines - 3)
            pages = paginate_lines(payload, height)

            def write(frame: str) -> None:
                print("\x1b[2J\x1b[H" + frame, end="\n", flush=True)

            await asyncio.to_thread(
                run_pager, pages, read_key=_read_key_interactive, write=write)
        else:
            # Non-interactive host (headless tests, exotic consoles): the
            # original behaviour plus a temp-file handle the user can open
            # with anything.
            fd, name = tempfile.mkstemp(prefix="oaset-view-", suffix=".txt")
            os.close(fd)  # mkstemp leaks the descriptor if we drop it
            path = Path(name)
            path.write_text(payload, encoding="utf-8")
            from oaset.i18n import t

            print(f"{payload}\n\n{t('viewer_saved_to', path=path)}", flush=True)
            await asyncio.to_thread(_wait_for_enter)
    # a child/exit path can leave the console modes wrong — reassert ours
    from oaset.tui._win_mouse_fix import reapply_console_mode

    reapply_console_mode()
