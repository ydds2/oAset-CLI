"""Tool call line: `✓ Used Name (args) · result`.

A plain flowing widget (no box): one bold title line, a dim one-line peek of
the output, and the full output revealed with Ctrl-O / click.

Output formatting: unified diffs get real diff styling (hunks, headers, only
genuine +/- lines), code-looking output gets syntax highlighting, everything
else stays plain text — never markup-parsed, so tool output cannot inject
styles or crash on stray brackets.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from oaset.utils import one_line

# extension -> pygments lexer for the common cases; unknown stays plain
_LEXERS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown",
    ".sh": "bash", ".bash": "bash", ".ps1": "powershell", ".cmd": "batch",
    ".bat": "batch", ".sql": "sql", ".html": "html", ".css": "css",
    ".xml": "xml", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".rb": "ruby", ".php": "php",
    ".ini": "ini", ".cfg": "ini", ".diff": "diff", ".patch": "diff",
}

MAX_HIGHLIGHT_CHARS = 20000


def peek_text(first: str, extra: str, width: int) -> str:
    """The dim `⎿` line: always exactly one row (P2-3).

    Width is the card's real width in terminal cells, so the peek is cut where
    the terminal would cut it — with an explicit ellipsis instead of silently
    wrapping onto a second row that lost its indent (the old behaviour at
    80 columns, where the fold looked like a second output line)."""
    from rich.cells import cell_len

    budget = max(20, width - 6)
    base = one_line(first, budget)
    if extra and cell_len(base) + cell_len(extra) + 6 <= width:
        return "  ⎿ " + base + extra
    return "  ⎿ " + base


def _result_peek(call: str, output: str) -> tuple[str, str]:
    """One useful output line that does not just repeat the call arguments."""
    from oaset.i18n import t

    call_l = (call or "").lower()
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    chosen = ""
    rest = 0
    for i, line in enumerate(lines):
        body = line.lstrip("0123456789 \t")
        token = (body.split() or [""])[0].rstrip("()[],:").lower()
        if token and token in call_l:
            continue
        chosen = one_line(body or line, 48)
        rest = len(lines) - i - 1
        break
    extra = t("tool_extra_lines", n=rest).strip() if rest > 0 else ""
    return chosen, extra


def meaningful_line(text: str) -> str:
    """First line that actually carries content (peek).

    `list_dir` of a shallow directory prints "." as its first line — quoting
    that told the user nothing. Skip dot/blank stubs; fall back to the first
    line when everything is trivial."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if len(stripped) >= 2:
            return line
    return (text or "").splitlines()[:1][0] if (text or "").strip() else "(no output)"


def looks_like_diff(text: str) -> bool:
    """True for a unified diff — requires real hunk/header structure.

    A prose line starting with '-' must NOT be styled as a deletion, so this
    is deliberately strict: `@@` hunks, or a `--- `/`+++ ` header pair, or a
    `diff --git` line.
    """
    lines = text.splitlines()
    if any(ln.startswith("@@") for ln in lines):
        return True
    if any(ln.startswith("diff --git ") for ln in lines):
        return True
    has_old = any(ln.startswith("--- ") for ln in lines)
    has_new = any(ln.startswith("+++ ") for ln in lines)
    return has_old and has_new


def diff_text(text: str):
    """Rich Text with diff-aware styling (headers bold, hunks cyan)."""
    from rich.text import Text

    body = Text()
    for i, line in enumerate(text.splitlines()):
        if i:
            body.append("\n")
        if line.startswith(("+++", "---", "diff --git", "index ")):
            body.append(line, style="bold")
        elif line.startswith("@@"):
            body.append(line, style="bold cyan")
        elif line.startswith("+"):
            body.append(line, style="green")
        elif line.startswith("-"):
            body.append(line, style="red")
        else:
            body.append(line)
    return body


def guess_lexer(summary: str, text: str) -> str | None:
    """Lexer for the output, from the tool summary's file name or content."""
    import re

    for match in re.finditer(r"[\w./\\-]+(\.[A-Za-z0-9]+)", summary or ""):
        lexer = _LEXERS.get(match.group(1).lower())
        if lexer:
            return lexer
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            import json

            json.loads(text)
            return "json"
        except (ValueError, TypeError):
            pass
    if stripped.startswith(("<?xml", "<!doctype", "<html")):
        return "html"
    return None


def render_tool_output(summary: str, text: str, *, highlight: bool = True):
    """Text or Syntax for a finished tool body (diff > code > plain)."""
    if looks_like_diff(text):
        return diff_text(text)
    if highlight and text and len(text) <= MAX_HIGHLIGHT_CHARS:
        lexer = guess_lexer(summary, text)
        if lexer:
            from rich.syntax import Syntax

            return Syntax(text, lexer, word_wrap=True, background_color="default",
                          line_numbers=False, theme="ansi_dark")
    from rich.text import Text

    return Text(text)


class ToolCard(Vertical):
    can_focus = True  # keyboard path to expand/collapse (B6): Tab to card, Enter toggles

    BINDINGS = [
        Binding("enter", "toggle_card", "Toggle", show=False),
        Binding("space", "toggle_card", "Toggle", show=False),
    ]

    DEFAULT_CSS = """
    ToolCard { height: auto; margin: 0; padding: 0; background: ansi_default; }
    ToolCard:focus { text-style: bold; }
    ToolCard .tool-title { color: $foreground; text-style: bold; }
    ToolCard.tool-running .tool-title { color: $secondary; }
    ToolCard.tool-error .tool-title { color: $error; text-style: bold; }
    ToolCard .tool-peek { color: $secondary; }
    ToolCard .tool-output { color: $secondary; margin-bottom: 0; }
    """


    def __init__(self, name: str, summary: str = "", raw_args: str = "",
                 auto_peek: bool = True, restored: bool = False) -> None:
        import time

        self.tool_name = name
        self.raw_args = raw_args  # FULL arguments for the expanded view (P1-7)
        self._summary = one_line(summary, 56)  # keep the call line on one row
        self._started = time.monotonic()
        self._elapsed: float | None = None
        # A card rebuilt from a session file has no elapsed time to report: the
        # transcript does not store it. Measuring from construction printed a
        # confident "· 0.0s" on every restored tool line, which read as "this
        # call was instant" rather than "unknown".
        self.restored = restored
        self._title = Static("", classes="tool-title", markup=False)
        self._peek = Static("", classes="tool-peek", markup=False)
        self._body = Static("", classes="tool-output", markup=False)
        self.raw_output = ""
        self._streamed: list[str] = []
        self._streamed_chars = 0
        self._partial_line = ""  # incremental flush: never re-scan history
        self._finished = False
        self._stream_dirty = False
        self.expanded = False
        self._spin = 0
        # auto_peek=False: activity-area mode — the card enters the flow as a
        # single collapsed line (the activity line owned the live state); the
        # peek and body stay behind click / ctrl+o.
        self.auto_peek = auto_peek
        super().__init__(self._title, self._peek, self._body, classes="tool-running")
        self._render_title(running=True)
        self._peek.display = False
        self._body.display = False

    def _display_name(self) -> str:
        from oaset.utils import display_tool_name

        return display_tool_name(self.tool_name)

    def _render_title(self, running: bool, is_error: bool = False) -> None:
        import time

        from oaset.i18n import t

        label = t("tool_used", name=self._display_name())
        if running:
            try:
                cfg = getattr(self.app, "cfg", None)
                reduce_motion = bool(cfg is not None and cfg.ui.reduce_motion)
            except Exception:
                reduce_motion = False
            if reduce_motion:
                from oaset.tui.glyphs import STATIC

                spin = STATIC["tool_running"]
            else:
                from oaset.tui.glyphs import spinner_frame

                spin = spinner_frame(self.app, self._spin)
            # live elapsed: a long build/搜索 with no output must not look dead
            run_secs = int(time.monotonic() - self._started)
            base = f"{spin} {label} ({self._summary})" if self._summary else f"{spin} {label}"
            if run_secs >= 3:
                base += f" · {run_secs}s"
            self.title_text = base
        else:
            from oaset.tui.glyphs import STATIC

            mark = STATIC["error"] if is_error else STATIC["done"]
            elapsed = (f" · {self._elapsed:.1f}s" if self._elapsed is not None else "")
            result = f" · {self._summary}" if self._summary else ""
            self.title_text = f"{mark} {label}{result}{elapsed}"
        self._title.update(self.title_text)

    def spin_tick(self) -> None:
        if self.has_class("tool-running"):
            self._spin += 1
            self._render_title(running=True)

    # Streaming peek is bounded: shell tools stream up to MAX_CAPTURE
    # (200 KB) and the old flush joined+scanned the WHOLE history on every
    # newline (O(n²)), freezing the UI on chatty builds. Keep a sliding
    # window of the most recent output; the full text still lands in
    # raw_output via finish().
    STREAM_PEEK_CHARS = 16_000

    def append_stream(self, chunk: str) -> None:
        self.raw_output += chunk
        self._streamed.append(chunk)
        self._streamed_chars += len(chunk)
        while self._streamed_chars > self.STREAM_PEEK_CHARS and len(self._streamed) > 1:
            self._streamed_chars -= len(self._streamed.pop(0))
        self._stream_dirty = True

    def flush(self) -> None:
        if not self._stream_dirty:
            return
        # Incremental: only the newest chunk can complete a line; the
        # partial accumulator replaces the full-history rescan.
        chunk = self._streamed[-1] if self._streamed else ""
        tail = self._partial_line + chunk
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if tail.endswith(("\n", "\r")):
            latest, self._partial_line = (lines[-1] if lines else ""), ""
        else:
            latest = lines[-1] if lines else tail.strip()
            self._partial_line = tail.rsplit("\n", 1)[-1]
        self._peek.update(peek_text(latest, "", self.size.width or 80))
        self._peek.display = True
        if self.expanded:
            self._body.update("".join(self._streamed))
        self._stream_dirty = False

    def finish(self, output: str, is_error: bool = False) -> None:
        import time

        if self._finished:
            return  # denied calls emit tool_denied AND tool_end: finish once
        self._finished = True

        self.remove_class("tool-running")
        self.add_class("tool-error" if is_error else "tool-ok")
        # Restored cards report no duration rather than a fabricated 0.0s.
        self._elapsed = None if self.restored else time.monotonic() - self._started
        self.raw_output = output or "(no output)"
        call = one_line(self._summary, 40)
        result, extra = _result_peek(call, self.raw_output)
        peek = " ".join(p for p in (result, extra) if p)
        self._summary = one_line(
            " · ".join(p for p in (call, peek) if p).strip(" ·"), 88)
        self._render_title(running=False, is_error=is_error)
        self._body.update(self._render_body())
        self._peek.display = False

    # The expanded body renders at most this much text. A shell tool streams
    # up to MAX_CAPTURE (200 KB); rendering all of it made one expand click
    # freeze the UI. raw_output keeps everything — selection copy and
    # /copy tools still see the full text.
    MAX_BODY_CHARS = 40_000

    def _render_body(self):
        """Expanded body: the FULL call arguments above the output (P1-7) —
        the title line truncates arguments to keep one row, the expanded view
        must not hide what the model actually sent."""
        text = self.raw_output
        if len(text) > self.MAX_BODY_CHARS:
            from oaset.i18n import t

            text = text[: self.MAX_BODY_CHARS] + "\n" + t(
                "tool_body_clamped", n=len(self.raw_output) - self.MAX_BODY_CHARS)
        body = render_tool_output(self._summary, text)
        if not self.raw_args:
            return body
        from rich.console import Group
        from rich.text import Text

        args = Text(f"args: {self.raw_args}", style="dim")
        return Group(args, Text(""), body)

    def action_toggle_card(self) -> None:
        self.toggle_expand()

    def toggle_expand(self) -> None:
        self.expanded = not self.expanded
        self._peek.display = not self.expanded and bool(self.raw_output)
        if self.expanded:
            self._body.update(self._render_body())
        self._body.display = self.expanded

    def copy_text(self) -> str:
        """Selection copy: the call line plus the full (untruncated) output."""
        parts = [self.title_text]
        if self.tool_name and self.tool_name not in self.title_text:
            parts.append(self.tool_name)
        if self.raw_output:
            parts.append(self.raw_output)
        return "\n".join(parts)

    def content_offset_rows(self) -> int:
        """Visual rows above the output body (title + peek line).

        Line-range copy maps screen rows onto copy_text() lines; the title and
        the live peek line sit above the output, so a drag that starts on the
        output must not count them.
        """
        rows = 0
        for child in (self._title, self._peek):
            try:
                if child.display and child.region:
                    rows += int(child.region.height)
            except Exception:
                pass
        return rows

    def on_click(self) -> None:
        chat = getattr(self.app, "chat", None)
        if chat is not None and chat.dragging_recently():
            return  # that was a selection drag, not a click
        self.toggle_expand()


class ToolGroupCard(Vertical):
    """One turn, MANY tool calls → ONE collapsed card (anti-flood,
    parity). A long agentic turn used to paste N two-line cards into the flow;
    now a multi-call turn ends as a single summary line —

        ⏺ 12 次工具调用 · read_file ×9 · grep ×2 · 3.2s

    which expands (click / Enter) to one row per call: mark, name, argument
    summary and the output peek. Single/double-call turns keep the original
    per-call ToolCard."""

    can_focus = True

    BINDINGS = [
        Binding("enter", "toggle_group", "Toggle", show=False),
        Binding("space", "toggle_group", "Toggle", show=False),
    ]

    DEFAULT_CSS = """
    ToolGroupCard { height: auto; margin: 0; padding: 0; background: ansi_default; }
    ToolGroupCard .tool-title { color: $foreground; }
    ToolGroupCard:focus { text-style: bold; }
    ToolGroupCard .group-row { color: $secondary; }
    """

    MAX_KINDS = 3  # kinds named in the collapsed line before "…"

    def __init__(self, runs: list) -> None:
        from collections import Counter

        from oaset.i18n import t

        self.runs = runs
        self.expanded = False
        total = sum(r.elapsed or 0 for r in runs if r.elapsed)
        errors = sum(1 for r in runs if r.is_error)
        kinds = " · ".join(
            f"{name} ×{n}" for name, n in Counter(r.name for r in runs).most_common(self.MAX_KINDS)
        )
        extra = len(Counter(r.name for r in runs)) - self.MAX_KINDS
        if extra > 0:
            kinds += f" …+{extra}"
        err = f" ✗{errors}" if errors else ""
        title = f"⏺ {t('tool_group_calls', n=len(runs))} · {kinds} · {total:.1f}s{err}"
        self._title = Static(title, classes="tool-title", markup=False)
        self._body = Static(self._render_rows(), classes="group-row", markup=False)
        self._body.display = False
        super().__init__(self._title, self._body, classes="tool-ok")

    def _render_rows(self) -> str:
        from oaset.utils import one_line

        lines = []
        for r in self.runs:
            mark = "✗" if r.is_error else "✓"
            elapsed = f" · {r.elapsed:.1f}s" if r.elapsed else ""
            peek = one_line(r.result or "", 60)
            lines.append(f"{mark} {r.name} {r.summary}{elapsed}")
            if peek:
                lines.append(f"  └ {peek}")
        return "\n".join(lines)

    def action_toggle_group(self) -> None:
        self.expanded = not self.expanded
        self._body.display = self.expanded

    def toggle_expand(self) -> None:
        self.action_toggle_group()

    def on_click(self) -> None:
        chat = getattr(self.app, "chat", None)
        if chat is not None and chat.dragging_recently():
            return  # selection drag, not a click
        self.action_toggle_group()

    def copy_text(self) -> str:
        title = str(getattr(self._title, "renderable", "") or "")
        return "\n".join([title] + [
            f"{r.name} {r.summary}\n{r.result or ''}" for r in self.runs
        ])
