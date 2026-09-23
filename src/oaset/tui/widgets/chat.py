"""Chat log — a flowing transcript.

Everything is plain flowing text separated by blank lines: `›` user
messages, a two-line thinking status, plain answers, dim
`✓ Used Name` tool lines, and a plain welcome block as the very first element.
The input row keeps `❯`; sent turns use a lighter mark so the two never collide.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, cast

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Label, Markdown, Static

# Built with chr() so no shell/heredoc layer can turn the escape into a real
# newline inside a string literal (that has bitten this file more than once).
BLANK_LINE = chr(10) + chr(10)

if TYPE_CHECKING:
    from textual.dom import DOMNode
    from textual.widget import Widget

    from oaset.tui.widgets.tool_card import ToolCard

_md_parser: Any = None


def _parser() -> Any:
    """markdown-it parser (already a Textual dependency) — block boundaries."""
    global _md_parser
    if _md_parser is None:
        try:
            from markdown_it import MarkdownIt

            _md_parser = MarkdownIt("commonmark")
        except Exception:  # pragma: no cover - parser always ships with Textual
            _md_parser = False
    return _md_parser or None


def split_committable(text: str) -> tuple[str, str]:
    """Split streamed markdown into (confirmed_prefix, live_tail).

    P2-1 (incremental commitment): everything except the LAST
    top-level block is already final — a block is only complete once another
    block follows it — so it can be rendered once and never re-parsed. The
    tail is what still changes.

    markdown-it gives real block boundaries, so a fenced code block, a table
    or a *loose* list is never cut in half (splitting on blank lines alone
    would shred all three).
    """
    if not text.strip():
        return "", text
    md = _parser()
    if md is None:
        return "", text
    try:
        tokens = md.parse(text)
    except Exception:
        return "", text
    spans: list[tuple[int, int]] = []
    for tok in tokens:
        # level 0 = top-level; nesting 1 (open) or 0 (leaf like fence/hr)
        if tok.level == 0 and tok.map and tok.nesting >= 0 and tok.type != "inline":
            spans.append((tok.map[0], tok.map[1]))
    if len(spans) < 2:
        return "", text
    lines = text.splitlines(keepends=True)
    boundary = spans[-2][1]
    if boundary <= 0 or boundary > len(lines):
        return "", text
    prefix = "".join(lines[:boundary])
    if not prefix.strip():
        return "", text
    return prefix, "".join(lines[boundary:])


class ThinkingBlock(Static):
    can_focus = True  # keyboard parity with click/Ctrl+O (B6): Tab to block, Enter toggles

    BINDINGS = [
        Binding("enter", "toggle_block", "Toggle", show=False),
        Binding("space", "toggle_block", "Toggle", show=False),
    ]

    """Reasoning, NEVER dumped verbatim while it streams.

    While the model thinks this is EXACTLY two lines, refreshed live:
    status (`✻ 思考中… Ns · M 字`) plus the newest reasoning line. Raw
    reasoning never floods the screen. At turn end it collapses to a
    one-line summary; Ctrl+O / click expands the full text, and copy_text
    keeps it all.
    """

    def __init__(self, restored: bool = False) -> None:
        self._buffer: list[str] = []
        self._streaming = False
        self.collapsed = False
        self._dirty = False
        self._last_render = 0.0
        self._started = time.monotonic()
        # A restored block was not streamed in this process, so it has no
        # duration to report: measuring from construction printed "思考 0s" on
        # every restored turn, which reads as "the model barely thought".
        self.restored = restored
        self.render_count = 0  # P2-2 regression: deltas must not render 1:1
        super().__init__("", classes="thinking-text", markup=False)
        self.display = False

    def begin(self) -> None:
        """Show the two-line live status even before any reasoning tokens."""
        self._streaming = True
        self.display = True
        self._refresh_text()

    THROTTLE = 0.25  # seconds between status-line re-renders (cheap now)

    def push(self, delta: str) -> None:
        self._buffer.append(delta)
        self.display = True
        self._streaming = True
        self._dirty = True
        # P2-2: reasoning arrives per token; re-rendering on every delta was a
        # CPU spike with no visual benefit (the eye cannot read faster than
        # ~12 updates/s). Batch to one render per THROTTLE window.
        now = time.monotonic()
        if now - self._last_render >= self.THROTTLE:
            self._last_render = now
            self._refresh_text()

    def flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self._last_render = time.monotonic()
            self._refresh_text()

    def copy_text(self) -> str:
        return "".join(self._buffer)

    def finish(self) -> None:
        self._streaming = False
        if not self._buffer:
            self.display = False
            self.update("")
            return
        self.display = True
        self.collapsed = True
        self._refresh_text()

    def toggle(self) -> None:
        if not self._buffer or self._streaming:
            return
        self.collapsed = not self.collapsed
        self._refresh_text()

    def action_toggle_block(self) -> None:
        self.toggle()

    def on_click(self) -> None:
        chat = getattr(self.app, "chat", None)
        if chat is not None and chat.dragging_recently():
            return  # that was a selection drag, not a click
        self.toggle()

    def _refresh_text(self) -> None:
        from oaset.i18n import t
        from oaset.tui.glyphs import ripple_frame

        self.render_count += 1
        body = "".join(self._buffer)
        elapsed = int(time.monotonic() - self._started)
        if self._streaming:
            # oMotion: thinking is a RIPPLE — a different motion language
            # from the busy dial, phase-locked across blocks by wall clock.
            # reduce_motion and the legacy raster-font class get the static
            # "o" instead — the ripple glyphs (∘ ◌) garble there just like
            # the dial's ◔◑◕ do, and stillness is the whole point of the
            # accessibility setting.
            try:
                static_motion = (self.app.has_class("legacy")
                                 or bool(self.app.cfg.ui.reduce_motion))
            except Exception:
                static_motion = False
            phase = "◎" if static_motion else ripple_frame(time.monotonic())
            # Always two lines while the turn is live: status + latest thought
            # (or a waiting hint when the model has not streamed reasoning yet).
            n = len(body)
            tail = ""
            for line in reversed(body.splitlines()):
                line = line.strip()
                if line:
                    from oaset.utils import one_line as _one_line

                    # CELL-aware, not char-aware: 72 hanzi = 144 cells, which
                    # wrapped and broke the "exactly two rows while streaming"
                    # contract (CJK repro: block height hit 4)
                    budget = max(24, (self.size.width or 72) - 2)
                    tail = _one_line(line, budget)
                    break
            if not tail:
                tail = t("thinking_waiting")
            self.update(
                f"{phase} {t('thinking')}… {elapsed}s · {n} {t('chars_short')}\n  {tail}"
            )
        elif self.collapsed:
            if self.restored:
                self.update("⏵ " + t("thought_folded_no_time", n=len(body)))
            else:
                self.update("⏵ " + t("thought_folded", n=len(body), secs=elapsed))
        else:
            self.update("● " + body)


class AssistantTurn(Vertical):
    DEFAULT_CSS = """
    AssistantTurn { height: auto; margin: 0; padding: 0; }
    AssistantTurn Markdown { padding: 0; margin: 0; }
    AssistantTurn .assistant-md { padding: 0; margin: 0; }
    AssistantTurn .assistant-meta { height: auto; margin: 0; padding: 0; }
    """

    """One assistant reply: thinking status + incrementally committed markdown.

    There is NO standalone bullet row (output-density parity with
    Claude Code) — the markdown starts on its own first line. A ``●`` prefix
    inside the markdown source would break fence/heading/table parsing, which
    is why it never joins the source.

    P2-1: finished top-level blocks are mounted once and never touched again;
    only the unfinished tail is re-parsed on each flush. The old behaviour
    re-fed the ENTIRE answer to the Markdown widget every 120 ms, so a long
    reply cost O(length) parsing per frame.
    """

    def __init__(self, *, live: bool = True) -> None:
        self.thinking = ThinkingBlock(restored=not live)
        # Live tail is a Static. Finished blocks mount as Markdown once
        # (split_committable); finish() only paints the leftover tail.
        self.live = Static("", classes="assistant-md", markup=False)
        self._rich = Markdown("", classes="assistant-md")
        self._rich.display = False
        self.markdown: Static | Markdown = self.live
        self.meta = Label("", classes="assistant-meta", markup=False)
        self._meta_text = ""
        self._full: list[str] = []  # every delta (copy/meta source of truth)
        self._tail: list[str] = []  # not yet committed
        self._dirty = False
        self._done = False
        self._last_render_at = 0.0
        self._final_markdown = False
        self.parse_calls = 0
        self.parse_chars = 0
        self._committed: list[Markdown] = []
        self._committed_src: list[str] = []
        super().__init__(self.thinking, self.live, self._rich, self.meta,
                         classes="assistant")
        if live:
            self.thinking.begin()

    def reset_thinking(self) -> None:
        """Retry/fallback companion to reset_stream: the dead attempt's
        reasoning must go too — the persisted message keeps only the
        winner's reasoning, and copy/expand must not carry stale thought."""
        self.thinking._buffer.clear()
        self.thinking._started = time.monotonic()
        self.thinking._dirty = True

    def push_content(self, delta: str) -> None:
        self._full.append(delta)
        self._tail.append(delta)
        self._dirty = True

    def reset_stream(self) -> None:
        """Retry/fallback: the provider RESTARTS the answer, so the streamed
        text so far is a dead prefix — hide it instead of letting the
        regenerated answer append to it. Without this the screen showed the
        dead attempt twice while the transcript (replace semantics) kept
        only the winner, and the two views never converged."""
        if self._done:
            return  # sealed by message_done; the canonical message wins
        self._full.clear()
        self._tail.clear()
        self._committed_src.clear()
        for block in self._committed:
            block.display = False
        for block in self._committed:
            with contextlib.suppress(Exception):
                block.remove()  # not display=False: hidden widgets still leak
        self._committed.clear()
        self._dirty = True
        self.reset_thinking()

    def push_reasoning(self, delta: str) -> None:
        self.thinking.push(delta)

    # P2-1 edge case: an unclosed fence (or any single huge block) keeps the
    # commit boundary frozen, so the tail IS the whole block and every flush
    # re-parses it. Back off the render rate as the tail grows instead of
    # spinning the parser at 8 Hz — visual updates stay smooth enough while
    # the CPU spike disappears.
    TAIL_SLOW_CHARS = 12_000
    TAIL_SLOW_INTERVAL = 0.4  # seconds between renders once the tail is huge

    def flush(self) -> None:
        """Commit finished markdown blocks; only the live tail is repainted."""
        self.thinking.flush()
        if self._final_markdown or not self._dirty:
            return
        pending = "".join(self._tail)
        if len(pending) >= self.TAIL_SLOW_CHARS:
            now = time.monotonic()
            if now - self._last_render_at < self.TAIL_SLOW_INTERVAL:
                return
            self._last_render_at = now
        self._dirty = False
        self.parse_calls += 1
        self.parse_chars += len(pending)
        prefix, tail = split_committable(pending)
        if prefix.strip():
            self._commit_markdown(prefix)
            pending = tail
        self._tail = [pending]
        self.live.update(pending)
        self._mount_committed()

    def _commit_markdown(self, text: str) -> None:
        """Record a finished block. Mount it once the turn is in the DOM."""
        cleaned = text.strip("\n")
        if not cleaned:
            return
        self._committed_src.append(cleaned)
        self._mount_committed()

    def _mount_committed(self) -> None:
        """Attach any committed blocks that are not in the tree yet."""
        if not self.is_mounted:
            return
        try:
            if self.live.parent is None:
                return
        except Exception:
            return
        while len(self._committed) < len(self._committed_src):
            cleaned = self._committed_src[len(self._committed)]
            widget = Markdown(cleaned, classes="assistant-md")
            self.mount(widget, before=self.live)
            self._committed.append(widget)

    def _paint_final_markdown(self) -> None:
        """Render the leftover tail as Markdown; committed blocks stay put."""
        if self._final_markdown:
            return
        self._mount_committed()
        rest = "".join(self._tail).strip("\n")
        leftover = self._committed_src[len(self._committed):]
        if leftover:
            rest = "\n\n".join([*leftover, rest] if rest else leftover)
        if rest:
            self.parse_calls += 1
            self.parse_chars += len(rest)
            self._rich.update(rest)
            self._rich.display = True
            self.markdown = self._rich
        else:
            self._rich.display = False
            if self._committed:
                self.markdown = self._committed[-1]
        self.live.display = False
        self._final_markdown = True
        self._dirty = False
        self._tail = []

    def text(self) -> str:
        return "".join(self._full)

    def copy_text(self) -> str:
        """Selection copy: the answer. Thinking is opt-in via its own block."""
        return self.text()

    def content_offset_rows(self) -> int:
        """Visual rows between the block's top and the answer content.

        Line-range copy maps screen rows onto copy_text() lines, which exclude
        the thinking header — without this offset, a drag inside a turn that
        has reasoning copied the line below the highlighted one.
        """
        try:
            if self.thinking.display and self.thinking.region:
                return int(self.thinking.region.height)
        except Exception:
            pass
        return 0

    def finish(self, partial: bool = False, *, usage: dict | None = None,
               elapsed: float | None = None, model: str = "",
               tool_calls: int = 0) -> None:
        """Close the turn and write its meta line (model · tokens · elapsed)."""
        from oaset.i18n import t

        self._done = True
        self.flush()
        self._paint_final_markdown()
        self.thinking.finish()
        parts: list[str] = []
        if partial:
            parts.append(f"⏹ {t('interrupted')}")
        if model:
            parts.append(model)
        if usage:
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            total = usage.get("total_tokens")
            if total is not None:
                if prompt is not None and completion is not None:
                    parts.append(t("meta_tokens_io", prompt=prompt, completion=completion))
                else:
                    parts.append(t("meta_tokens", total=total))
        if elapsed is not None:
            parts.append(f"{elapsed:.1f}s")
        self._meta_text = " · ".join(parts)
        self.meta.update(self._meta_text)
        self.meta.display = bool(self._meta_text)

    def add_meta(self, text: str) -> None:
        """Append one more item to the meta line.

        The item may arrive with its own separator ("· ≈$0.01") because the
        separator is part of the localised string. Normalising here keeps the
        join to exactly one "·" — the caller includes the separator AND this
        method added a space, so the line rendered as `model · 1.2s  · ≈$0.01`
        with a double space.
        """
        item = text.strip()
        if item.startswith("·"):
            item = item.lstrip("·").strip()
        if not item:
            return
        # Turn-STATE annotations (plan progress) fold onto the previous
        # segment with a comma (`… · 1.2s · ≈$0.01, plan 2/3`): they describe
        # the same group the cost/time do. Value items keep their own "·".
        is_state = item.startswith("plan ") or " plan " in item
        if is_state and self._meta_text:
            self._meta_text = f"{self._meta_text}, {item}"
        else:
            parts = [p for p in (self._meta_text, item) if p]
            self._meta_text = " · ".join(parts)
        self.meta.update(self._meta_text)
        self.meta.display = True

    def meta_text(self) -> str:
        return self._meta_text


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        self.raw = text
        super().__init__(f"› {text}", classes="user-msg", markup=False)

    def copy_text(self) -> str:
        """Selection copy: the message itself, without the prompt glyph."""
        return self.raw


class NoticeCard(Static):
    """One notice line. Accepts a UiNotice or the legacy (text, kind) pair.

    Long bodies are CLAMPED for display (a /history or tool dump used to fill
    the whole screen); the full text stays in raw_text for selection copy.
    """

    KIND_CLASS = {"info": "notice-info", "error": "notice-error", "warn": "notice-warn"}
    KIND_ICON = {"info": "ℹ", "error": "✗", "warn": "⚠"}
    MAX_SHOWN_LINES = 12

    def __init__(self, text, kind: str = "info", *, code: str = "", hint: str = "") -> None:
        from oaset.i18n import t
        from oaset.tui.notice import UiNotice

        notice = UiNotice.of(text, level=kind, code=code, hint=hint)
        self.notice = notice
        self.raw_text = notice.text  # FULL body — copy keeps everything
        self.code = notice.code
        self.level = notice.level
        body = notice.render()
        lines = body.splitlines()
        if len(lines) > self.MAX_SHOWN_LINES + 2:
            hidden = len(lines) - self.MAX_SHOWN_LINES
            body = "\n".join(lines[: self.MAX_SHOWN_LINES])
            body += "\n" + t("notice_clamped", n=hidden)
        super().__init__(
            f"{self.KIND_ICON.get(notice.level, 'ℹ')} {body}",
            classes=f"notice {self.KIND_CLASS.get(notice.level, 'notice-info')}",
            markup=False,
        )

    def copy_text(self) -> str:
        return self.raw_text

    # A NoticeCard is a plain Static with markup=False over copy_text(), so its
    # rendered rows ARE its copy lines: line-range copy is sound here. Blocks
    # that render through Markdown or compose several children must stay False
    # (see ChatLog._line_at).
    rows_map_to_copy_lines = True


class WelcomePanel(Static):
    """The welcome page: gradient wordmark over the session facts.

    Startup stays quiet — a keyless model is badged here (never a red error
    popup); the send path refuses with a /login hint if it is actually used.
    The wordmark needs 48 columns and truecolor; below that (or under the
    legacy conhost class) the page degrades to the compact text header.
    """

    def __init__(self, version: str, model_id: str, session_id: str, cwd, provider: str,
                 needs_key: bool = False) -> None:
        self._facts = dict(version=version, model_id=model_id, session_id=session_id,
                           cwd=str(cwd), provider=provider, needs_key=needs_key)
        self.raw_text = ""
        super().__init__("", classes="welcome-box")

    def _build(self) -> None:
        from rich.text import Text

        from oaset.i18n import t
        from oaset.tui import brand

        f = self._facts
        # Colour is gated on truecolor support (modern conhost included),
        # NOT on the `legacy` glyph class — and the ramp flips for light
        # themes so the far end never washes out against a pale background.
        colored = brand.truecolor_supported()
        theme = None
        try:
            theme = self.app.current_theme
        except Exception:
            try:
                theme = self.app.get_theme(self.app.theme)
            except Exception:
                theme = None
        dark = getattr(theme, "dark", True)
        ramp = brand.GRADIENT if dark else brand.GRADIENT_LIGHT
        width = max(int(self.size.width), 20)
        # An unconfigured install has NO model: showing the shipped default
        # id plus a warning read as "this is your model, and it is broken".
        # The page says 暂无配置 and points at /login instead.
        model_line = (t("welcome_model_unset") if f["needs_key"]
                      else str(f["model_id"]))
        extra = t("welcome_next_step")
        # the copyable twin: no wordmark, no colour, every fact
        self.raw_text = (
            f"oAset v{f['version']} — {t('welcome_tagline')}\n"
            f"{t('welcome_model')}: {model_line}\n"
            f"{t('welcome_dir')}: {f['cwd']}\n"
            f"\n{t('welcome_hint')}\n"
            f"{t('welcome_tips')}"
            + (f"\n{extra}" if f["needs_key"] else "")
        )
        # ---- hero: one centred block, facts listed as rows ----
        # Build the page as equal-width lines, THEN centre via CSS: every row
        # is padded to the block width first, so the labels stay
        # column-aligned inside a block that itself sits centred.
        from oaset.utils import cell_width

        # ---- assemble adaptively: the box must fit the chat viewport ----
        # A 30-row terminal cannot show everything a 40-row one can; content
        # is admitted by priority (session line > getting-started > wordmark
        # > recent sessions) so the hint/tips tail is never pushed below the
        # fold. The wordmark is brand, not information — it goes last.
        try:
            viewport = int(self.app.chat.size.height) or 0
        except Exception:
            viewport = 0
        if viewport <= 0:
            viewport = max(int(self.size.height) - 6, 16)
        # CLAMP against the SCREEN: before layout settles (the CI runner
        # reproduces it) BOTH chat.size and app.size misreport — CI measured
        # app.size.height=42 on a 30-row run_test, the budget over-admitted,
        # and the panel scrolled its own branding off. screen.size is the
        # ground truth here; app.size is only the fallback.
        bound = 0
        try:
            bound = int(self.app.screen.size.height)
        except Exception:
            bound = 0
        if bound <= 0:
            try:
                bound = int(self.app.size.height)
            except Exception:
                bound = 0
        if bound > 0:
            viewport = min(viewport, max(bound - 6, 12))
        budget = max(viewport - 5, 8)  # body rows: frame+padding take 4,
        # one spare row so the box never scrolls its own top border off

        top: list = []
        if colored and width >= brand.WORDMARK_WIDTH + 2:
            # truecolor is also the proxy for a modern terminal font: on a
            # pre-19041 raster console the block letters garble like ◔◑◕.
            top += list(brand.wordmark(colored, ramp).split("\n")) + [None]

        title = Text()
        # a time-of-day greeting gives the page a pulse without noise: the
        # user reads "晚上好" and knows the terminal noticed them
        import datetime as _dtmod

        hour = _dtmod.datetime.now().hour
        greet_key = ("welcome_greet_late" if hour < 5 else
                     "welcome_greet_morning" if hour < 11 else
                     "welcome_greet_noon" if hour < 14 else
                     "welcome_greet_afternoon" if hour < 18 else
                     "welcome_greet_evening")
        title.append(t(greet_key), style="bold")
        title.append(" — ")
        title.append(f"oAset v{f['version']}", style="bold")
        title.append(f" — {t('welcome_tagline')}")
        base_top = [title, brand.rule(min(max(width - 4, 10), 60), colored, ramp), None,
                    f"{t('welcome_model')}: {model_line}",
                    f"{t('welcome_dir')}: {f['cwd']}"]

        sid = str(f.get("session_id") or "")
        session_row: list = []
        if sid:
            # ordinal + streak: a small "you've been here" pulse, local data
            try:
                ordinal = len(self.app.store.list_sessions(
                    cwd=str(self.app.cwd), limit=500)) or 1
            except Exception:
                ordinal = 1
            row = f"{t('welcome_session')}: {sid[-8:]}"
            bits = [t("welcome_session_ordinal", n=ordinal)]
            try:
                import datetime as _dtmod

                rows30 = {d: a for d, a in self.app.store.usage_by_days(days=30)}
                streak = 0
                day = _dtmod.date.today()
                while rows30.get(day.isoformat(), {}).get("total_tokens"):
                    streak += 1
                    day -= _dtmod.timedelta(days=1)
                if streak >= 2:
                    bits.append(t("welcome_streak", n=streak))
            except Exception:
                pass
            session_row = [row + "  ·  " + " · ".join(bits)]

        # --- character: seven-day activity sparkline (local data only) ---
        spark_row: list = []
        try:
            import datetime as _dt

            rows7 = {day: a for day, a in self.app.store.usage_by_days(days=7)}
            today = _dt.date.today()
            days = [(today - _dt.timedelta(days=6 - i)).isoformat() for i in range(7)]
            vals = [int((rows7.get(d) or {}).get("total_tokens", 0) or 0) for d in days]
            if any(vals):
                peak = max(vals) or 1
                glyphs = "▁▂▃▄▅▆▇█"
                bars = "".join(
                    glyphs[min(7, int(v / peak * 8))] if v else "▁" for v in vals)
                total = sum(vals)
                turns = sum(int((rows7.get(d) or {}).get("turns", 0) or 0) for d in days)
                total_s = f"{total / 1000:.0f}k" if total >= 1000 else str(total)
                spark_row = [f"{t('welcome_stats_7d')}  {bars}  "
                             f"{t('welcome_stats_line', tokens=total_s, turns=turns)}"]
        except Exception:
            spark_row = []

        # --- character: the branch, once the status bar has detected it ---
        branch = ""
        try:
            branch = str(getattr(self.app.status_bar, "_branch", "") or "")
        except Exception:
            branch = ""
        if branch:
            spark_row.append(f"{t('welcome_branch')}: {branch}")
        base_top.extend(spark_row)

        def cheat(command: str, purpose: str) -> str:
            # command column padded by CELL width so CJK labels stay aligned
            return f"  {command}" + " " * max(16 - cell_width(command), 1) + purpose

        quick_start: list = [None, Text(t("welcome_quick_start"), style="bold")]
        # TWO leading Nones: the spacer row AND the section header line each
        # occupy a rendered row. Missing the header's placeholder shifted every
        # action by one — clicking "Enter 提问" filled "/help".
        quick_actions: list = [None, None]
        for command, purpose in (
            ("Enter", t("welcome_qs_ask")),
            ("/help", t("welcome_qs_help")),
            ("/model", t("welcome_qs_model")),
            ("/login", t("welcome_qs_login")),
            ("/sessions", t("welcome_qs_sessions")),
            ("/undo", t("welcome_qs_undo")),
            ("Ctrl+K", t("welcome_qs_keys")),
            ("Esc", t("welcome_qs_esc")),
        ):
            quick_start.append(cheat(command, purpose))
            # "/" rows are launchers: clicking fills the composer
            quick_actions.append(("cmd", command) if command.startswith("/") else None)

        try:
            metas = self.app.store.list_sessions(cwd=str(self.app.cwd), limit=3)
        except Exception:
            metas = []
        recent: list = []
        recent_actions: list = [None, None]
        if metas:
            recent = [None, Text(t("welcome_recent"), style="bold")]
            for m in metas:
                mt = (m.title or t("welcome_untitled"))[:36]
                recent.append(f"  {mt}  {m.session_id[-8:]}  {str(m.updated_at)[:16]}")
                recent_actions.append(("session", m.session_id))

        if not getattr(self, "_fun_line", ""):
            # one personality line per launch (stable across resizes)
            import random

            self._fun_line = t(f"welcome_fun_{random.randint(1, 12)}")
        tail = [None, t("welcome_hint"), t("welcome_tips")]
        charms = [t("welcome_click_hint"), f"· {self._fun_line}"]
        if f["needs_key"]:
            tail.append(extra)

        used = len(base_top) + len(tail)
        chosen: list = []
        for block in (session_row, quick_start, charms, top, recent):
            if block and used + len(block) <= budget:
                chosen.append(block)
                used += len(block)
        rows: list = []
        actions: list = []
        block_actions = {"top": [None] * len(top), "base_top": [None] * len(base_top),
                         "session_row": [None] * len(session_row),
                         "quick_start": quick_actions, "recent": recent_actions,
                         "tail": [None] * len(tail), "charms": [None] * len(charms)}
        for name, block in (("top", top), ("base_top", base_top),
                            ("session_row", session_row), ("quick_start", quick_start),
                            ("charms", charms), ("recent", recent), ("tail", tail)):
            if block in chosen or block in (base_top, tail):
                rows.extend(block)
                actions.extend(block_actions[name])
        self._row_actions = actions

        def _row_cells(row) -> int:
            if row is None:
                return 0
            return row.cell_len if isinstance(row, Text) else cell_width(str(row))

        # widest row: rows are padded to this width for the gradient block
        # (named block_w — `block` is the loop variable of the chooser above)
        block_w = max((_row_cells(r) for r in rows), default=0)
        page = Text()
        padded: list[str | None] = []
        for row in rows:
            if row is None:
                padded.append(None)
            else:
                padded.append(str(row) + " " * max(block_w - _row_cells(row), 0))
        # actions stay 1:1 with LOGICAL rows; the physical expansion happens
        # at click time against the live rendered text — building it here
        # desynced from the screen whenever the build-time width guess
        # differed from the renderer's (container padding, resize races)
        self._row_actions = actions
        self._row_strings = padded
        # the page itself keeps its logical rows (wrap happens at render)
        for i, row in enumerate(rows):
            if row is None:
                page.append("\n")
                continue
            if isinstance(row, Text):
                page.append(row)
            else:
                page.append(str(row) + " " * max(block_w - _row_cells(row), 0))
            if i < len(rows) - 1:
                page.append("\n")
        # Explicit height: auto-height does not reliably re-measure a Text
        # updated mid-layout (the panel used to clip after the wordmark).
        wrap_width = max(int(self.size.width) or 120, 1)
        body_rows = sum(
            max(1, -(-cell_width(line) // wrap_width))
            for line in page.plain.split("\n"))
        # Textual box-sizing is border-box: height must include the frame
        # (2 rows) and the 1-cell vertical padding (2 rows) or the border
        # eats the last content rows.
        target_height = body_rows + 4
        # ONLY write when it actually changes: a style write schedules a
        # resize, on_resize rebuilds, and when two size measurements
        # disagree (CI-first-frame misreports) an unconditional write
        # oscillates forever — the historical "full gate hang": a rebuild
        # loop burning CPU and memory until the job died
        try:
            current = self.styles.height
            if current is None or int(current.value) != target_height:
                self.styles.height = target_height
        except Exception:
            self.styles.height = target_height
        self._last_build_size = (int(self.size.width), int(self.size.height))
        self.update(page)

    async def on_click(self, event) -> None:
        """Launcher rows: click a "/" cheat row to fill the composer, click a
        recent-session row to restore it. Drag-selection never triggers this
        (ChatLog stops dragged releases), and unknown rows are no-ops."""

        actions = getattr(self, "_row_actions", None)
        strings = getattr(self, "_row_strings", None)
        if not actions or strings is None:
            return
        # border (1) + vertical padding (1) sit above the first body row
        index = event.y - 2
        # Attribute the CLICKED rendered line to its logical row by content
        # matching, instead of re-simulating the renderer's wrap (three
        # simulators disagreed with the screen by one line on deep paths:
        # rich folds long words, Textual clips-or-wraps depending on
        # measure timing). Every row's first rendered line starts with the
        # row's own text (padding is trailing), so walk the RENDERED lines
        # and assign each to the nearest row whose text it starts.
        rendered = self.render().plain.split("\n")
        if not (0 <= index < len(rendered)):
            return
        clicked = rendered[index].rstrip()
        action = None
        if clicked:
            row_index = None
            starts: list[tuple[int, str]] = []
            for i, line in enumerate(strings):
                if line is None:
                    continue
                head = line.rstrip()[:24]
                if head:
                    starts.append((i, head))
            for pos, (i, head) in enumerate(starts):
                if clicked.startswith(head[:min(len(head), 12)]) or head.startswith(
                        clicked[:min(len(clicked), 12)]):
                    row_index = i
                    break
            if row_index is not None:
                action = actions[row_index]
        if action is None:
            return
        kind, value = action
        app = self.app
        if kind == "cmd":
            app.input_area.load_text(value)
            with contextlib.suppress(Exception):
                app.input_area.focus()
        elif kind == "session":
            load = getattr(app, "_load_session", None)
            if load is not None:
                from oaset.i18n import t

                app.chat.add_notice(t("welcome_restoring", sid=value[-8:]), "info")
                asyncio.create_task(load(value))

    def on_mount(self) -> None:
        self._build()
        # second pass AFTER layout settles: during on_mount the screen
        # geometry is not final (CI: screen.size unavailable/measured larger
        # than the eventual 30 rows), so the budget was computed from the
        # app-level fallback and the panel came out 36 rows on a 30-row
        # screen. The height-write dedup keeps this second pass loop-free.
        self.call_after_refresh(self._rebuild_settled)

    def _rebuild_settled(self) -> None:
        self._geometry_settled = True
        self._build()

    def on_resize(self) -> None:
        # skip the rebuild when nothing about the geometry changed: the
        # set-height→resize→rebuild chain is fine once, a loop is not.
        # Until the settled pass has run, resizes still rebuild (they are
        # the only chance to see the real size during early layout).
        size = (int(self.size.width), int(self.size.height))
        if getattr(self, "_geometry_settled", False) and \
                size == getattr(self, "_last_build_size", None):
            return
        self._build()

    def copy_text(self) -> str:
        return self.raw_text


class FoldChip(Static):
    """One "⋯ N tool calls folded" line. Click to reveal the hidden cards.

    A long agent turn mounts one line per tool call; past five consecutive
    calls the older ones become a wall of `✓ Used …`. They are hidden (never
    removed — copy/transcript keep every card) behind this single chip.
    """

    DEFAULT_CSS = "FoldChip { color: $secondary; height: 1; padding: 0 0 0 1; }"

    def __init__(self, cards: list) -> None:
        self.cards: list = list(cards)
        super().__init__("", classes="fold-chip", markup=True)
        self.refresh_label()

    def add_cards(self, cards: list) -> None:
        for card in cards:
            if card not in self.cards:
                self.cards.append(card)

    def refresh_label(self) -> None:
        from oaset.i18n import t

        self.update(f"[accent]▸[/] {t('fold_chip', n=len(self.cards))}")

    def copy_text(self) -> str:
        """Copying the chip yields the group it stands for.

        The chip is a fold marker, not a transcript line: without this, dragging
        across a folded group silently dropped the calls it hides, and the chip
        itself could not be selected at all.
        """
        parts = [(getattr(card, "copy_text", lambda: "")() or "").strip()
                 for card in self.cards]
        return "\n".join(p for p in parts if p)

    def unfold(self) -> None:
        """Reveal every folded block and retire this chip (keyboard path too:
        Ctrl+O reaches this, the click handler used to be the only way in)."""
        for card in self.cards:
            card.display = True
            # Mark as deliberately unfolded: the folder must never re-hide a
            # block the user opened (same_group used to re-fold them via a
            # detached chip, unrecoverably).
            card._oaset_unfolded = True
        chat = self.parent
        if isinstance(chat, ChatLog) and getattr(chat, "_fold_chip", None) is self:
            chat._fold_chip = None
        # Hide NOW: prune timing is not synchronous enough to rely on, and a
        # stale "⋯ N tool calls" line floating above its expanded cards reads
        # as a second, phantom group.
        self.display = False
        with contextlib.suppress(Exception):
            self.remove()  # remove() initiates the prune synchronously

    async def on_click(self) -> None:
        self.unfold()


class ChatLog(VerticalScroll):
    """Scrollable conversation view. Follows the tail unless the user scrolls up.

    Drag-selection lives here: a mouse drag over the flow selects a block, and
    a drag that stays inside one block also records start/end rows so
    Ctrl+Shift+C can copy a line range (stack-trace three-liner) without F2.
    Cross-block drags still copy whole blocks.
    """

    DEFAULT_CSS = """
    ChatLog { padding: 0 1 0 0; }
    /* width auto made MarkdownTable's 1fr fall back to content width, so a
    wide table pushed past the chat column and its right border got cropped. */
    ChatLog > AssistantTurn { width: 100%; }
    ChatLog .chat-selected { text-style: underline; }
    ChatLog .archive-hint { color: $secondary; }
    """

    # P2-5: bound the mounted DOM. A 10k-block session used to keep every
    # widget mounted forever, so per-frame layout cost grew without limit.
    MAX_MOUNTED_BLOCKS = 600
    # Blocks restored per scroll-to-top gesture. 200 re-mounted faster than
    # the eye could follow and, with the 600-block cap, one gesture could
    # yank the whole mounted set past its bound; 60 keeps one gesture to a
    # screenful-scale batch (reading older history takes a few more scrolls).
    ARCHIVE_PAGE = 60

    def __init__(self, **kwargs) -> None:
        self.follow = True
        kwargs.setdefault("id", "chat")
        super().__init__(**kwargs)
        self._selecting = False
        self._sel_anchor: Widget | None = None
        self._selection: list[Widget] = []
        self._line_anchor: int | None = None
        self._line_end: int | None = None
        self._drag_ended_at = 0.0
        self._down_screen: tuple[int, int] | None = None
        self._archived: list[Widget] = []  # off-DOM blocks, still live objects
        self.max_mounted_blocks = self.MAX_MOUNTED_BLOCKS
        self.archive_page = self.ARCHIVE_PAGE
        self._loading_archive = False
        self._archive_hint = Static("", classes="archive-hint", markup=False)
        self._archive_hint.display = False
        self._active_turn: AssistantTurn | None = None
        self._fold_chip: FoldChip | None = None

    # -------------------------------------------------------------- archiving

    def _live_blocks(self) -> list[Widget]:
        return [w for w in self.children if w is not self._archive_hint]

    def _enforce_cap(self) -> None:
        """Move the oldest blocks off the DOM and into `_archived`.

        The detach is synchronous on purpose. It used to be
        `asyncio.create_task(self._archive_detach(page))`, and those tasks never
        ran to completion in practice: 900 mounted blocks left 300 widgets
        flagged as archived *and still mounted*, so the cap bounded nothing and
        per-frame layout cost kept growing with the session (the exact failure
        MAX_MOUNTED_BLOCKS exists to prevent).

        The `_oaset_archived` flag still guards the census: it marks a block as
        already archived, so a later mount cannot re-archive it while the page
        is off-DOM.
        """
        blocks = [w for w in self._live_blocks()
                  if not getattr(w, "_oaset_archived", False)]
        overflow = len(blocks) - self.max_mounted_blocks
        if overflow <= 0:
            return
        page = blocks[:overflow]
        for widget in page:
            setattr(widget, "_oaset_archived", True)
            # Re-home to the top of the archive: _load_archived_page() takes
            # the tail, so the page order stays chronological.
            self._archived.insert(0, widget)
            with contextlib.suppress(Exception):
                widget.remove()
        self._update_archive_hint()

    def _update_archive_hint(self) -> None:
        from oaset.i18n import t

        if self._archived:
            self._archive_hint.update(t("archive_hint", n=len(self._archived)))
            self._archive_hint.display = True
            if not self._archive_hint.is_mounted and self.is_attached and not self._closing:
                with contextlib.suppress(Exception):
                    self.mount(self._archive_hint, before=0)
        else:
            self._archive_hint.display = False

    async def _load_archived_page(self) -> None:
        """Re-mount the next archived page at the top, keeping the view still.

        Reaching the top is the natural "I want to read older output" signal;
        scrolling back a whole page is the one place where a jump would be
        obvious, hence the scroll-offset correction."""
        try:
            page = self._archived[-self.archive_page:]
            # The del must wait for the teardown guard: deleting here and
            # returning below used to lose archived blocks outright.
            if not self.is_attached or self._closing or self._pruning:
                return  # teardown won the race; nothing left to mount into
            del self._archived[-len(page):]
            before_height = self.virtual_size.height
            was_at = self.scroll_y
            first_live = self._live_blocks()
            anchor = first_live[0] if first_live else None
            for widget in page:
                setattr(widget, "_oaset_archived", False)  # back in the census
                if anchor is not None:
                    self.mount(widget, before=anchor)
                else:
                    self.mount(widget)
            self._update_archive_hint()
            # Layout needs a real pass before virtual_size reflects the new
            # mounts; measuring right away reads delta=0, the correction
            # leaves the view at the top, and the watcher cascades page after
            # page — scrolling once used to drain the ENTIRE archive (750
            # mounted blocks, cap bypassed) in about a second.
            self.refresh(layout=True)
            await asyncio.sleep(0)
            delta = self.virtual_size.height - before_height
            target = (was_at or 0) + delta
            if target <= 1.0:
                target = 2.0  # strictly below the "at top" threshold: one
                # scroll gesture loads ONE page, never a cascade
            self.scroll_to(y=target, animate=False)
        finally:
            self._loading_archive = False

    # ------------------------------------------------------------ selection

    def selectable_blocks(self) -> list[Widget]:
        """Direct children that can be copied (they expose copy_text()).

        Blocks hidden by a fold are excluded: they still carry `copy_text()`,
        so a drag across a folded group used to append output that was nowhere
        on screen. The FoldChip that stands for them is selectable instead.
        """
        return [w for w in self.children
                if callable(getattr(w, "copy_text", None))
                and getattr(w, "display", True) is not False]

    def _block_under(self, event) -> Widget | None:
        try:
            # get_widget_at lives on Screen; hit-test in screen coordinates and
            # then walk up to the nearest selectable block.
            widget, _region = self.screen.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            return None
        blocks = self.selectable_blocks()
        node: DOMNode | None = widget
        while node is not None and node is not self:
            if node in blocks:
                return node
            node = node.parent
        return None

    def _range_between(self, a: Widget, b: Widget) -> list[Widget]:
        blocks = self.selectable_blocks()
        if a not in blocks or b not in blocks:
            return [a] if a in blocks else []
        i, j = blocks.index(a), blocks.index(b)
        lo, hi = (i, j) if i <= j else (j, i)
        return blocks[lo : hi + 1]

    def _paint_selection(self) -> None:
        chosen = set(self._selection)
        for block in self.selectable_blocks():
            block.set_class(block in chosen, "chat-selected")
        # Archived (unmounted) blocks kept a stale chat-selected class and
        # reappeared highlighted after a scroll-to-top reload — deceive the
        # eye no more: they are not selected and will not copy.
        for block in self._archived:
            block.remove_class("chat-selected")

    def begin_selection(self, block: Widget, line: int | None = None) -> None:
        self._sel_anchor = block
        self._selection = [block]
        self._line_anchor = line
        self._line_end = line
        self._paint_selection()

    def extend_selection(self, block: Widget | None, line: int | None = None) -> None:
        if self._sel_anchor is None or block is None:
            return
        self._selection = self._range_between(self._sel_anchor, block)
        if len(self._selection) == 1 and line is not None:
            self._line_end = line
        else:
            self._line_anchor = None
            self._line_end = None
        self._paint_selection()

    def clear_selection(self) -> None:
        if not self._selection and self._sel_anchor is None:
            return
        self._sel_anchor = None
        self._selection = []
        self._line_anchor = None
        self._line_end = None
        self._paint_selection()

    def selected_blocks(self) -> list[Widget]:
        return list(self._selection)

    def _line_at(self, event, block: Widget) -> int | None:
        """Copy-text line index for a mouse event, or None when unknowable.

        Line-range copy translates a screen row into an index into
        `copy_text().splitlines()`. That translation is only sound for blocks
        whose rendered rows are their copy lines. It is NOT sound for the two
        blocks that used to claim it:

        * `AssistantTurn` renders through several `Markdown` widgets, and
          Textual gives headers/paragraphs vertical margins, so a rendered row
          has no 1:1 counterpart in the markdown source (long lines also
          soft-wrap, mapping one source line to N rows).
        * `ToolCard.copy_text()` emits `title + tool_name + output`, but
          `content_offset_rows()` subtracted a peek line that is not in that
          text and never accounted for the inserted `tool_name` line — a drag
          starting on the first output row copied the tool name instead.

        Returning None makes the caller copy the whole block, which is what the
        user sees highlighted; a wrong sub-range is worse than a whole block.
        """
        if not getattr(block, "rows_map_to_copy_lines", False):
            return None
        try:
            region = block.region
            y = int(event.screen_y) - int(region.y)
        except Exception:
            return None
        offset = getattr(block, "content_offset_rows", None)
        if callable(offset):
            y -= int(offset())
        # wrapped rows betray the 1-rendered-row==1-copy-line claim: a long
        # Notice line (error paths carry deep paths) soft-wraps and shifts
        # every later row. When heights disagree, fall back to whole-block
        # copy — same principle as above: a wrong sub-range is worse.
        copy_lines = getattr(block, "copy_text", lambda: "")()
        if int(region.height) != len(str(copy_lines).splitlines()):
            return None
        return max(0, y)

    def selected_text(self) -> str:
        if (
            len(self._selection) == 1
            and self._line_anchor is not None
            and self._line_end is not None
        ):
            raw = (getattr(self._selection[0], "copy_text")() or "")
            lines = raw.splitlines()
            if lines:
                lo, hi = sorted((self._line_anchor, self._line_end))
                hi = min(hi, len(lines) - 1)
                lo = max(0, min(lo, hi))
                return "\n".join(lines[lo : hi + 1]).strip()
        parts = [(getattr(b, "copy_text")() or "").strip() for b in self._selection]
        return "\n\n".join(p for p in parts if p).strip()

    def transcript_text(self) -> str:
        """Everything selectable, in order - the whole conversation as text.

        Blocks beyond the mounted cap live in ``_archived`` (still live
        widgets, just off-DOM); reading only the DOM silently truncated long
        conversations.
        """
        parts = [(getattr(b, "copy_text", lambda: "")() or "").strip()
                 for b in self._ordered_blocks()]
        return BLANK_LINE.join(p for p in parts if p).strip()

    def tool_outputs_text(self) -> str:
        """Every tool call line plus its output (archived cards included)."""
        from oaset.tui.widgets.tool_card import ToolCard

        parts = [b.copy_text().strip() for b in self._ordered_blocks()
                 if isinstance(b, ToolCard)]
        return BLANK_LINE.join(p for p in parts if p).strip()

    def assistant_turns(self) -> list["AssistantTurn"]:
        """Archived + mounted assistant turns, oldest first."""
        return [b for b in self._ordered_blocks() if isinstance(b, AssistantTurn)]

    def _ordered_blocks(self) -> list[Widget]:
        """Archived (older) then live blocks, chronological, hint excluded."""
        return [w for w in [*self._archived, *self.children]
                if w is not self._archive_hint]

    def dragging_recently(self) -> bool:
        """True just after a drag, so a block does not treat it as a click.

        Textual synthesises Click from down+up; a drag that ends on the same
        block would otherwise also expand/collapse it.
        """
        return (time.monotonic() - self._drag_ended_at) < 0.4

    async def on_mouse_down(self, event) -> None:
        if getattr(event, "shift", False):
            # Shift+drag belongs to the TERMINAL's native character selection
            # (every host supports it while an app reports mouse). Stay out of
            # the way — block-level selection stays available without shift.
            self._selecting = False
            return
        if event.button == 3:
            # cmd.exe convention: right-click copies a selection, otherwise it
            # pastes into the input. Either way a stale highlight must not
            # survive the click.
            self._selecting = False
            self.release_mouse()
            if self._selection:
                await self._right_click_copy()
            else:
                input_area = getattr(self.app, "input_area", None)
                if input_area is not None:
                    with contextlib.suppress(Exception):
                        input_area.focus()
                    await input_area.action_paste_clipboard()
            event.stop()
            return
        block = self._block_under(event)
        if block is None:
            self.clear_selection()
            return
        self.begin_selection(block, self._line_at(event, block))
        self._selecting = True
        self._down_screen = (int(event.screen_x), int(event.screen_y))
        self.capture_mouse()

    async def _right_click_copy(self) -> None:
        """Copy the selection to the clipboard and report it (cmd.exe style).

        Routed through the app's copy funnel: retry, OSC52 fallback and the
        localised confirmation all live there (P1-8)."""
        text = self.selected_text()
        if not text:
            return
        app = cast(Any, self.app)  # the app is always OasetApp here
        if await app._copy_text(text):
            self.clear_selection()

    async def on_mouse_move(self, event) -> None:
        if not self._selecting:
            return
        # A drag that moved real distance ends in a DROP, not a Click — even
        # inside a block with no line mapping. Without this, selecting text
        # inside an expanded ToolCard collapsed it on mouse-up.
        if self._down_screen is not None:
            dx = abs(int(event.screen_x) - self._down_screen[0])
            dy = abs(int(event.screen_y) - self._down_screen[1])
            if dx + dy >= 2:
                self._drag_ended_at = time.monotonic()
        block = self._block_under(event)
        if block is None:
            return
        line = self._line_at(event, block)
        if block is not self._sel_anchor or (
            line is not None and line != self._line_end
        ):
            self.extend_selection(block, line)
            event.stop()

    async def on_mouse_up(self, event) -> None:
        self._down_screen = None
        if not self._selecting:
            return
        self._selecting = False
        self.release_mouse()
        if self._drag_ended_at:
            event.stop()


    # ------------------------------------------------------------- streaming

    def add_welcome(self, **kwargs) -> WelcomePanel:
        widget = WelcomePanel(**kwargs)
        self._add_widget(widget)
        return widget

    def add_user(self, text: str) -> UserMessage:
        widget = UserMessage(text)
        self.follow = True  # sending a message re-engages tail-follow
        self._add_widget(widget)
        return widget

    def start_assistant(self, *, live: bool = True) -> AssistantTurn:
        turn = AssistantTurn(live=live)
        self._active_turn = turn
        self._add_widget(turn)
        return turn

    @property
    def active_turn(self) -> AssistantTurn | None:
        return self._active_turn

    def ensure_assistant(self) -> AssistantTurn:
        """The segment newly streamed text goes into.

        A tool call seals the current segment, so post-tool narration starts a
        NEW block BELOW the tool cards. The old single-turn-per-run layout
        rendered post-tool text inside the block ABOVE the cards — the cards
        looked pinned to the bottom while fresh output appeared above them,
        in the wrong chronological order."""
        if self._active_turn is None:
            self.start_assistant()
        return self._active_turn  # type: ignore[return-value]

    def seal_assistant(self) -> None:
        """Close the current text segment: it is finished content (empty meta),
        and the next delta opens a fresh segment after whatever follows.
        Already-finished segments (turn_done wrote their meta) are left alone."""
        if self._active_turn is not None and not self._active_turn._done:
            self._active_turn.finish()
        self._active_turn = None

    def add_card(self, content: str, *, pin_start: bool = False) -> Static:
        """Rich-markup static card rendered in the conversation flow (/help etc.)."""
        widget = Static(content, classes="chat-card", markup=True)
        self._add_widget(widget, pin_start=pin_start)
        return widget

    def add_notice(self, text, kind: str = "info", *, code: str = "", hint: str = "") -> NoticeCard:
        """Record a notice. `text` may be a UiNotice, an exception or a string."""
        widget = NoticeCard(text, kind, code=code, hint=hint)
        self._add_widget(widget)
        return widget

    def add_tool_card(self, name: str, summary: str, raw_args: str = "",
                      *, restored: bool = False) -> ToolCard:
        from oaset.tui.widgets.tool_card import ToolCard

        card = ToolCard(name, summary, raw_args=raw_args, restored=restored)
        self._add_widget(card)
        return card

    def add_spacer(self) -> None:
        self._add_widget(Static(" ", markup=False))

    # --------------------------------------------------------- tool folding

    FOLD_THRESHOLD = 5   # start folding at this many consecutive finished calls
    FOLD_KEEP = 2        # always leave the newest calls visible

    def _fold_tool_runs(self) -> None:
        """Collapse long runs of consecutive finished tool cards behind a chip.

        The wall of `✓ Used …` lines was the single biggest density complaint:
        a 15-call turn printed 15 rows. Cards are hidden (display=False), never
        removed, so selection copy, /copy tools and the transcript keep them.
        """
        from oaset.tui.widgets.tool_card import ToolCard

        blocks = self._live_blocks()
        run: list[ToolCard] = []
        for block in reversed(blocks):
            if block is self._fold_chip:
                continue  # our own chip sits inside its group
            if isinstance(block, ToolCard) and not block.has_class("tool-running"):
                run.append(block)
            else:
                break
        run.reverse()
        if len(run) < self.FOLD_THRESHOLD:
            return
        # A block the user unfolded must never be re-hidden by a later fold.
        run = [card for card in run if not getattr(card, "_oaset_unfolded", False)]
        if len(run) < self.FOLD_THRESHOLD:
            return
        hidden = run[: len(run) - self.FOLD_KEEP]
        chip = self._fold_chip
        # same group iff this chip is still live (mounted — a clicked-open chip
        # was removed from the DOM and must not receive add_cards), and its
        # hidden cards are in this run
        same_group = (
            chip is not None
            and chip.cards
            and chip.is_attached
            and not getattr(chip, "_oaset_unfolded", False)
            and chip.cards[-1] in run
        )
        if not same_group:
            chip = FoldChip(hidden)
            self._fold_chip = chip
            if not self.is_attached or self._closing or self._pruning:
                return  # teardown race: skip the chip, keep cards hidden-safe
            self.mount(chip, before=hidden[0])
        else:
            chip.add_cards(hidden)  # type: ignore[union-attr]
        for card in hidden:
            card.display = False
        chip.refresh_label()  # type: ignore[union-attr]

    def clear_view(self) -> None:
        self.remove_children()
        self._active_turn = None
        self.follow = True
        self._fold_chip = None
        # State that outlived the children corrupted the next view: a stale
        # selection still copied cleared content, and leftover archived blocks
        # re-mounted the PREVIOUS session the moment the cleared (short) view
        # scrolled to the top.
        self.clear_selection()
        self._archived.clear()
        self._loading_archive = False
        self._archive_hint.display = False

    # --------------------------------------------------------------- helpers

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        try:
            super().watch_scroll_y(old_value, new_value)
        except Exception:
            pass
        try:
            distance = self.max_scroll_y - new_value
            if distance <= 2:
                self.follow = True
            elif new_value < old_value:
                self.follow = False
        except Exception:
            pass
        # Scroll feedback (P1-1): the user must see that there is more above
        # and where they are — the scrollbar alone is one cell wide.
        try:
            total = self.max_scroll_y
            cast(Any, self.app).status_bar.set_scroll_position(
                None if total <= 0 else min(1.0, new_value / total))
        except Exception:
            pass
        # P2-5: reaching the top loads the next archived page back into view.
        # follow=True means the view sits at the tail (or was clamped there
        # by content shrinking) — that is not a user's "show me older output".
        if (self._archived and new_value <= 1.0 and not self._loading_archive
                and not self.follow):
            self._loading_archive = True
            asyncio.create_task(self._load_archived_page())

    def _add_widget(self, widget, *, pin_start: bool = False) -> None:
        # Teardown race (real-machine crash): exiting while a turn finishes
        # unmounts the tree, and the worker's final notices still tried to
        # mount — MountError killed the exit. Detached means done: drop it.
        if not self.is_attached or self._closing or self._pruning:
            return
        self.mount(widget)
        if pin_start:
            self.follow = False
            self.call_after_refresh(widget.scroll_visible, animate=False, top=True)
        elif self.follow:
            self.call_after_refresh(self.scroll_end, animate=False, force=True)
        self._enforce_cap()
        # a non-tool block ends the current tool fold group (a fresh run of
        # cards after it builds its own chip). Mounts settle asynchronously,
        # so group membership cannot depend on mount timing.
        from oaset.tui.widgets.tool_card import ToolCard

        if not isinstance(widget, ToolCard):
            self._fold_chip = None

    def _on_resize(self, event) -> None:
        """Streaming widgets grow in place (thinking/markdown/tool cards); keep
        the tail pinned while the user hasn't scrolled away."""
        if self.follow:
            try:
                self.call_after_refresh(self.scroll_end, animate=False, force=True)
            except Exception:
                pass

    def watch_virtual_size(self, old_size, new_size) -> None:
        """Fires after the content height settles (Markdown expands over several
        frames) — scroll_end here uses the fresh height, unlike resize-time."""
        if self.follow and new_size != old_size:
            try:
                self.scroll_end(animate=False, force=True)
            except Exception:
                pass

    def rehydrate(self, conversation) -> None:
        """Replay a restored conversation in live chronological order.

        Tool-call-only assistant frames seal the text above, then each tool
        result mounts immediately (same as a live turn). Post-tool assistant
        text opens a new block below the cards.
        """
        from oaset.agent.loop import arguments_summary

        pending: dict[str, object] = {}
        for message in conversation.messages:
            if message.role == "user":
                self.seal_assistant()
                content = message.display_text()
                # loop-injected turns (steer, verify gate, background results)
                # are persisted as user messages; drawing them as user bubbles
                # made the restored transcript show words the user never wrote
                from oaset.agent.messages import is_injected_user_message

                if is_injected_user_message(message):
                    self.add_notice(content, "info")
                else:
                    self.add_user(content)
            elif message.role == "assistant":
                for call in (message.tool_calls or []):
                    pending[call.id] = call
                text = message.content or ""
                reasoning = message.reasoning or ""
                if message.tool_calls and not text.strip() and not reasoning:
                    self.seal_assistant()
                    continue
                self.seal_assistant()
                turn = self.start_assistant(live=False)
                if reasoning:
                    turn.push_reasoning(reasoning)
                if text:
                    turn.push_content(text)
                turn.finish(partial=message.partial)
                if message.tool_calls:
                    self.seal_assistant()
            elif message.role == "tool":
                self.seal_assistant()
                call = pending.pop(message.tool_call_id or "", None)
                name = getattr(call, "name", "") or "tool"
                summary = arguments_summary(getattr(call, "arguments", "") or "")
                raw_args = getattr(call, "arguments", "") or ""
                card = self.add_tool_card(name, summary, raw_args=raw_args,
                                          restored=True)
                card.finish(message.content or "", is_error=message.error)
        self.seal_assistant()
        # same density as the live path: an 8-tool restored turn used to
        # show 8 unfolded "Used" cards while the live twin folded to a chip
        with contextlib.suppress(Exception):
            self._fold_tool_runs()
