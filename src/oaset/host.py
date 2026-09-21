"""SessionHost — the public session runtime every surface drives.

TUI, SDK, one-shot CLI, gateway, cron, and a future desktop skin are views
over this object. JSONL under ``~/.oaset/sessions`` is the source of truth
(the same contract pi-gui uses: UI does not keep a second transcript).

    from oaset.host import SessionHost

    async with SessionHost(cwd=".", persist=True) as host:
        async for event in host.chat("fix the tests"):
            send_ipc(event.as_dict())   # desktop / any skin
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oaset.agent import Conversation, initial_system_prompt
from oaset.config import AppConfig, load_config
from oaset.errors import SessionBusyError
from oaset.hooks import HookRunner
from oaset.kernel import AgentKernel, Event
from oaset.network import NetworkPolicy
from oaset.runtime import build_runtime
from oaset.session import Session, SessionMeta, SessionStore
from oaset.tools import AutoGate, ToolContext, ToolRegistry, workspace_allow_tools
from oaset.utils import oaset_home

if TYPE_CHECKING:
    from oaset.mcp import McpManager

__all__ = ["SessionHost"]


class SessionHost:
    """One session, one runtime, one event stream.

    Surfaces must not construct :class:`AgentLoop` or a second conversation.
    Swap sessions through :meth:`attach_session` / :meth:`new_session` so the
    kernel, MCP tools and persistence stay on the same host.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        cwd: str | Path | None = None,
        *,
        model_id: str | None = None,
        provider: Any | None = None,
        gate: Any | None = None,
        mode: str | None = None,
        tools: bool = True,
        yolo: bool = False,
        mcp: bool = True,
        persist: bool = True,
        session: Session | None = None,
        store: SessionStore | None = None,
        hooks: dict[str, str] | None = None,
        elicit: Callable[[str, str], Awaitable[str | None]] | None = None,
        on_message: Callable[[Any], Any] | None = None,
        max_iterations: int | None = None,
        auto_compact: bool = True,
        budget_tokens: int = 0,
        persist_usage: bool = True,
    ) -> None:
        self.cfg = config or load_config()
        self.cwd = Path(cwd or Path.cwd())
        auto = yolo or self.cfg.permission_mode == "auto"
        self.mode = mode or ("auto" if auto else "default")
        rt = build_runtime(
            self.cfg, self.cwd, model_id=model_id, provider=provider,
            gate=gate if gate is not None else (AutoGate() if auto else None),
            mode=self.mode, tools=tools,
        )
        self.model_cfg = rt.model_cfg
        self.provider = rt.provider
        self.fallbacks = rt.fallbacks
        self.registry = rt.registry
        self.agents = rt.agents
        self.tools_enabled = tools
        self.policy = NetworkPolicy(self.cfg.network_mode)
        self.max_iterations = (
            self.cfg.max_iterations if max_iterations is None else max_iterations
        )
        self.auto_compact = auto_compact
        self.budget_tokens = int(budget_tokens or 0)
        self.hooks_map: dict[str, str] = (
            dict(hooks) if hooks is not None else dict(self.cfg.hooks)
        )
        self.elicit = elicit
        self.mcp_enabled = mcp
        self.mcp: McpManager | None = None
        self._mcp_started = False
        self._mcp_starting = False
        self._turns_running = 0
        self._closed = False
        self._mode_before_plan = self.mode if self.mode in ("default", "auto") else "default"

        self.registry.ctx.session_allowed.update(
            workspace_allow_tools(self.cfg.allow_tools, self.cwd)
        )
        # per-model thinking default ("" = provider default); /think can
        # override it for the rest of this process run
        from oaset.thinking import normalize_level

        _level = normalize_level(getattr(self.model_cfg, "thinking", "") or "")
        if _level and "thinking_level" not in self.registry.ctx.session_state:
            self.registry.ctx.session_state["thinking_level"] = _level
        if tools:
            from oaset.agent.subagent_runner import make_subagent_runner

            self.registry.ctx.session_state["subagent_runner"] = make_subagent_runner(
                self.provider, self.registry, self.registry.ctx,
                max_iterations=self.cfg.max_iterations,
                context_max_tokens=self.model_cfg.max_context_size,
                config=self.cfg,
            )
            self.registry.ctx.session_state["set_mode"] = self.set_mode
            self.registry.ctx.session_state["restore_mode"] = self.restore_mode

        system_prompt, _ = initial_system_prompt(self.cwd)
        self.system_prompt = system_prompt
        self.store = store or SessionStore()
        self.persist = persist
        # usage lines: the door writes them for every surface EXCEPT the TUI,
        # whose dispatcher owns the ordered pending-io write (see turn_view)
        self.persist_usage = persist_usage
        if tools and "checkpoint_sink" not in self.registry.ctx.session_state:
            # Headless surfaces get checkpoints too: every WRITE tool declares
            # revertible files, but only the TUI ever installed a sink, so
            # outside it /undo had nothing to restore.
            self.registry.ctx.session_state["checkpoint_sink"] = self._default_checkpoint_sink
        if session is None:
            if persist:
                session = self.store.new_session(self.cwd, self.model_cfg.id)
                if not session.conversation.system_prompt:
                    session.conversation.system_prompt = system_prompt
            else:
                session = Session(
                    meta=SessionMeta(
                        session_id=f"ephemeral-{id(self):x}",
                        path=Path(""),
                        cwd=str(self.cwd),
                        model=self.model_cfg.id,
                    ),
                    conversation=Conversation(system_prompt=system_prompt),
                    file=None,
                )
        elif not session.conversation.system_prompt:
            session.conversation.system_prompt = system_prompt
        self.session = session
        self.conversation = session.conversation
        self.session.conversation = self.conversation

        persist_cb = on_message
        if persist_cb is None and persist:
            persist_cb = self._persist_message
        self.kernel = AgentKernel(
            provider=self.provider,
            fallbacks=self.fallbacks,
            registry=self.registry if tools else self._bare_registry(),
            conversation=self.conversation,
            max_iterations=self.max_iterations,
            max_tool_calls=self.cfg.max_tool_calls,
            context_max_tokens=self.model_cfg.max_context_size,
            auto_compact=self.auto_compact,
            on_compact=self._persist_compaction if persist else None,
            budget_tokens=self.budget_tokens,
            hooks=HookRunner(self.hooks_map, self.cwd, network_mode=self.cfg.network_mode),
            on_message=persist_cb,
            session_id=self.session.meta.session_id,
        )

    def _bare_registry(self) -> ToolRegistry:
        registry = ToolRegistry(gate=AutoGate(), tools=[])
        registry.bind(ToolContext(cwd=self.cwd, mode="auto", output_limit=200))
        return registry

    def _default_checkpoint_sink(self, snapshot: dict) -> None:
        session = getattr(self, "session", None)
        if session is None or session.file is None or not snapshot:
            return
        try:
            self.store.checkpoint(session, snapshot)
        except Exception:
            pass  # a checkpoint failure must not fail the tool call

    def _persist_message(self, message: Any) -> None:
        self.store.append_message(self.session, message)

    def _persist_compaction(self, summary: str, dropped: int) -> None:
        """Auto-compaction writes its marker to the transcript, like /compact."""
        self.store.append_compaction(self.session, summary, dropped)

    # ------------------------------------------------------------------ session

    def set_gate(self, gate: Any) -> None:
        """Swap the permission gate AFTER construction (ACP learns the
        session id only once the host exists).

        The single public seam: the gate lives on the registry and is
        copied into the context at bind time, so both spots must move.
        Surfaces that poked those internals directly broke silently the
        next time the binding changed."""
        self.registry.gate = gate
        if self.registry.ctx is not None:
            self.registry.ctx.gate = gate

    def attach_session(self, session: Session) -> None:
        """Point this host at an already-loaded session (TUI / desktop switch)."""
        # a steer typed for the previous session must not ride along into
        # this one's first turn (it used to arrive there, inverted)
        with contextlib.suppress(Exception):
            self.kernel.injections.clear()
        # session-scoped tool state must not survive the swap: todos are the
        # previous conversation's plan, the evidence flag its audit setting,
        # the search budget its per-conversation quota
        if self.registry is not None and self.registry.ctx is not None:
            state = self.registry.ctx.session_state
            for key in ("todos", "evidence", "search_budget", "subagent_usage"):
                state.pop(key, None)
        if not session.conversation.system_prompt:
            session.conversation.system_prompt = self.system_prompt
        self.session = session
        self.conversation = session.conversation
        self.kernel.conversation = self.conversation
        self.kernel.session_id = session.meta.session_id

    def new_session(self) -> Session:
        # a steer typed for the previous session must not ride along
        with contextlib.suppress(Exception):
            self.kernel.injections.clear()
        session = self.store.new_session(self.cwd, self.model_cfg.id)
        session.conversation = Conversation(system_prompt=self.system_prompt)
        self.attach_session(session)
        self.registry.ctx.session_state.pop("todos", None)
        self.registry.ctx.session_allowed.clear()
        self.registry.ctx.session_allowed.update(
            workspace_allow_tools(self.cfg.allow_tools, self.cwd)
        )
        return session

    def snapshot(self) -> dict[str, Any]:
        """Full state for a late-joining view (desktop window, reconnect).

        The status keys below are stable; the schema_version field gates
        breaking changes. Messages serialize through Message.to_dict — the
        same shape the session store persists, so a restored snapshot equals
        a restored session. No credentials ever appear here.
        """
        state = self.registry.ctx.session_state
        usage = self.kernel.last_usage if isinstance(self.kernel.last_usage, dict) else {}
        return {
            "schema_version": 1,
            "session_id": self.session.meta.session_id,
            "cwd": str(self.cwd),
            "model": self.model_cfg.id,
            "provider": self.model_cfg.provider,
            "message_count": len(self.conversation.messages),
            "turn_active": self.kernel.turn_active,
            "network_mode": self.cfg.network_mode,
            "path": str(self.session.meta.path),
            # full-view additions (G0): everything a desktop pane must render
            "mode": self.mode,
            "title": self.session.meta.title,
            "todos": list(state.get("todos", []) or []),
            "goal": state.get("goal") or None,
            "usage": dict(usage),
            "messages": [m.to_dict() for m in self.conversation.messages],
        }

    def inject(self, text: str) -> None:
        self.kernel.inject(text)

    def set_mode(self, mode: str) -> None:
        """Permission mode for this host (plan / default / auto). `restore` leaves plan."""
        if mode == "restore":
            mode = self._mode_before_plan if self._mode_before_plan in ("default", "auto") else "default"
        if mode not in ("default", "plan", "auto"):
            return
        if mode == "plan" and self.mode != "plan":
            self._mode_before_plan = self.mode if self.mode in ("default", "auto") else "default"
        self.mode = mode
        if self.registry is not None and self.registry.ctx is not None:
            self.registry.ctx.mode = mode

    def restore_mode(self) -> str:
        self.set_mode("restore")
        return self.mode

    # ------------------------------------------------------------------ turns

    async def chat(
        self, prompt: str, images: list[dict] | None = None
    ) -> AsyncIterator[Event]:
        if self.mcp_enabled and not self._mcp_started:
            await self.mcp_start()
        self.kernel.hooks = HookRunner(
            self.hooks_map, self.cwd, network_mode=self.cfg.network_mode
        )
        self.kernel.registry = (
            self.registry if self.tools_enabled else self._bare_registry()
        )
        self._turns_running += 1
        try:
            with self._cross_surface_turn_lock():
                async for event in self.kernel.chat(prompt, images=images):
                    # /evidence receipts persist at the door: every surface (TUI,
                    # ACP, gateway, one-shot) shares this loop, so the audit chain
                    # lands in the transcript no matter who drove the turn.
                    if getattr(event, "type", "") == "evidence":
                        items = list((event.data or {}).get("items") or [])
                        if items and self.persist and self.session is not None:
                            try:
                                self.store.append_evidence(self.session, items)
                            except Exception:
                                pass  # a receipt write must not kill the turn
                    if (getattr(event, "type", "") == "turn_completed"
                            and self.persist and self.persist_usage
                            and self.session is not None):
                        usage = (event.data or {}).get("usage")
                        if usage:
                            import contextlib as _contextlib

                            with _contextlib.suppress(Exception):
                                self.store.append_usage(
                                    self.session, usage, model=self.model_cfg.id)
                    yield event
        finally:
            self._turns_running -= 1

    @contextlib.contextmanager
    def _cross_surface_turn_lock(self):
        """ONE turn per session ACROSS processes.

        The per-process "session busy" check cannot see the TUI running in
        another terminal while a panel/gateway drives the same session; the
        sidecar `.turn` lock can. Contention raises SessionBusyError instead
        of two surfaces interleaving turns into one transcript. Sessions
        without a file (ephemeral one-shots) cannot conflict and skip the lock.
        """
        if self.session is None or self.session.file is None:
            yield None
            return
        from oaset.utils import file_lock

        lock = file_lock(self.session.file.with_suffix(".turn"), timeout=0.0)
        try:
            lock.__enter__()
        except TimeoutError as exc:
            raise SessionBusyError() from exc
        try:
            yield lock
        finally:
            lock.__exit__(None, None, None)

    async def ask(self, prompt: str, images: list[dict] | None = None) -> str:
        final = {"text": ""}
        async for event in self.chat(prompt, images=images):
            if event.type == "turn_completed":
                final["text"] = str(event.data.get("content", ""))
        return final["text"]

    # -------------------------------------------------------------------- mcp

    async def mcp_start(self) -> dict[str, str | None]:
        if not self.mcp_enabled:
            return {}
        if self._mcp_started or self._mcp_starting:
            return {}
        from oaset.mcp import McpManager

        self._mcp_starting = True
        try:
            if self.mcp is None:
                self.mcp = McpManager(oaset_home(), self.cwd, network_mode=self.cfg.network_mode)
            results = await self.mcp.start_all()
            for adapter in self.mcp.adapters():
                self.registry.add_tool(adapter)
            self._attach_bridges()
            self._mcp_started = True
            return dict(results)
        finally:
            self._mcp_starting = False

    async def mcp_reload(self) -> dict[str, str | None]:
        if self._turns_running:
            raise RuntimeError("mcp reload busy: a turn is running; wait for it to finish")
        await self._mcp_shutdown(remove_tools=True)
        return await self.mcp_start()

    def mcp_status(self) -> str:
        if self.mcp is None:
            from oaset.i18n import t

            return t("mcp_not_started")
        return self.mcp.status_line()

    def _attach_bridges(self) -> None:
        if self.mcp is None:
            return
        from oaset.mcp.bridges import make_roots_handler
        from oaset.mcp.sampling import make_sampling_bridge

        gate = self.registry.gate
        sampling = make_sampling_bridge(self.provider, gate)
        extra_roots = self.registry.ctx.session_state.get("extra_roots")
        roots = make_roots_handler(gate, self.cwd, extra_roots=extra_roots)
        for conn in self.mcp.connections.values():
            conn.sampling_handler = sampling
            conn.roots_handler = roots
            if self.elicit is not None:
                from oaset.mcp.bridges import make_elicitation_bridge

                conn.elicitation_handler = make_elicitation_bridge(gate, self.elicit)

    async def _mcp_shutdown(self, remove_tools: bool = False) -> None:
        if self.mcp is None:
            return
        if remove_tools:
            for adapter in self.mcp.adapters():
                with contextlib.suppress(Exception):
                    self.registry.remove_tool(adapter.name)
        await self.mcp.shutdown()
        self._mcp_started = False

    # ----------------------------------------------------------------- close

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        bg = self.registry.ctx.session_state.get("background_tasks")
        if bg is not None:
            with contextlib.suppress(Exception):
                await bg.stop_all()
        await self._mcp_shutdown()
        seen: set[int] = set()
        for provider in (self.provider, *self.fallbacks):
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def __aenter__(self) -> SessionHost:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
