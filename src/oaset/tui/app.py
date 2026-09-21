"""oAset TUI — the Textual application wiring everything together."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, TextArea

import oaset.tui._win_mouse_fix  # noqa: F401  (installs the Windows mouse fix on import)

# agent/agents imports are FUNCTION-LEVEL in this module: they used to sit
# on the first-frame path and cost ~150ms before the welcome painted
from oaset.config import (
    AppConfig,
    available_model_ids,
    save_config,
)
from oaset.i18n import t
from oaset.plugins import PluginCommand, load_plugins
from oaset.session import Session, SessionStore
from oaset.skills import find_skill, load_skills
from oaset.tools import AutoGate
from oaset.tui import commands
from oaset.tui.controllers.content_cmds import ContentCmdsMixin
from oaset.tui.controllers.market_ui import MarketUiMixin
from oaset.tui.controllers.mcp_admin_ui import McpAdminUiMixin
from oaset.tui.controllers.model_admin import ModelAdminMixin
from oaset.tui.controllers.notices import NoticesMixin
from oaset.tui.controllers.prompts import PromptsMixin
from oaset.tui.controllers.session_admin import SessionAdminMixin
from oaset.tui.controllers.system_admin import SystemAdminMixin
from oaset.tui.themes import (
    THEMES,
    load_custom_themes,
    normalize_theme,
    register_themes,
)
from oaset.tui.widgets.chat import ChatLog, ThinkingBlock
from oaset.tui.widgets.divider import BrandDivider
from oaset.tui.widgets.inline import (
    InlineInput,
    InlinePermission,
    InlinePromptBase,
)
from oaset.tui.widgets.inline import (
    help_text as build_help_text,
)
from oaset.tui.widgets.input import InputArea
from oaset.tui.widgets.palette import InlineCommandPalette
from oaset.tui.widgets.status_bar import StatusBar
from oaset.tui.widgets.tool_card import ToolCard
from oaset.utils import extract_image_attachments, oaset_home, run_io, workdir_bucket

if TYPE_CHECKING:
    from oaset.host import SessionHost
    from oaset.mcp import McpManager



class UIGate:
    """PermissionGate bridging the ToolRegistry to the inline permission panel."""

    def __init__(self, app: OasetApp) -> None:
        self.app = app

    async def request(self, tool_name: str, level: str, summary: str, preview: str | None = None) -> str:
        # One approval at a time: the single prompt slot turns a second,
        # concurrent ask into an auto-deny of the first (parallel read tools,
        # sub-agents). Queue instead — the panel's own timeout still bounds
        # a client that never answers.
        import asyncio as _aio

        lock = self.app.__dict__.get("_gate_lock")
        if lock is None:
            lock = self.app._gate_lock = _aio.Lock()
        async with lock:
            return await self.app.show_permission(tool_name, level, summary, preview)


class TurnController:
    """Owns the lifecycle of the single in-flight agent turn.

    One turn at a time is the product rule; every event, persistence write and
    busy-flag update carries the generation it was produced under, so a late
    write from a superseded turn is routed to the record it belongs to or
    dropped — never written into the session the user just switched to.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.session: Any | None = None
        self.conversation: Any | None = None
        self.worker: Any | None = None

    @property
    def active(self) -> bool:
        return self.worker is not None and bool(getattr(self.worker, "is_running", False))

    def begin(self, session: Any, conversation: Any, worker: Any) -> int:
        self.generation += 1
        self.session, self.conversation, self.worker = session, conversation, worker
        return self.generation

    def bump(self) -> int:
        """Invalidate the current turn without starting another one.

        Used when the session or the runtime is replaced: the in-flight turn
        keeps its own generation, so its events still reach the old record
        while nothing it emits is treated as current.
        """
        self.generation += 1
        self.session, self.conversation, self.worker = None, None, None
        return self.generation

    def is_stale(self, generation: int) -> bool:
        return generation != self.generation


class OasetApp(ContentCmdsMixin, McpAdminUiMixin, MarketUiMixin, ModelAdminMixin,
               NoticesMixin, PromptsMixin, SessionAdminMixin, SystemAdminMixin, App):
    TITLE = "oAset CLI"
    APPROVAL_TIMEOUT = 90  # seconds; safe default (timeout => deny)
    ELICIT_INPUT_TIMEOUT = 120  # seconds; MCP elicitation field must not hang the turn
    SIDEBAR_MIN_WIDTH = 100  # below this the screen is tagged `narrow`
    CSS = """
    * { scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }
    App { background: ansi_default; }
    /* [ui] density="compact" (P2 acceptance #5): tighter chat spacing. */
    .compact ToolCard { margin: 0 0 0 0; }
    .compact .assistant { margin-bottom: 0; }
    .compact UserMessage { margin-bottom: 0; }
    #chat { scrollbar-size-vertical: 1; }
    #input { scrollbar-size-vertical: 1; }
    Screen { layers: default overlay; background: ansi_default; }
    #main { height: 1fr; background: ansi_default; }
    #left { width: 1fr; background: ansi_default; }
    #chat { height: 1fr; background: ansi_default; }
    #prompt-row {
        height: auto; min-height: 1;
        background: ansi_default;
        /* the input row needs a visible frame: without it the composer sank
        into the transcript wall and was hard to locate at a glance */
        border: round #5f5f5f;
        padding: 0 1 0 0;
    }
    #prompt-row:focus-within { border: round $accent; }
    #prompt-glyph { width: 3; height: 1; color: $accent; padding: 0 0 0 1; background: ansi_default; }
    #input {
        width: 1fr;
        height: 1;
        padding: 0 1 0 0;
        background: ansi_default;
        border: none !important;
    }
    #input:focus { background: ansi_default; border: none !important; }
    #input .text-area--cursor-line, #input .text-area--cursor-gutter {
        background: ansi_default;
    }
    #working {
        display: none;
        height: 1;
        color: $secondary;
        padding: 0 1;
        background: ansi_default;
    }
    #suggest {
        display: none;
        height: auto;
        /* 16 option rows + ▲ + "more" line + the 2 border cells: 19 cut the
           last hint row off (border-box) exactly when a >16 list is scrolled */
        max-height: 21;
        padding: 0 1;
        background: ansi_default;
        /* the suggestion list needs its own frame: without one it reads as
        more transcript text (screenshot complaint) while the input below it
        is boxed — visually inconsistent */
        border: round #5f5f5f;
    }
    #sidebar {
        display: none;
        height: auto;
        /* title + 5 todo rows + the "… +N" overflow line: 6 clipped the
           overflow hint into invisibility (scrollbars are disabled) */
        max-height: 7;
        padding: 0 1;
        color: $secondary;
        background: ansi_default;
    }
    /* NB: the status bar styles itself in StatusBar.DEFAULT_CSS through classes
    (.status-left / .status-right / .status-right2). This stylesheet used to
    repeat them as #status-left / #status-right1 / #status-right2 plus a
    `#status` rule, but those widgets carry no ids, so all four matched nothing
    — they only made a later edit to the bar look like it should have applied. */
    .welcome-box {
        color: $secondary; margin: 0 0 1 0; width: 1fr;
        /* The hero lives INSIDE a full frame: rows are padded to one block
           width above, centring keeps the label columns aligned, and the
           border wraps the whole first screen. */
        text-align: center;
        border: round #5f5f5f;
        padding: 1 2;
    }
    .user-msg { color: $accent; text-style: bold; margin: 0; padding: 0; }
    .assistant { height: auto; margin: 0; padding: 0; }
    .assistant-md { margin: 0; padding: 0; }
    AssistantTurn Markdown { padding: 0 !important; margin: 0 !important; }
    .assistant-meta { color: $secondary; margin: 0; }
    .thinking-text { color: $secondary; text-style: italic; margin: 0; }
    .notice { margin: 0; }
    .notice-info { color: $secondary; }
    .notice-warn { color: $warning; }
    .notice-error { color: $error; }
    .chat-selected { text-style: underline; }
    .chat-card {
        height: auto;
        margin: 0;
        padding: 0;
        color: $secondary;
    }
    .login-banner {
        height: auto;
        padding: 0 1;
        color: $warning;
    }
    """

    BINDINGS = [
        # Interrupt lives on Escape ONLY: ctrl+x belongs to the input box's
        # cut semantics (Windows convention); a priority app binding used to
        # steal it and kill the running agent instead of cutting text.
        Binding("escape", "interrupt", "Interrupt", priority=True, show=False),
        Binding("ctrl+c", "quit_double", "Exit", priority=True),
        Binding("ctrl+k", "palette", "Command palette", show=True),
        Binding("ctrl+o", "toggle_collapse", "Fold tool output", show=False),
        Binding("shift+tab", "toggle_plan", "Plan mode", priority=True),
        Binding("ctrl+s", "inject", "Inject message", priority=True, show=False),
        Binding("ctrl+t", "toggle_sidebar", "Plan strip", show=False),
        Binding("ctrl+g", "external_editor", "Edit in $EDITOR", show=False),
        Binding("f2", "view_message", "View message (select & copy)", show=False),
        # Chat-wide copy: with a selection it copies that, otherwise the draft.
        Binding("ctrl+shift+c", "copy_selection", "Copy selection", show=False),
    ]

    async def action_palette(self) -> None:
        self.cmd_palette("")  # @work: returns a Worker, not a coroutine

    def __init__(
        self,
        cfg: AppConfig,
        cwd: Path,
        provider: Any | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        model_id: str | None = None,
        yolo: bool = False,
        thinking_level: str = "",
    ):
        super().__init__(ansi_color=True)  # keep ansi_default; skip MONOKAI fill
        self.cfg = cfg
        self.cwd = Path(cwd)  # callers may hand a str; keep every /-operation safe
        self.injected_provider = provider
        self._pending_images: list[Path] = []
        # The one mounted inline prompt. A single owner is what stops two
        # prompts from stacking (the input wedge: keys went to a stale panel
        # that nothing would remove).
        self._active_prompt: InlinePromptBase | None = None
        self._sidebar_pref: bool | None = None  # None = pin while the plan is live
        self.injected_session = session
        self.store = store or SessionStore()
        # config may request plan (read-only) at startup; unknown values fall back to default
        self.mode = cfg.permission_mode if cfg.permission_mode in ("default", "auto", "plan") else "default"
        self._lsp_manager: Any = None  # LspManager, imported lazily (set before any lifecycle hook can read it)
        self._boot_pending: list[str] = []
        self._welcomed = False
        self._init_model_id = model_id
        # --think at launch: seeded once the registry exists (host-ready),
        # which both boot paths reach
        self._startup_thinking = str(thinking_level or "").strip().lower()
        self._init_gate = AutoGate() if yolo else UIGate(self)
        # Optional-until-boot annotations: the fast-boot path defers host build
        self.host: SessionHost | None = None
        self.registry: Any = None
        self._kernel: Any = None
        self.conversation: Any = None
        self.session: Any = None
        self.system_prompt: str = ""
        self.provider: Any = None
        self._fallbacks: Any = None
        self.mcp: McpManager | None = None
        if provider is not None or session is not None:
            self._build_host(provider, session)
        else:
            # FAST BOOT (a real `oaset` start): painting the welcome first and
            # building the provider in a background worker cuts time to the
            # first frame by the openai import alone (~1.6s measured).
            from oaset.config import resolve_model

            self.model_cfg = resolve_model(cfg, model_id)
            self.session = self.store.new_session(cwd, self.model_cfg.id)
            self.injected_session = self.session  # host adopts it: same id on screen

        self._mcp_tool_names: set[str] = set()
        self.active_card: Any | None = None
        self.tool_runs: dict[str, Any] = {}
        self._pending_cards: list[Any] = []
        self._pending_runs: list[Any] = []
        self._custom_themes: set[str] = set()
        self.plugin_commands: dict[str, PluginCommand] = {}
        self._agent_worker: Any | None = None
        self._turn = TurnController()
        self._last_ctrl_c = 0.0
        self.input_queue: list[str] = []  # Enter while running queues (P1-6)
        self._interrupt_requested = False
        self.flusher_turns: list[Any] = []
        self.running_cards: set = set()  # live spinner registry (P4-3)
        self._deferred_provider_close: Any | None = None
        self._deferred_mcp: Any | None = None

    def _build_host(self, provider: Any | None, session: Any | None) -> None:
        """Construct the SessionHost and wire every attribute derived from it.

        Runs at init for injected providers (tests/mock) and in a boot worker
        for a real `oaset` start — the provider chain pays the openai import
        (~1.6s) and must not sit in front of the first frame.
        """
        from oaset.host import SessionHost  # deferred: pulls the agent chain

        self.host = SessionHost(
            self.cfg, self.cwd, model_id=self._init_model_id,
            provider=provider,
            gate=self._init_gate, mode=self.mode,
            session=session, store=self.store, persist=True,
            persist_usage=False,  # the TUI dispatcher owns the pending-io write
        )
        # The MCP connect path only mounts elicitation when host.elicit is
        # set; without this line every elicitation-capable server got -32601
        # until a manual /mcp reload.
        self.host.elicit = self._elicit_field
        self.model_cfg = self.host.model_cfg
        self.provider = self.host.provider
        self._fallbacks = self.host.fallbacks
        self.registry = self.host.registry
        self._kernel = self.host.kernel
        self.mcp = self.host.mcp
        self.conversation = self.host.conversation
        self.session = self.host.session
        self.system_prompt = self.host.system_prompt

        def _persist_allow_tool(tool_name: str) -> None:
            entry = f"{tool_name}::{workdir_bucket(self.cwd)}"
            if entry not in self.cfg.allow_tools and tool_name not in self.cfg.allow_tools:
                self.cfg.allow_tools.append(entry)
                self.run_worker(run_io(save_config, self.cfg), group="util", exclusive=False)
            self.chat.add_notice(t("allow_persisted", tool_name=tool_name), "info")

        self.registry.ctx.persist_allow = _persist_allow_tool

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        self.chat_log = ChatLog()
        self.suggest_box = Static("", id="suggest")
        self.working_line = Static("", id="working", markup=False)
        self.prompt_glyph = Static("❯", id="prompt-glyph", markup=False)
        self.input_box = InputArea()
        self.side_bar = Vertical(id="sidebar")
        self.status_widget = StatusBar()
        self.brand_divider = BrandDivider()
        with Vertical(id="main"):
            with Vertical(id="left"):
                yield self.chat_log
                yield self.suggest_box
                yield self.working_line
                yield self.side_bar
                with Horizontal(id="prompt-row"):
                    yield self.prompt_glyph
                    yield self.input_box
        yield self.brand_divider
        yield self.status_widget

    async def on_unmount(self) -> None:
        """Release MCP servers and the provider HTTP client on shutdown.

        Background shell tasks are killed first: their process trees would
        otherwise outlive the TUI (the cleanup Job is the backstop, not the
        mechanism)."""
        worker = self._agent_worker
        if worker is not None and worker.is_running:
            worker.cancel()
        with contextlib.suppress(Exception):
            if self._lsp_manager is not None:
                await self._lsp_manager.shutdown()
        if self.host is not None:  # fast boot may still be warming
            with contextlib.suppress(Exception):
                await self.host.aclose()

    def on_mount(self) -> None:
        import os as _os

        # A4 fix: language/theme must be loaded BEFORE any visible widget is
        # populated, so the welcome panel, placeholder and status bar render in
        # the configured language on the first frame.
        from oaset.i18n import set_language

        set_language(self.cfg.ui_language)
        if _os.name == "nt":
            # Kernel-enforced cleanup: every child (MCP servers, shell
            # commands) dies with this process, even on taskkill /F.
            from oaset.sandbox import attach_current_process_to_cleanup_job

            with contextlib.suppress(Exception):
                attach_current_process_to_cleanup_job()
        if _os.name == "nt" and not (_os.environ.get("WT_SESSION")
                                     or _os.environ.get("TERM_PROGRAM")):
            # legacy = a console we cannot trust with fancy glyphs (raster
            # fonts). Windows Terminal AND modern Electron terminals (VS Code
            # et al, TERM_PROGRAM) render ◔◑◕ fine; only bare conhost on
            # old builds needs the dot-pulse fallback.
            self.add_class("legacy")
        register_themes(self, oaset_home())
        self._custom_themes = set(load_custom_themes(oaset_home()))
        self.theme = normalize_theme(self.cfg.ui.theme, extra=self._custom_themes)
        if self.cfg.ui.density == "compact":
            self.add_class("compact")
        self.set_interval(0.12, self._flush_turns)
        from oaset.tui.glyphs import SPINNER_INTERVAL

        self.set_interval(SPINNER_INTERVAL, self._spin_cards)
        input_area = self.query_one(InputArea)
        self.plugin_commands = {}
        self._loaded_plugins: list[str] = []
        from oaset.plugins import LoadedPlugin

        self._plugin_records: list[LoadedPlugin] = []  # enables /reload-plugins retraction
        input_area.placeholder = t("input_placeholder")
        from oaset.utils import workspace_files

        input_area.suggest_source = lambda prefix="": commands.suggest(prefix, self.plugin_commands)
        input_area.file_source = lambda query: workspace_files(self.cwd, query)
        input_area.bind_suggest(self._show_suggest, self._hide_suggest)
        # Accessibility: freeze cursor blink + spinners when motion is reduced
        input_area.cursor_blink = not bool(self.cfg.ui.reduce_motion)
        self.status_bar.set_reduce_motion(bool(self.cfg.ui.reduce_motion))
        self.status_bar.set_model(self.model_cfg.id, self.model_cfg.provider)
        # fast-boot paths reach here before the registry binds; /think and
        # /model refresh the badge from that side once state exists
        try:
            self.status_bar.set_thinking(str(
                self.registry.ctx.session_state.get("thinking_level") or ""))
        except AttributeError:
            pass
        self.status_bar.set_mode(self.mode)
        self.status_bar.set_cwd(str(self.cwd))
        self.status_bar.set_session(self.session.meta.session_id)
        self.status_bar.set_busy(False)
        # Anti-idle-signal: while a turn runs, the bar says how long since the
        # last visible progress — "工作中…… 469s" with nothing else read as a hang.
        self._last_activity_at = time.monotonic()
        self.status_bar.stalled_for = lambda: time.monotonic() - self._last_activity_at
        self._branch_worker = self.run_worker(
            self.status_bar.detect_branch_async(str(self.cwd)),
            group="branch", exclusive=False)
        with contextlib.suppress(Exception):
            # the hero shows the branch once known: repaint it exactly once
            self._branch_worker.add_done_callback(lambda _w: self._refresh_welcome())
        self._working_visible = False
        input_area.focus()
        self.call_after_refresh(self.chat.scroll_home, animate=False)
        if self.host is None:
            # fast boot: paint NOW, build the provider (openai import ~1.6s)
            # in a worker; the welcome session id stays valid because the host
            # adopts the pre-created session record
            self._welcomed = True
            self._welcome()
            self.run_worker(self._boot_host_async(), group="boot", exclusive=True)
        else:
            self._after_host_ready()

    async def _boot_host_async(self) -> None:
        """Warm the runtime off the paint path, then finish wiring."""
        self._build_host(self.injected_provider, self.injected_session)
        self._after_host_ready()
        pending, self._boot_pending = self._boot_pending, []
        for text in pending:  # every message the user sent during warm-up
            # from_queue semantics: the hold path above ALREADY rendered the
            # bubble — re-submitting without the flag painted it twice for
            # the rest of the session
            self.call_after_refresh(self.submit_text, text, from_queue=True)

    def _after_host_ready(self) -> None:
        """Everything that needs the registry/kernel — runs once the host
        exists (immediately for injected providers, post-boot otherwise)."""
        # --think from launch seeds here (registry exists now); and whatever
        # the host seeded from the model config finally shows its badge —
        # fast-boot read the state before the registry existed
        state = self.registry.ctx.session_state
        if getattr(self, "_startup_thinking", ""):
            from oaset.thinking import normalize_level

            _level = normalize_level(self._startup_thinking)
            if _level:
                state["thinking_level"] = _level
                state["thinking_level_set"] = True
        self.status_bar.set_thinking(str(state.get("thinking_level") or ""))
        self._plugin_failures: list[str] = []
        self._loaded_plugins = load_plugins(
            self.cwd, self.registry, self.plugin_commands,
            log=lambda t: self.chat.add_notice(t, "info"),
            loaded=self._plugin_records,
            failures=self._plugin_failures)
        self.status_bar.set_tokens(self.conversation.token_estimate())
        from oaset.agent.subagent_runner import make_subagent_runner
        from oaset.agents import load_agents

        agents = load_agents(self.cwd)
        self.registry.ctx.session_state["agents"] = {a.name.lower(): a for a in agents}
        self.registry.ctx.session_state["subagent_runner"] = make_subagent_runner(
            self.provider,
            self.registry,
            self.registry.ctx,
            max_iterations=self.cfg.max_iterations,
            context_max_tokens=self.model_cfg.max_context_size,
            config=self.cfg,
            on_event=self._on_subagent_event,
        )
        # replaced per turn by _run_agent with a generation-bound closure
        self.registry.ctx.session_state["checkpoint_sink"] = (
            lambda snapshot: self._checkpoint_sink(self._turn.generation, self.session, snapshot))
        self.registry.ctx.session_state["stream_sink"] = (
            lambda chunk: self._stream_sink(self._turn.generation, chunk))
        self.registry.ctx.session_state["ask_user"] = self._ask_user
        # production wiring for the spill-to-disk feature (Hermes-parity H2):
        # without this key nothing ever spilled, so oversized tool output was
        # only ever clamped in the UI with no recoverable file behind it
        self.registry.ctx.session_state["tool_output_dir"] = str(oaset_home() / "spill")
        self.registry.ctx.session_state["set_mode"] = self.set_mode
        self.registry.ctx.session_state["restore_mode"] = self.restore_mode
        if not self._welcomed:
            self._welcomed = True
            self._welcome()
        # One prompt at most on a fresh install: the language picker. A
        # keyless model stays quiet — the welcome page badges it and the
        # send path refuses with a /login hint when it is actually used.
        self._first_run_flow()  # @work group="language"
        self._discover_local_models()
        self._mcp_worker = self._load_mcp()
        self._lsp_worker = self._start_lsp()
        # Probe the usable shell now, in a worker thread: lazily this can cost
        # up to ~8s per bad candidate in the middle of a turn (P0-2).
        self.run_worker(self._warmup_shell(), group="warmup", exclusive=False)

    @on(TextArea.Changed, "#input")
    def _input_content_changed(self, event: TextArea.Changed) -> None:
        self.input_area._sync_auto_size()

    @on(TextArea.SelectionChanged, "#input")
    def _input_selection_moved(self, event: TextArea.SelectionChanged) -> None:
        self.input_area._sync_auto_size()

    # ------------------------------------------------------------ properties

    @property
    def chat(self) -> ChatLog:
        return self.chat_log

    @property
    def status_bar(self) -> StatusBar:
        return self.status_widget

    @property
    def input_area(self) -> InputArea:
        return self.input_box

    # ------------------------------------------------------------- lifecycle

    async def _persist_message(self, generation: int, session: Any, message) -> None:
        """Persist one message to the session the turn belongs to.

        The store write runs on the ordered IO thread: disk latency never
        blocks the turn, and the single worker keeps JSONL append order (S2)."""
        try:
            await run_io(self.store.append_message, session, message)
        except Exception as exc:
            self.notify_error(exc, source="session", code="session.save_failed")
            return
        if not self._turn.is_stale(generation):
            self.status_bar.set_tokens(self.conversation.token_estimate())

    def _flush_turns(self) -> None:
        # Timer bodies must never kill the app (S6): a render bug in one turn
        # used to be a fatal Textual exit; now it is a visible error notice.
        try:
            for turn in self.flusher_turns:
                if not turn._done:
                    turn.flush()
            for run in self.tool_runs.values():
                card = getattr(run, "card", None)
                if card is not None and getattr(card, "has_class", lambda *_: False)("tool-running"):
                    card.flush()
        except Exception as exc:
            self.notify_error(exc, source="render", code="render.flush_failed")

    # ------------------------------------------------------- tool activity

    def activity_start(self, tool_id: str, name: str, summary: str,
                       raw_args: str = "") -> Any:
        """Mount a running tool line NOW. Finished modules stay in the
        transcript and scroll up; only the working strip is pinned."""
        from oaset.tui.turn_view import ToolRun
        from oaset.tui.widgets.tool_card import ToolCard

        run = ToolRun(id=tool_id, name=name, summary=summary, raw_args=raw_args)
        card = ToolCard(name, summary, raw_args=raw_args, auto_peek=False)
        self.chat._add_widget(card)
        run.card = card
        self.tool_runs[tool_id] = run
        self._pending_cards.append(card)
        self._pending_runs.append(run)
        self.active_card = run
        self._refresh_activity_line()
        return run

    def activity_finish(self, tool_id: str, result: str, is_error: bool,
                        run: Any | None = None) -> None:
        """Complete a run already sitting in the transcript."""
        run = run or self.tool_runs.get(tool_id)
        if run is None:
            return
        run.result = result
        run.is_error = is_error
        run.elapsed = time.monotonic() - run.started
        if run.card is not None:
            run.card._started = run.started
            run.card.finish(result, is_error=is_error)
        self.active_card = None
        self._refresh_activity_line()
        # density: a long run of finished calls folds behind a ⋯ chip
        self.chat._fold_tool_runs()

    def activity_anchor_segment(self, seg) -> None:
        """No-op: cards mount immediately, so they need no flush-time anchor."""
        return

    def flush_tool_activity(self) -> None:
        from oaset.tui.turn_view import flush_tool_activity as _flush

        _flush(self)

    def _refresh_activity_line(self) -> None:
        """The working line doubles as the activity strip (2 rows max):
        row 1 = the running tool with a spinner, row 2 = progress + counts."""
        if not self._working_visible:
            return
        from oaset.tui.glyphs import STATIC, spinner_frames

        reduce_motion = bool(self.cfg.ui.reduce_motion)
        glyphs = spinner_frames(self)
        self._spin_pos = 0 if reduce_motion else getattr(self, "_spin_pos", 0) + 1
        running = [r for r in self.tool_runs.values() if r.elapsed is None]
        row1 = ""
        if running:
            run = running[-1]
            spin = STATIC["tool_running"] if reduce_motion else glyphs[self._spin_pos % len(glyphs)]
            name = run.card._display_name() if run.card is not None else run.name
            label = t("tool_used", name=name)
            row1 = f"{spin} {label} ({run.summary})" if run.summary else f"{spin} {label}"
        done = [r for r in self.tool_runs.values() if r.elapsed is not None]
        counts = f"✓{sum(1 for r in done if not r.is_error)} ✗{sum(1 for r in done if r.is_error)}"
        elapsed = ""
        busy_since = getattr(self.status_bar, "_busy_since", None)
        if busy_since is not None:
            elapsed = f" · {int(time.monotonic() - busy_since)}s"
        bg = (self.registry.ctx.session_state.get("background_tasks")
              if self.registry is not None else None)
        bg_n = len(bg.list(active_only=True)) if bg is not None else 0
        extra = f" · {t('bg_working', n=bg_n)}" if bg_n else ""
        if row1:
            self.working_line.update(f"{row1}  {counts}{elapsed}{extra}")
        else:
            marker = STATIC["busy"] if reduce_motion else glyphs[self._spin_pos % len(glyphs)]
            self.working_line.update(f"{marker} {t('working')}… {counts}{elapsed}{extra}")
        self.status_bar.set_bg_running(bg_n)

    def _spin_cards(self) -> None:
        try:
            self._refresh_activity_line()
            # The per-card animation lives on the card: without this tick a
            # running tool's glyph froze on its first frame and the elapsed
            # counter never appeared, so a 90s build looked like a hang (the
            # card is the only thing in the transcript that says "still alive").
            for run in self.tool_runs.values():
                card = getattr(run, "card", None)
                if card is not None and run.elapsed is None:
                    card.spin_tick()
        except Exception as exc:
            self.notify_error(exc, source="render", code="render.spin_failed")

    # ----------------------------------------------------------- interaction

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        self.submit_text(event.text)

    def _handle_exception(self, error: Exception) -> None:
        """A crashed worker or handler must not take the app down.

        Textual's default hook exits with a traceback. The typing path
        already funnelled command errors (see _on_command_done), but the
        palette path and @work tasks did not: one bad plugin, a mistyped
        [update] catalog_url or a malformed cron.json killed the session
        with a panic screen. Report through the notice funnel and keep
        running; the exception still lands in _exception so pilot-based
        tests see it.
        """
        if self._exception is None:
            self._exception = error
            with contextlib.suppress(Exception):
                self._exception_event.set()
        with contextlib.suppress(Exception):
            self.log.error("unhandled exception", error)
        with contextlib.suppress(Exception):
            self.notify_error(error, code="app.unhandled", source="app",
                              hint=t("command_crashed_hint"))

    def _on_command_done(self, task: "asyncio.Task[Any]") -> None:
        """Surface a crashed slash command instead of swallowing it.

        `submit_text` fires commands as fire-and-forget tasks; without this the
        exception is only emitted as an unretrieved-task warning, which a
        full-screen TUI never shows the user.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        with contextlib.suppress(Exception):
            self.notify_error(exc, code="command.crashed", source="command",
                              hint=t("command_crashed_hint"))
        if getattr(self, "_debug", False):
            with contextlib.suppress(Exception):
                self.log.error("command crashed", exc_info=exc)

    def submit_text(self, text: str, *, from_queue: bool = False) -> None:
        """Send a user message as its own turn.

        ``from_queue``: the message was already rendered by the queue preview
        (submit-while-running), so it must not be added to the chat again —
        the old path rendered every queued message twice.
        """
        if text.startswith("/"):
            # A command that raises used to vanish: the task's exception was
            # never retrieved, so the user saw nothing at all. Report it.
            task = asyncio.create_task(commands.execute(self, text))
            task.add_done_callback(self._on_command_done)
            return
        self.status_bar.clear_error()  # a new prompt means the user moved on
        self.last_error = None  # /errors must not resurrect a dismissed error
        self._last_user_prompt = text
        self._drain_background_notices()
        if self.host is None:
            # runtime still warming in the boot worker: hold EVERY text (a
            # plain overwrite used to drop all but the last one) — they
            # auto-send in order the moment the provider is ready
            self._boot_pending.append(text)
            self.chat.add_user(text)
            self.chat.add_notice(t("boot_pending"), "info")
            return
        if self._worker_running():
            # Queued, not injected: the message runs as its OWN turn once the
            # current one ends (P1-6). Ctrl+S remains the mid-stream steer.
            self.input_queue.append(text)
            self.status_bar.set_queue_count(len(self.input_queue))
            self.chat.add_user(text)
            self.chat.add_notice(t("queued_at", n=len(self.input_queue)), "info")
            return
        if not self._model_ready_for_send():  # no-key preflight
            self.input_area.load_text(text)
            self.input_area.move_cursor(self.input_area.document.end)
            return
        # A collapsed paste is a readability device for the composer, never the
        # message. Every path above can hand the draft back to the user (queued,
        # not-ready, still booting), so the marker is only expanded here, at the
        # point where a turn is actually created: otherwise the turn would carry
        # a pointer to a file under ~/.oaset that the model cannot read, and the
        # user's paste would be silently discarded (see tui/paste.py).
        from oaset.tui.paste import find_paste_refs, resolve_draft
        from oaset.utils import oaset_home

        original_refs = find_paste_refs(text)
        if original_refs:
            text, expanded = resolve_draft(text, oaset_home() / "pastes")
            if len(expanded) < len(original_refs):
                self.chat.add_notice(t("paste_expand_failed"), "warn")
        pending, self._pending_images = self._pending_images, []
        if pending:
            if self.model_cfg.has("image_in"):
                from oaset.utils import image_path_to_part

                pending_parts = [p for p in (image_path_to_part(path) for path in pending)
                                 if p is not None]
                images = [*pending_parts, *extract_image_attachments(text, self.cwd, enabled=True)]
            else:
                images = None
                self.chat.add_notice(t("image_ignored_unsupported", model=self.model_cfg.id), "warn")
        else:
            images = extract_image_attachments(text, self.cwd, enabled=self.model_cfg.has("image_in"))
        if not from_queue:
            self.chat.add_spacer()
            self.chat.add_user(text)
        self.title = text[:60]
        session = self.session
        gen = self._turn.begin(session, self.conversation, None)
        self._agent_worker = self._run_agent(text, images or None, gen, session)
        self._turn.worker = self._agent_worker

    def _worker_running(self) -> bool:
        return self._agent_worker is not None and self._agent_worker.is_running

    async def drain_turn(self, timeout: float = 5.0) -> bool:
        """Cancel the in-flight turn and wait for it to actually stop.

        Used before replacing the runtime or the session record: the caller
        gets a definite "the old turn is no longer writing" answer instead of
        racing a worker that is still mid-stream.
        """
        worker = self._agent_worker
        if worker is None or not worker.is_running:
            return True
        worker.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker.wait(), timeout=timeout)
        return not worker.is_running

    def _release_deferred_closes(self) -> None:
        """Close providers/MCP managers that were retired during a live turn.

        `/reload` cannot close the old client while a turn is streaming through
        it, so it parks the closeable here; the turn that was running releases it.
        """
        pending, self._deferred_provider_close = self._deferred_provider_close, None
        if pending is not None:
            with contextlib.suppress(Exception):
                self.run_worker(pending(), group="util", exclusive=False)
        pending_mcp, self._deferred_mcp = self._deferred_mcp, None
        if pending_mcp is not None:
            with contextlib.suppress(Exception):
                self.run_worker(pending_mcp(), group="util", exclusive=False)

    @staticmethod
    def _chain_close(previous: Any, new_close: Any):
        """Compose two deferred closers so `/reload` twice still closes both."""
        if previous is None:
            return new_close

        async def _both() -> None:
            for closer in (previous, new_close):
                with contextlib.suppress(Exception):
                    await closer()

        return _both

    @work(group="agent")
    async def _run_agent(self, text: str, images: list[dict] | None = None,
                         generation: int | None = None,
                         session: Any | None = None) -> None:
        generation = self._turn.generation if generation is None else generation
        session = session or self.session
        conversation = self._kernel.conversation
        # Bind persistence and the tool sinks to THIS turn's record, so a late
        # callback after a session switch lands on the old session instead of
        # contaminating the new one.
        self._kernel.on_message = lambda message: self._persist_message(generation, session, message)
        previous_checkpoint = self.registry.ctx.session_state.get("checkpoint_sink")
        previous_stream = self.registry.ctx.session_state.get("stream_sink")
        self.registry.ctx.session_state["checkpoint_sink"] = (
            lambda snapshot: self._checkpoint_sink(generation, session, snapshot))
        self.registry.ctx.session_state["stream_sink"] = (
            lambda chunk: self._stream_sink(generation, chunk))

        host = self.host
        if host is None:
            return  # submit_text gates on boot, but stay safe — BEFORE any
            # busy state, or this early return left the spinner hanging
        self.status_bar.set_busy(True)
        self._set_working(True)
        turn = self.chat.start_assistant()
        turn_started = time.monotonic()
        self.flusher_turns = [t for t in self.flusher_turns if not t._done]
        self.flusher_turns.append(turn)
        cards: dict[str, ToolCard] = {}
        self.active_card = None
        self.tool_runs = {}
        self._pending_io: Any | None = None  # awaitable parked by the dispatcher
        self.last_error = None  # full UiNotice, expandable via /errors

        try:
            async for event in host.chat(text, images=images):
                if self._turn.is_stale(generation):  # superseded: never write
                    continue
                self._handle_event(event, turn, cards, turn_started)
                # The dispatcher is sync (tests call it directly), so a handler
                # that needs ordered IO parks its awaitable here and the turn
                # awaits it now: a dropped run_io Future loses the "write is
                # done" guarantee and reports its failure to nobody.
                pending = self._pending_io
                if pending is not None:
                    self._pending_io = None
                    try:
                        await pending
                    except Exception as exc:
                        # A failed usage/audit write must not kill the turn, but
                        # it must not vanish either (see turn_view's comment).
                        self.notify_error(exc, code="session.usage_write_failed",
                                          source="session")
        except asyncio.CancelledError:
            (self.chat.active_turn or turn).finish(partial=True)
            self.chat.seal_assistant()
            self.chat.add_notice(t("interrupted_notice"), "warn")
            raise
        except Exception as exc:
            if getattr(self, "last_error", None) is None:  # error event already rendered it
                self.notify_error(exc, source="agent", hint=t("agent_failed_hint"))
        finally:
            if self.registry.ctx.session_state.get("checkpoint_sink") is not None:
                self.registry.ctx.session_state["checkpoint_sink"] = previous_checkpoint
            self.registry.ctx.session_state["stream_sink"] = previous_stream
            self._finish_turn(generation, conversation)
            self._release_deferred_closes()

    def _finish_turn(self, generation: int, conversation) -> None:
        """Retire the busy state for a finishing turn — the CURRENT one only.

        An old worker settling late must not clear a newer turn's spinner, nor
        overwrite its token count with a stale estimate.
        """
        if self._turn.is_stale(generation):
            return
        # Every finish path seals the reply block: an error-interrupted turn
        # used to leave "Thinking…" streaming forever (block unexpandable,
        # flusher spinning every 120ms) because only the cancel path sealed.
        active = getattr(self.chat, "active_turn", None)
        if active is not None and not getattr(active, "_done", False):
            with contextlib.suppress(Exception):
                active.finish(partial=True)
                self.chat.seal_assistant()
        self.flush_tool_activity()  # collapsed cards slot in chronologically
        self.status_bar.set_busy(False)
        self._set_working(False)
        self.status_bar.set_tokens(conversation.token_estimate())
        self._refresh_sidebar()
        self._turn.worker = None
        # a steer the loop never drained (last answer had no tool call) must
        # not silently ride into the NEXT turn/session — say it was dropped
        leftovers = getattr(self._kernel, "injections", None)
        if leftovers:
            n = len(leftovers)
            leftovers.clear()
            self.chat.add_notice(t("steer_not_delivered", n=n), "warn")
        interrupted = self._interrupt_requested
        self._interrupt_requested = False  # consumed unconditionally: a late
        # Esc during teardown used to leak into the next turn's branch
        if interrupted:
            # The user cancelled: hold the queue instead of firing into a turn
            # they just killed. The next Enter sends the first queued item.
            if self.input_queue:
                self.chat.add_notice(t("queue_kept", n=len(self.input_queue)), "info")
            return
        if self.input_queue:
            text = self.input_queue.pop(0)
            self.status_bar.set_queue_count(len(self.input_queue))
            if self.input_queue:
                self.chat.add_notice(t("queue_sending", n=len(self.input_queue)), "info")
            # the queue preview already rendered this message (submit_text's
            # queue branch) — do not render it a second time. The deferred
            # callback carries the session it was queued FOR: fired during a
            # session swap it used to become the NEW session's first message
            # while its preview had been cleared.
            import functools

            sid = self.session.meta.session_id if self.session is not None else ""
            self.call_after_refresh(
                functools.partial(self._submit_queued, text, sid))

    def _submit_queued(self, text: str, session_id: str) -> None:
        current = self.session.meta.session_id if self.session is not None else ""
        if session_id and current != session_id:
            self.chat.add_notice(t("queue_dropped_switch"), "warn")
            return
        self.submit_text(text, from_queue=True)

    def _handle_event(self, event, turn,
                      cards: dict[str, ToolCard],
                      turn_started: float = 0.0) -> None:
        """One-line delegate: the dispatch lives in tui/turn_view.py."""
        from oaset.tui.turn_view import handle_turn_event

        kind = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        if kind in ("assistant_delta", "reasoning_delta", "tool_call_started",
                    "tool_output_delta", "tool_completed", "turn_completed"):
            self._last_activity_at = time.monotonic()
        handle_turn_event(self, event, turn, cards, turn_started)

    # ------------------------------------------------------------- bindings

    async def action_interrupt(self) -> None:
        # A selection is the most recent, cheapest thing to undo: Escape clears
        # it first, so it cannot eat the "cancel the turn" press.
        if self.selected_text():
            self.chat.clear_selection()
            return
        if self.input_area._suggest_visible:
            self.input_area._hide_suggest()
            return
        prompts = list(self.query(InlinePromptBase))
        if prompts:
            top = prompts[-1]
            # Escape unwinds one level at a time: clear the palette filter,
            # then (on a second Escape) close the form without running anything.
            if isinstance(top, InlineCommandPalette) and top.back():
                return
            top._resolve(None)
            # ...and make sure it actually goes away. A prompt whose future was
            # already consumed had nothing left to remove it, so Escape appeared
            # to do nothing while the panel kept eating keystrokes.
            await self._dismiss_prompt(top)
            if self._worker_running():
                # closing a panel (ask_user, permission) cancels the QUESTION,
                # not the turn: second Esc stops the agent, and the user
                # deserves to know that two-stage rule
                self.chat.add_notice(t("esc_again_hint"), "info")
            with contextlib.suppress(Exception):
                self._restore_focus(self.input_box)
            return
        if self._worker_running() and self._agent_worker is not None:
            self._interrupt_requested = True
            self._agent_worker.cancel()

    def action_quit_double(self) -> None:
        now = time.monotonic()
        if now - self._last_ctrl_c < 2.0:
            # exiting mid-turn used to drop the queue without a word and race
            # the cancel path for the last streamed output: say what is lost
            # and give the cancellation a beat to land
            if self.input_queue:
                n = len(self.input_queue)
                self.input_queue.clear()
                self.chat.add_notice(t("queue_dropped_exit", n=n), "warn")
            worker = self._agent_worker
            if worker is not None and getattr(worker, "is_running", False):
                with contextlib.suppress(Exception):
                    worker.cancel()
            self.exit()
            return
        self._last_ctrl_c = now
        # through the notice funnel (chat + copyable), not a Textual toast
        self.chat.add_notice(t("quit_confirm"), "info")

    def action_toggle_collapse(self) -> None:
        # Folded runs are unreachable by keyboard otherwise: the chip only
        # unfolded on click and invisible cards never entered this loop.
        from oaset.tui.widgets.chat import FoldChip

        chips = list(self.chat.query(FoldChip))
        if chips:
            if self.chat.follow:
                # expanding grows virtual_size; follow-tail would fling the
                # view to the bottom, putting the just-expanded content many
                # screens away
                self.chat.follow = False
            for chip in chips:
                chip.unfold()
        for card in self.chat.query(ToolCard):
            card.toggle_expand()
        for block in self.chat.query(ThinkingBlock):
            block.toggle()

    def _set_working(self, visible: bool) -> None:
        self._working_visible = visible
        working = self.working_line
        working.display = visible
        if visible:
            working.update(f" ⁝ {t('working')}")

    def action_toggle_plan(self) -> None:
        if self.query(InlinePromptBase):
            return
        if self.mode == "plan":
            self.restore_mode()
        else:
            self.set_mode("plan")

    def action_inject(self) -> None:
        if self.query(InlinePromptBase):
            return
        text = self.input_area.text.strip()
        if not text:
            self.chat.add_notice(t("inject_nothing"), "warn")
            return
        self.input_area.load_text("")
        if self._worker_running():
            self._kernel.injections.append(text)
            self.chat.add_user(text)
        else:
            self.submit_text(text)

    def action_toggle_sidebar(self) -> None:
        if self.registry is None:  # fast-boot window
            return
        self._sidebar_pref = False if self.side_bar.display else True
        self._refresh_sidebar()

    def on_resize(self, event: Any) -> None:
        if self.screen_stack:
            self.screen.set_class(event.size.width < self.SIDEBAR_MIN_WIDTH, "narrow")

    # ------------------------------------------------------------ internals

    def _stream_sink(self, generation: int, chunk: str) -> None:
        if self._turn.is_stale(generation):
            return  # a superseded turn must not paint into the live view
        self.activity_stream(chunk)

    def plan_progress(self) -> tuple[int, int]:
        """(completed, total) of the session plan; (0, 0) when there is none."""
        if self.registry is None:  # fast-boot window
            return 0, 0
        todos = self.registry.ctx.session_state.get("todos", [])
        if not todos:
            return 0, 0
        done = sum(1 for td in todos if td.get("status") == "completed")
        return done, len(todos)

    def set_mode(self, mode: str) -> None:
        if mode == "restore":
            mode = getattr(self, "_mode_before_plan", "default") or "default"
            if mode not in ("default", "auto"):
                mode = "default"
        if mode not in ("default", "plan", "auto"):
            return
        if mode == "plan" and self.mode != "plan":
            self._mode_before_plan = self.mode
        self.mode = mode
        self.registry.ctx.mode = mode
        if self.host is not None:
            self.host.mode = mode
            self.host._mode_before_plan = getattr(self, "_mode_before_plan", "default")
        self.cfg.permission_mode = "auto" if mode == "auto" else "default"
        self.status_bar.set_mode(mode)
        self.chat.add_notice(
            {
                "default": t("mode_notice_default"),
                "plan": t("mode_notice_plan"),
                "auto": t("mode_notice_auto"),
            }[mode],
            "warn" if mode == "auto" else "info",
        )

    def restore_mode(self) -> str:
        """Leave plan mode; return to whatever was active before entering it."""
        self.set_mode("restore")
        return self.mode

    # ------------------------------------------------------------- notices


    # ------------------------------------------------------------- commands

    async def _run_prompt(self, widget: InlinePromptBase) -> Any:
        """Run one inline form, then restore the focus that was active before it.

        This is a focus stack expressed through the call stack: every nested
        prompt (a permission panel raised while a picker is open) captures the
        widget focused at its own entry, so unwinding restores the right one
        instead of unconditionally snapping focus to the input box.
        """
        # A pending PERMISSION panel must not be superseded: opening Ctrl+K
        # (or any prompt) while an approval waits used to auto-deny the tool
        # — "look at the command list" silently rejected a write. User
        # prompts queue behind the decision, which has its own timeout.
        deadline = time.monotonic() + 130
        while getattr(self._active_prompt, "_is_permission", False) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        previous = self.focused
        self._mount_prompt(widget)
        widget.focus()
        try:
            return await widget.future
        finally:
            await self._dismiss_prompt(widget)
            self._restore_focus(previous)

    async def _input(self, title: str, placeholder: str = "",
                     password: bool = False) -> str | None:
        """Inline free-text (or masked) prompt; None when cancelled.

        An empty submission resolves to "" (not None) so callers can tell
        "user pressed Enter to keep the current value" from "user cancelled".
        """
        widget = InlineInput(title, placeholder, password=password,
                             allow_empty=True)
        value = await self._run_prompt(widget)
        if value is None:
            return None
        return str(value)

    async def _pick_spec_choice(self, spec) -> str | None:
        """Enum form: the declared choices plus a manual-entry row.

        The manual row is what keeps a `choice` spec from being a dead end when
        the value the user needs is not in the list.
        """
        from oaset.tui.commands import localized_spec
        from oaset.tui.widgets.palette import CUSTOM_CHOICE

        title, localized = localized_spec(spec)
        options = list(localized)
        if spec.allow_custom:
            options.append((CUSTOM_CHOICE, t("palette_choice_custom")))
        choice = await self._pick(title, options)
        if choice is None or choice != CUSTOM_CHOICE:
            return choice
        return await self._run_prompt(InlineInput(title, spec.placeholder))

    @staticmethod
    def _path_rejection(candidate: Path, suffixes: tuple[str, ...]) -> str | None:
        """Return a human reason the path is unusable, or None when it is fine."""
        if not candidate.is_file():
            return t("image_not_found", path=candidate)
        if suffixes and candidate.suffix.lower() not in suffixes:
            return t("image_bad_suffix", path=candidate, suffixes=", ".join(suffixes))
        if suffixes:
            from oaset.utils import MAX_IMAGE_BYTES, fmt_bytes

            try:
                size = candidate.stat().st_size
            except OSError:
                return None
            if size > MAX_IMAGE_BYTES:
                return t("image_too_large", path=candidate, size=fmt_bytes(size),
                         limit=fmt_bytes(MAX_IMAGE_BYTES))
        return None

    async def show_permission(self, tool_name: str, level: str, summary: str, preview: str | None = None) -> str:
        widget = InlinePermission(tool_name, level, summary, preview)
        widget._is_permission = True  # user prompts must queue behind it
        try:
            decision = await asyncio.wait_for(self._run_prompt(widget), timeout=self.APPROVAL_TIMEOUT)
        except TimeoutError:
            self.chat.add_notice(t("approval_timeout", tool=tool_name), "warn")
            return "deny"
        return decision or "deny"

    def cmd_help(self, args: str) -> None:
        show_all = args.strip().lower() in ("all", "full", "-a", "--all")
        self.chat.add_card(build_help_text(all_commands=show_all), pin_start=True)

    @work(group="command", exclusive=True)
    async def cmd_palette(self, args: str) -> None:
        """Ctrl+K: a filterable command table, then a parameter form, then a
        confirmation page for anything that writes.

        Cancelling a parameter form goes BACK to the palette (nothing runs);
        only cancelling the palette itself leaves the flow.
        """
        initial = args.strip()
        while True:
            choice = await self.run_palette(initial)
            if not choice:
                return
            if await self.run_command_flow(choice):
                return
            initial = ""  # returned from a form/confirm: show the full table

    async def run_palette(self, initial_query: str = "") -> str | None:
        from oaset.tui.commands import alias_map, command_metadata
        from oaset.tui.widgets.palette import InlineCommandPalette

        palette = InlineCommandPalette(
            command_metadata(self.plugin_commands), initial_query, alias_map())
        return await self._run_prompt(palette)

    def _update_history(self, manager) -> str:
        records = manager.history(limit=30)
        if not records:
            return t("update_history_empty")
        lines = [f"[b]{t('update_history_title')}[/b]"]
        for rec in records:
            what = rec.get("target") or ", ".join(rec.get("targets") or []) or \
                ", ".join(rec.get("applied") or []) or rec.get("txn") or ""
            suffix = f"  v{rec['version']}" if rec.get("version") else ""
            if rec.get("failed"):
                suffix += f"  ✗ {rec['failed']}"
            lines.append(f"  {rec.get('ts', '?')}  {rec.get('action', '?'):>10}  {what}{suffix}")
        return "\n".join(lines)

    def _report_update(self, result, *, source: str) -> None:
        """Show an update result, escalating its errors to the status bar."""
        self.chat.add_card(result.render() if hasattr(result, "render") else str(result))
        if getattr(result, "errors", None):
            self.notify_error("\n".join(str(e) for e in result.errors),
                              code=f"{source}.failed", source="update",
                              hint=t("update_error_hint"))
        else:
            self.status_bar.clear_error()

    def cmd_exit(self, args: str) -> None:
        self.exit()

    async def action_view_message(self) -> None:
        """F2: open the last answer in the suspend viewer (P2-4)."""
        await self.cmd_view("")

    def _model_picker_options(self) -> list[tuple[str, str]]:
        """Usable models first; presets without a credential are labelled and
        sorted last, and the add/relay entry is the default pick when nothing
        is usable — a preset must never look ready when it is not."""
        from oaset.config import model_availability

        usable: list[tuple[str, str]] = []
        blocked: list[tuple[str, str]] = []
        for mid, model in sorted(self.cfg.models.items()):
            state, provider = model_availability(self.cfg, mid)
            label = f"{model.display_name or mid} · {mid}"
            if state == "ready":
                usable.append((mid, f"✓ {label}"))
            else:
                blocked.append((mid, f"· {label} — {t('model_needs_key', provider=provider)}"))
        add_row = ("__add__", t("models_menu_add"))
        list_row = ("__list__", t("models_menu_list"))
        if usable:
            return [*usable, add_row, list_row, *blocked]
        return [add_row, list_row, *blocked]

    def _existing_key(self, provider_name: str) -> str:
        """Credential already stored for this provider, if any."""
        pcfg = self.cfg.providers.get(provider_name)
        raw = (pcfg.api_key if pcfg else "") or ""
        if raw and raw != "mock" and not raw.startswith("env:"):
            return raw
        if raw.startswith("env:"):
            from oaset.config import resolve_api_key

            if resolve_api_key(raw).strip():
                return resolve_api_key(raw)
        try:
            from oaset.credentials import load_credential

            return load_credential(provider_name)
        except Exception:
            return ""

    async def _resolve_model_limits(self, provider_name: str, base_url: str,
                                   api_key: str, model_id: str,
                                   api_format: str) -> tuple[int, int, str]:
        """Ask the provider, then the vendor catalogue, then fall back.

        Token limits are metadata, not a user decision: the wizard resolves them
        so nobody has to type 128000/32768 by hand.
        """
        from oaset.model_limits import discover_limits, resolve_limits
        from oaset.provider_catalog import vendor_limits

        discovered: dict[str, int] = {}
        if api_format == "openai" and base_url:
            try:
                discovered = await discover_limits(
                    base_url, api_key, model_id, api_format=api_format,
                    network_mode=self.cfg.network_mode)
            except Exception:
                discovered = {}
        return resolve_limits(discovered=discovered,
                              vendor=vendor_limits(provider_name))

    async def _pick_base_url(self, provider: str, default: str, validate) -> str | None:
        """Offer the vendor default, or a manually entered, validated URL."""
        options: list[tuple[str, str]] = []
        if default:
            options.append((default, t("wizard_base_url_use_default", url=default)))
        options.append(("__custom__", t("wizard_base_url_custom")))
        choice = await self._pick(t("wizard_base_url_title", provider=provider), options)
        if not choice:
            return None
        if choice == "__custom__":
            while True:
                raw = await self._input(t("wizard_base_url_title", provider=provider),
                                        default or "https://")
                if raw is None:
                    return None
                try:
                    return validate(raw)
                except ValueError as exc:
                    self.chat.add_notice(str(exc), "warn")
        return choice

    async def _input_positive_int(self, title: str, placeholder: str) -> int | None:
        while True:
            raw = await self._input(title, placeholder)
            if raw is None:
                self.chat.add_notice(t("wizard_cancelled"), "info")
                return None
            try:
                value = int(raw)
            except ValueError:
                self.chat.add_notice(t("wizard_number_invalid", value=raw), "warn")
                continue
            if value <= 0:
                self.chat.add_notice(t("wizard_number_invalid", value=raw), "warn")
                continue
            return value

    def _apply_theme(self, theme: str) -> None:
        self.theme = normalize_theme(theme, extra=self._custom_themes)
        valid = theme == "auto" or theme in THEMES or theme in self._custom_themes
        self.cfg.ui.theme = theme if valid else "oaset-dark"
        # A swallowed save_config here announced "theme set" while the choice
        # silently reverted on the next start. Report it instead.
        persisted = self._persist_config(what="ui.theme")
        self._refresh_welcome()  # the ramp flips between dark and light
        with contextlib.suppress(Exception):
            self.brand_divider.refresh_brand()
        if persisted:
            self.chat.add_notice(t("theme_set", theme=self.cfg.ui.theme), "info")

    @work(group="command", exclusive=True)
    async def cmd_mode(self, args: str) -> None:
        if args.strip() in ("default", "plan", "auto"):
            self.set_mode(args.strip())
            return
        choice = await self.prompt_missing_arg("mode")
        if choice:
            self.set_mode(choice)

    @work(group="lsp")
    async def _start_lsp(self) -> None:
        from oaset.lsp import LspManager, load_lsp_config

        servers = load_lsp_config()
        if not servers:
            return
        manager = LspManager(servers, self.cwd)
        started = await manager.start_all()
        if started:
            self._lsp_manager = manager
            self.registry.ctx.session_state["lsp_manager"] = manager
            self.chat.add_notice(t("lsp_connected", servers=", ".join(started)), "info")

    async def _ask_user(self, question: str, choices: list[str]) -> str:
        """Called by the ask_user tool: mount an inline picker and await the answer."""
        self.chat.add_spacer()
        self.chat.add_notice(t("ask_user_prefix", question=question), "info")
        return await self._pick(t("picker_ask_user"), [(c, c) for c in choices]) or ""

    async def _elicit_field(self, title: str, placeholder: str) -> str | None:
        """One elicitation field via a free-text inline prompt.

        BOUNDED: an unnoticed inline input must not hang the turn forever —
        ask_user got this rule in P0-6, elicitation takes the same deal.
        Timeout maps to cancel (None), which the bridge reports to the server."""
        from oaset.tui.widgets.inline import InlineInput

        try:
            return await asyncio.wait_for(
                self._run_prompt(InlineInput(title, placeholder)),
                timeout=self.ELICIT_INPUT_TIMEOUT)
        except TimeoutError:
            self.chat.add_notice(
                t("elicit_timeout", seconds=int(self.ELICIT_INPUT_TIMEOUT)), "warn")
            return None

    @work(group="mcp")
    async def _load_mcp(self) -> None:
        if self.host is None:
            return  # post-boot only (spawned from _after_host_ready)
        results = await self.host.mcp_start()
        adapters: list = self.host.mcp.adapters() if self.host.mcp is not None else []
        self.mcp = self.host.mcp
        self._mcp_tool_names = {adapter.name for adapter in adapters}
        self._attach_sampling_bridge()
        if results:
            line = self.mcp.status_line() if self.mcp is not None else t("mcp_not_started")
            self.chat.add_notice(line, "info" if adapters else "warn")

    def cmd_reload_skills(self, args: str) -> None:
        self._refresh_skills_prompt()
        skills = load_skills(self.cwd)
        self.chat.add_notice(t("skills_reloaded", count=len(skills)), "info")

    def _refresh_skills_prompt(self) -> None:
        """Rebuild the system prompt from disk skills (no unbounded append).

        Routed through `_apply_goal_to_prompt` so the session's goal block
        survives a skills reload: rebuilding the prompt from scratch here used
        to silently drop the goal while `session_state["goal"]` still reported
        an active goal.
        """
        self._apply_goal_to_prompt()

    def _make_subagent_runner(self, provider, context_max_tokens: int):
        """Sub-agent runner wired with config (per-agent models) and progress."""
        from oaset.agent.subagent_runner import make_subagent_runner

        return make_subagent_runner(
            provider, self.registry, self.registry.ctx,
            max_iterations=self.cfg.max_iterations,
            context_max_tokens=context_max_tokens,
            config=self.cfg,
            # live: /goal budget can change after the runner was built
            budget_tokens=lambda: getattr(self._kernel, "budget_tokens", 0),
            on_event=self._on_subagent_event,
        )

    def _on_subagent_event(self, agent: str, kind: str, payload: dict) -> None:
        """Live sub-agent activity into the running tool record (or a notice)."""
        run = self.active_card
        line = ""
        if kind == "start":
            line = "  ↳ " + t("subagent_started", agent=agent)
        elif kind == "tool_start":
            line = "  ↳ " + t("subagent_tool", agent=agent,
                              name=str(payload.get("name", "")))
        elif kind == "tool_end":
            mark = "✗" if payload.get("is_error") else "✓"
            line = "  ↳ " + t("subagent_tool_mark", agent=agent,
                              name=str(payload.get("name", "")), mark=mark)
        elif kind == "timeout":
            line = "  ↳ " + t("subagent_timeout", agent=agent)
        elif kind == "error":
            # the final report carries the text, but the LIVE card went
            # silent for a dying sub-agent until this branch existed
            line = "  ↳ " + t("subagent_error", agent=agent,
                              error=str(payload.get("error", ""))[:120])
        elif kind == "done":
            line = "  ↳ " + t("subagent_done", agent=agent)
        if not line:
            return  # deltas are too chatty for the card; final text follows anyway
        if run is not None:
            run.result += line + "\n"
        else:
            self.chat.add_notice(line.strip(), "info")

    async def _roots_menu(self) -> None:
        """Interactive roots management (`/mcp` → roots, `/roots` bare)."""
        await self.cmd_roots("")

    async def _checkpoint_sink(self, generation: int, session: Any, snapshot: dict[str, str | None]) -> None:
        """Record a checkpoint against the session the turn belongs to (IO thread).

        A superseded turn's late checkpoint must not land in the live record."""
        if self._turn.is_stale(generation):
            return
        try:
            await run_io(self.store.checkpoint, session, snapshot)
        except Exception as exc:
            self.notify_error(exc, source="session", code="session.checkpoint_failed")

    def _agent_row(self, a) -> str:
        model = a.model or t("agent_parent_model")
        tools = ", ".join(a.tools) if a.tools else "all"
        scope = a.scope or "user"
        return f"{a.name} · {scope} · {model} · tools: {tools} · {a.description}"

    def _agents_reload(self, agents=None) -> None:
        """Reload definitions and install them for the task tool immediately."""
        from oaset.agents import load_agents

        agents = load_agents(self.cwd) if agents is None else agents
        self.registry.ctx.session_state["agents"] = {a.name.lower(): a for a in agents}
        return agents

    def _agent_show(self, name: str) -> None:
        from oaset.agents import find_agent

        agent = find_agent(self.cwd, name)
        if agent is None:
            self.chat.add_notice(t("agents_unknown", name=name), "warn")
            return
        head = "\n".join(agent.system_prompt.splitlines()[:10])
        limits = []
        if agent.max_iterations:
            limits.append(f"max_iterations={agent.max_iterations}")
        if agent.timeout_seconds:
            limits.append(f"timeout={agent.timeout_seconds:.0f}s")
        if getattr(agent, "thinking", ""):
            limits.append(f"thinking={agent.thinking}")
        self.chat.add_card(
            f"[b]{agent.name}[/b] ({agent.scope or 'user'})\n{agent.description}\n"
            f"[dim]{agent.path} · model={agent.model or 'inherit'}"
            + (f" · {' '.join(limits)}" if limits else "") + "[/dim]\n\n" + head)

    def _agents_usage(self) -> None:
        usage = self.registry.ctx.session_state.get("subagent_usage") or {}
        if not usage:
            self.chat.add_notice(t("agents_usage") + " (none)", "info")
            return
        lines = [f"[b]{t('agents_usage')}[/b]"]
        for name, u in sorted(usage.items()):
            lines.append(f"  {name}: {u.get('runs', 0)} runs · "
                         f"↑{u.get('prompt_tokens', 0)} ↓{u.get('completion_tokens', 0)} · "
                         f"total {u.get('total_tokens', 0)} tok")
        self.chat.add_card("\n".join(lines))

    async def _agent_name_picker(self) -> str | None:
        from oaset.agents import load_agents

        agents = load_agents(self.cwd)
        if not agents:
            self.chat.add_notice(t("no_subagents"), "info")
            return None
        return await self._pick(t("agents_menu_title"),
                                [(a.name, self._agent_row(a)) for a in agents])

    def _mcp_list(self) -> None:
        from oaset.tui.mcp_admin import list_servers

        rows = list_servers(oaset_home(), self.cwd)
        status = self.mcp.status_line() if self.mcp is not None else t("mcp_not_started")
        lines = [f"[b]{t('mcp_menu_title')}[/b]", escape(status)]
        if not rows:
            lines.append(t("mcp_empty"))
        for row in rows:
            state = "●" if row.enabled else "○"
            extra = f"  {escape(row.tool_filter)}" if row.tool_filter else ""
            lines.append(f"{state} {escape(row.name)}  {escape(row.transport)}  "
                         f"{escape(row.target)}  ({row.scope}){extra}")
        self.chat.add_card("\n".join(lines))

    async def _mcp_scope(self) -> str | None:
        from oaset.tui.mcp_admin import SCOPES

        assert "project" in SCOPES and "user" in SCOPES
        return await self._pick(t("mcp_scope_title"), [
            ("project", t("mcp_scope_project")),
            ("user", t("mcp_scope_user")),
        ])

    async def _mcp_apply_prompt(self) -> None:
        choice = await self._pick(t("mcp_confirm_reload"), [
            ("now", t("mcp_apply_yes")),
            ("later", t("mcp_apply_later")),
        ])
        if choice == "now":
            await self._reload_mcp_now()
        elif choice == "later":
            self.chat.add_notice(t("mcp_added_hint"), "info")

    async def cmd_export(self, args: str) -> None:
        raw = args.strip() or f"oaset-session-{self.session.meta.session_id[-8:]}.md"
        target = Path(raw)
        if not target.is_absolute():
            # relative to the SESSION workspace (meta.cwd) — self.cwd is the
            # launch directory and wrote exports into the wrong repo when a
            # session from another workspace was restored
            base = Path(self.session.meta.cwd) if self.session.meta.cwd else self.cwd
            target = base / target
        try:
            result = await run_io(self.store.export_markdown,
                                  self.session.meta.session_id, target)
        except OSError as exc:
            self.notify_error(exc, code="session.export_failed", source="session",
                              text=t("export_failed_reason", err=str(exc)))
            return
        if result is not None:
            self.chat.add_notice(t("export_ok", path=result), "info")
        else:
            self.notify_error(t("export_failed"), code="session.export_failed",
                              source="session", hint=t("export_failed_hint"))

    def cmd_yolo(self, args: str) -> None:
        self.set_mode("auto" if self.mode != "auto" else "default")

    def _skill_load(self, name: str) -> None:
        skill = find_skill(self.cwd, name)
        if skill is None:
            self.chat.add_notice(t("skill_not_found", name=name), "warn")
            return
        self._refresh_skills_prompt()
        marker = f"# Active skill: {skill.name}"
        if marker not in self.system_prompt:
            self.system_prompt = (
                f"{self.system_prompt}\n\n{marker}\n"
                f"Follow `{skill.path}` for this turn. Call read_file on that "
                f"path if you need the full steps.\n"
            )
            self.conversation.system_prompt = self.system_prompt
            if self.host is not None:
                self.host.system_prompt = self.system_prompt
                self.host.conversation.system_prompt = self.system_prompt
        self.chat.add_notice(
            t("skill_loaded", name=skill.name) + f"\n{skill.path}", "info")

    def _skill_show(self, name: str) -> None:
        skill = find_skill(self.cwd, name)
        if skill is None:
            self.chat.add_notice(t("skill_not_found", name=name), "warn")
            return
        head = "\n".join(skill.body.splitlines()[:12])
        self.chat.add_card(
            f"[b]{skill.name}[/b]\n{skill.description}\n"
            f"[dim]{skill.path} - {len(skill.body)} chars[/dim]\n\n{head}")

    async def _skill_name_picker(self) -> str | None:
        skills = load_skills(self.cwd)
        if not skills:
            self.chat.add_notice(t("no_skills"), "info")
            return None
        return await self._pick(t("skills_menu_title"),
                                [(s.name, s.description or s.name) for s in skills])

    def cmd_config(self, args: str) -> None:
        lines = [t("config_header", path=oaset_home() / "config.toml"),
                 t("config_cwd", cwd=self.cwd), t("config_providers")]
        for name, p in self.cfg.providers.items():
            key = "set" if (p.api_key and not p.api_key.startswith("env:")) else p.api_key or t("none_marker")
            lines.append(f"  {name}: {p.base_url} · key {key}")
        lines.append(t("config_models", models=", ".join(available_model_ids(self.cfg))))
        self.chat.add_notice("\n".join(lines), "info")
