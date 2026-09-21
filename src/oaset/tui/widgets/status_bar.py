"""状态栏 — 双行、宽度自适应、CJK 安全。

  row1: <model> <state>  <cwd> <branch>     <shortcuts>
  row2:                         <context: x.x% (used/total)>

所有行按实际控件宽度截断（rich cell_len 正确处理 CJK 双宽字符），
任何窗口尺寸下都不会溢出或被裁切。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    pass

from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from oaset.i18n import resolve_language, t
from oaset.tui.glyphs import SPINNER_DIAL, STATIC
from oaset.utils import cell_width

SPINNER = SPINNER_DIAL


def fmt_k(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def clip(text: str, width: int) -> str:
    """按显示单元格宽度截断字符串（CJK 双宽感知）。"""
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        w = cell_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def visible_len(markup: str) -> int:
    """Display width of a rich-markup string.

    cell_len() counts the markup itself ("[red]x[/red]" is 14 cells), which made
    the left side think the right side was far wider than it renders - that is
    why a long error headline used to overlap the cwd.
    """
    from rich.text import Text

    try:
        return Text.from_markup(markup).cell_len
    except Exception:
        return cell_width(markup)


def clip_markup(text: str, width: int) -> str:
    r"""Clip a markup string to `width` cells without breaking markup.

    Plain clip() cuts through a [tag] — rich then rejects the wreckage with
    MarkupError inside _tick. Tags cost no cells here, `\[` escapes pass
    through, and whatever is still open at the cut is closed cleanly."""
    out: list[str] = []
    open_tags: list[str] = []
    used = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] == "[":
            out.append(text[i:i + 2])  # escaped bracket: one visible cell
            used += 1
            i += 2
            continue
        if ch == "[":
            j = text.find("]", i)
            if j == -1:
                break  # truncated tag: drop it entirely
            tag = text[i:j + 1]
            if tag.startswith("[/"):
                if open_tags:
                    open_tags.pop()
            else:
                open_tags.append(tag)
            out.append(tag)
            i = j + 1
            continue
        w = cell_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
        i += 1
    out.extend(reversed(open_tags))  # balance before rich ever sees it
    return "".join(out)


class StatusBar(Vertical):
    DEFAULT_CSS = """
    StatusBar { height: auto; min-height: 1; background: ansi_default; }
    .status-row { height: 1; width: 1fr; }
    .status-left { width: 1fr; color: $secondary; }
    .status-right { width: auto; color: $secondary; text-align: right; }
    .status-row2 { height: 1; width: 1fr; }
    .status-right2 { width: auto; color: $secondary; text-align: right; }
    """

    def __init__(self, **kwargs) -> None:
        self._model = ""
        self._thinking = ""  # /think badge; empty = provider default (hidden)
        self._state = ""
        self._cwd = ""
        self._cwd_full = ""  # unsliced path; the bar shortens at render time
        self._branch = ""
        self._mode = "default"
        self._session = ""
        self._max_context = 0
        self._tokens = 0
        self._busy = False
        self._busy_since: float | None = None
        self._spin = 0
        self._goal = ""
        self._error = ""  # sticky error headline; the tip carousel must not hide it
        self._needs_key = True  # until the app says otherwise, the login tip is fair
        self.stalled_for: "Callable[[], float] | None" = None  # app-set: seconds since last visible progress
        self.reduce_motion = False  # accessibility: static busy glyph, no rotation
        self._error_hint = ""  # recovery hint shown on row 2 while pinned
        self._scroll_pct: float | None = None  # chat scroll indicator (P1-1)
        self._note = ""  # transient state note (e.g. provider retry countdown)
        self._input_line = 0  # input cursor position "行 x/y" (P1-2)
        self._input_total = 0
        self._queue_n = 0  # messages waiting in the Enter queue
        self._bg_running = 0  # live background tasks, shown while busy
        self.left = Static("", classes="status-left", markup=True)
        self.right1 = Static("", classes="status-right", markup=True)
        self.right2 = Static("", classes="status-right2", markup=True)
        self._tip_index = 0
        super().__init__(
            Horizontal(self.left, self.right1, classes="status-row"),
            self.right2,
            **kwargs,
        )
        # dial cadence (SPINNER_INTERVAL): ~0.96s breath
        from oaset.tui.glyphs import SPINNER_INTERVAL

        self.set_interval(SPINNER_INTERVAL, self._tick)
        self.set_interval(10.0, self._next_tip)

    def set_model(self, model_id: str, provider: str) -> None:
        self._model = model_id
        self._refresh()

    def set_thinking(self, level: str) -> None:
        """Reasoning-depth badge (/think). Empty = provider default → hidden."""
        self._thinking = str(level or "")
        self._refresh()

    def _think_badge(self) -> str:
        # narrow bars already drop cwd/branch by width; the badge is short
        # enough to keep, but never displaces the busy state
        if not self._thinking:
            return ""
        from rich.markup import escape as _esc

        return f" ✦{_esc(self._thinking)}"

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh()

    def set_cwd(self, cwd: str) -> None:
        # Keep the full path and shorten at render time: pre-shortening to one
        # segment made the "last two segments below 96 cols" rule dead code —
        # the slice could only ever see the single segment already stored.
        self._cwd_full = str(cwd).replace("\\", "/")
        self._cwd = self._shorten(cwd)
        self._refresh()

    def set_tokens(self, tokens: int) -> None:
        self._tokens = tokens
        self._refresh()

    def set_max_context(self, max_tokens: int) -> None:
        self._max_context = max_tokens
        self._refresh()

    def set_session(self, session_id: str) -> None:
        self._session = session_id

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._busy_since = time.monotonic() if busy else None
        if not busy:
            self._note = ""
        self._refresh()

    def set_note(self, note: str) -> None:
        """Transient status note next to the spinner (retry countdown, …).
        Cleared on any turn-state change; never sticky like errors."""
        note = clip(note or "", 32)
        if note != self._note:
            self._note = note
            self._refresh()

    def set_branch(self, branch: str) -> None:
        self._branch = branch
        self._refresh()

    def set_goal(self, goal: str | None) -> None:
        """Short goal badge shown in the left status row ."""
        self._goal = clip(goal or "", 24)
        self._refresh()

    def set_error(self, notice) -> None:
        """Pin an error headline to the status bar until it is cleared.

        Token/model/session updates keep calling `_refresh`, but they never
        clear this: the previous behaviour replaced a failure with the next
        rotating tip, so the user could miss it entirely.
        """
        from oaset.tui.notice import UiNotice

        resolved = UiNotice.of(notice, level="error")
        self._error = clip(resolved.headline(), 70)
        self._error_hint = resolved.hint or ""
        self._refresh()

    def clear_error(self) -> None:
        if self._error:
            self._error = ""
            self._error_hint = ""
            self._refresh()

    def set_scroll_position(self, pct: float | None) -> None:
        """Chat scroll position (P1-1): None = no overflow / nothing to show.

        (Not `set_scroll` — that name collides with Widget.set_scroll.)

        Quantised to whole percent before the equality check. Every scroll event
        used to carry a slightly different fraction, so the guard never fired
        and `_refresh()` rebuilt the whole bar (markup parsing + three Statics +
        a style write) per pixel of scroll.
        """
        pct = None if pct is None else round(pct, 2)
        if self._scroll_pct == pct:
            return
        self._scroll_pct = pct
        self._refresh()

    def set_input_pos(self, line: int, total: int) -> None:
        """Input cursor position (P1-2): "行 x/y" once the draft multilines."""
        if self._input_line == line and self._input_total == total:
            return
        self._input_line, self._input_total = line, total
        self._refresh()

    def set_queue_count(self, n: int) -> None:
        """Enter-queue depth (P1-6): a persistent "queued n" badge."""
        if self._queue_n == n:
            return
        self._queue_n = n
        self._refresh()

    def set_bg_running(self, n: int) -> None:
        """Live background-task count, shown next to the busy spinner."""
        if self._bg_running == n:
            return
        self._bg_running = n
        self._refresh()

    @property
    def error_text(self) -> str:
        return self._error

    async def detect_branch_async(self, cwd: str) -> None:
        """Git branch probe off the UI thread.

        A cold `git rev-parse` can take seconds on network drives or busy
        repos; running it synchronously in on_mount froze the first frame
        (S2 in docs/PLAN.zh-CN.md)."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=3,
            )
        except Exception:
            return
        if result.returncode == 0 and result.stdout.strip():
            self.set_branch(result.stdout.strip())

    def _shorten(self, path: str) -> str:
        path = path.replace("\\", "/")
        parts = [p for p in path.rstrip("/").split("/") if p]
        if len(parts) <= 1:
            return path
        return parts[-1]

    def set_reduce_motion(self, value: bool) -> None:
        self.reduce_motion = bool(value)

    def _tick(self) -> None:
        if not self._busy:
            return  # idle: the 10s tip carousel owns refresh; don't repaint 8×/s
        if not self.reduce_motion:  # a11y: the spinner is motion
            self._spin += 1
        self._refresh()

    def _next_tip(self) -> None:
        """Footer tips carousel — only while idle and error-free."""
        if self._busy or self._error or not TIPS:
            return
        tips = self._tips()
        self._tip_index = (self._tip_index + 1) % len(tips)
        self._refresh()

    def _tips(self) -> list:
        """The carousel list: the /login tip is noise for mock/local models."""
        return TIPS if self._needs_key else TIPS[1:]

    def set_needs_key(self, value: bool) -> None:
        """Tell the bar whether the current model still needs a credential."""
        if self._needs_key != bool(value):
            self._needs_key = bool(value)
            self._tip_index = 0
            self._refresh()

    def _refresh(self) -> None:
        from rich.markup import escape as _esc

        try:
            width = self.size.width or 100
        except Exception:
            width = 100

        state = ""
        if self._busy:
            if self.reduce_motion:
                glyph = STATIC["busy"]
            else:
                # legacy consoles mangle the dial glyphs: the same fallback
                # the title/tool-card spinners use (half-applied before)
                try:
                    from oaset.tui.glyphs import spinner_frames

                    frames = spinner_frames(self.app)
                except Exception:
                    frames = SPINNER
                glyph = frames[self._spin % len(frames)]
            elapsed = ""
            if self._busy_since is not None:
                elapsed = f" {int(time.monotonic() - self._busy_since)}s"
            state = f"{glyph} {t('working')}…{elapsed}"
            # the anti-hang signal: a turn with no visible progress for 8s+
            # says so, instead of reading as a frozen UI
            stall = self.stalled_for() if self.stalled_for is not None else 0.0
            if stall > 8:
                state += f" [yellow]· {t('stalled_hint', n=int(stall))}[/yellow]"
            if self._note:
                state += f" [yellow]{_esc(self._note)}[/yellow]"
            if self._bg_running:
                state += f" · {t('bg_working', n=self._bg_running)}"
        elif self._mode == "plan":
            state = "⏸ " + t("plan_mode_badge")

        # auto (/yolo) auto-approves every tool call. It used to be announced by
        # a one-shot notice that scrolled away, so after a few turns the only
        # persistent region on screen said nothing about the permission policy
        # the agent was actually running under. `plan` already has a badge; this
        # is its security-relevant counterpart and outranks the busy text.
        if self._mode == "auto":
            badge = f"[bold yellow]{t('auto_mode_badge')}[/bold yellow]"
            if self._busy:
                state = f"{state}  {badge}" if state else badge
            else:
                state = badge

        if self._queue_n:
            # Persistent queue indicator (P1-6 follow-up): a one-shot notice
            # scrolls away, "queued 2" on the bar does not.
            state = (state + "  " if state else "") + f"[cyan]⏳ {t('queue_badge', n=self._queue_n)}[/cyan]"

        if self._error:
            # An unresolved error outranks the tips and the busy hint - but it
            # must not crowd out the model/cwd on the left, so the headline is
            # capped at half the bar INCLUDING the /errors marker (the marker
            # is the expansion entry, not an extra row).
            marker = f" {t('error_more')}"
            budget = max(10, width // 2 - visible_len(marker))
            right1 = (f"[red]✗ {_esc(clip(self._error, budget))}[/red]"
                      f"[dim]{marker}[/dim]")
        elif self._busy:
            right1 = t("hint_busy") if width >= 60 else ""
        else:
            tips = self._tips()
            if width < 72:
                # Narrow: a rotating tip is not worth the columns, but so is a
                # third of the row left blank. The mode badge already sits in the
                # left segment (plan/auto both render one), so repeating it here
                # would just waste the space twice — carry the session instead,
                # which is the one fact the left side drops first.
                if self._mode in ("plan", "auto"):
                    right1 = ""
                else:
                    short_sid = str(self._session)[-8:] if self._session else ""
                    right1 = f"[dim]{short_sid}[/dim]" if short_sid else ""
            else:
                tip_en, tip_zh = tips[self._tip_index % len(tips)]
                right1 = f"[dim]{tip_zh if resolve_language() == 'zh' else tip_en}[/dim]"

        context = ""
        if self._error:
            # P1-4: the pinned headline is one line only — keep the recovery
            # hint visible on row 2 until the error is cleared.
            context = f"[red]→ {clip(self._error_hint, max(20, width // 2))}[/red]" if self._error_hint else ""
        elif self._max_context:
            pct = min(100.0, 100.0 * self._tokens / self._max_context)
            used = f"{fmt_k(self._tokens)}/{fmt_k(self._max_context)}"
            # Idle + low usage: leave the tip the whole right side
            # density). Show a compact meter only when busy or the window is
            # actually filling.
            if self._busy or pct >= 50:
                body = used if pct < 50 else f"{t('context_label')} {pct:.0f}% {used}"
                context = body if self._busy else f"[{context_tier(pct)}]{body}[/]"
        if self._input_total > 1:
            context = (context + "  " if context else "") + f"[dim]{t('input_pos', line=self._input_line, total=self._input_total)}[/dim]"
        if self._scroll_pct is not None and self._scroll_pct < 0.995:
            context = (context + "  " if context else "") + f"[dim]↑ {int(self._scroll_pct * 100)}%[/dim]"

        # dynamic values (model/cwd/branch/goal/note/error) are user- or
        # provider-controlled and land in markup text — escaped above and here
        goal = f" ⌖ {_esc(self._goal)}" if self._goal else ""
        # Narrow bars: full-path cwd + branch crashed into the state segment
        # ("工作中…… 469s <cwd>"). Show the last 2 path segments below 96
        # cols, drop cwd below 64, drop branch below 80.
        cwd_disp = self._cwd
        if width < 64:
            cwd_disp = ""
        elif width < 96:
            parts = [p for p in (self._cwd_full or self._cwd).rstrip("/").split("/") if p]
            cwd_disp = "/".join(parts[-2:]) if len(parts) >= 2 else self._cwd
        left = f" {_esc(self._model)}{self._think_badge()} {state}{goal}"
        if cwd_disp:
            left += f" {_esc(cwd_disp)}"
        if self._branch and width >= 80:
            left += f"  {_esc(self._branch)}"
        extra = bool(self._error and self._error_hint) or self._input_total > 1 or (
            self._scroll_pct is not None and self._scroll_pct < 0.995
        )
        # markup-aware clip: clip() counts cells and happily cuts through a
        # [tag], which markup then rejects (MarkupError inside _tick)
        if extra:
            self.right2.display = True
            if self.styles.height is None or self.styles.height.value != 2:
                self.styles.height = 2
            self.left_text = clip_markup(left, max(10, width - visible_len(right1) - 2))
            self.right2.update(context)
            self.right2_text = context
        else:
            self.right2.display = False
            if self.styles.height is None or self.styles.height.value != 1:
                self.styles.height = 1
            if context:
                right1 = f"{context}  {right1}" if right1 else context
            self.left_text = clip_markup(left, max(10, width - visible_len(right1) - 2))
            self.right2.update("")
            self.right2_text = ""
        self.left.update(self.left_text)
        self.right1.update(right1)
        self.right1_text = right1


# Footer tips — rotated while idle.
TIPS = [
    ("/login then type", "/login 后直接提问"),
    ("/model to switch", "/model 换模型"),
    ("right-click copy/paste", "右键复制/粘贴"),
    ("esc interrupt · /undo files", "esc 中断 · /undo 回滚"),
    ("ctrl+o fold · ↑ history", "ctrl+o 折叠 · ↑ 历史"),
    ("/help commands", "/help 命令"),
    ("shift+drag = native select", "shift+拖拽 终端原生选择"),
    ("/context what eats the window", "/context 看谁在吃窗口"),
]


def context_tier(pct: float) -> str:
    """Status-bar 5-tier context coloring."""
    if pct < 50:
        return "dim"
    if pct < 75:
        return "green"
    if pct < 85:
        return "yellow"
    if pct < 95:
        return "red"
    return "bold red"
