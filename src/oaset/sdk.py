"""Public SDK — a thin skin over :class:`oaset.host.SessionHost`.

    from oaset.sdk import Oaset

    agent = Oaset(model="glm/glm-5.3-flash", cwd=".")
    async for event in agent.chat("read demo.txt"):
        if event.type == "assistant_delta":
            print(event.data["text"], end="")

    answer = await agent.ask("now summarize")

A future desktop app should prefer ``SessionHost`` directly; this class keeps
the historical Python API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oaset.config import AppConfig, load_config
from oaset.i18n import t

if TYPE_CHECKING:
    from oaset.kernel import Event
    from oaset.mcp import McpManager


class Oaset:
    def __init__(
        self,
        config: AppConfig | None = None,
        cwd: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
        tools: bool = True,
        yolo: bool = False,
        max_iterations: int | None = None,
        auto_compact: bool = True,
        budget_tokens: int = 0,
        mcp: bool = True,
        persist: bool = True,
        hooks: dict[str, str] | None = None,
        elicit: Callable[[str, str], Awaitable[str | None]] | None = None,
    ):
        from oaset.host import SessionHost

        self.cfg = config or load_config()
        self.cwd = Path(cwd or Path.cwd())
        auto = yolo or self.cfg.permission_mode == "auto"
        self.host = SessionHost(
            self.cfg, self.cwd, model_id=model, provider=provider,
            yolo=auto, tools=tools, mcp=mcp, persist=persist,
            hooks=hooks, elicit=elicit,
            max_iterations=max_iterations, auto_compact=auto_compact,
            budget_tokens=budget_tokens,
        )
        self.model_cfg = self.host.model_cfg
        self.provider = self.host.provider
        self.fallbacks = self.host.fallbacks
        self.registry = self.host.registry
        self.agents = self.host.agents
        self.tools_enabled = tools
        self.conversation = self.host.conversation
        self.policy = self.host.policy
        self.max_iterations = self.host.max_iterations
        self.auto_compact = auto_compact
        self.budget_tokens = int(budget_tokens or 0)
        self.mcp_enabled = mcp
        self.mcp: McpManager | None = self.host.mcp
        self._mcp_started = False
        self._turns_running = 0
        self.hooks: dict[str, str] = self.host.hooks_map
        self.elicit = elicit
        self._kernel = self.host.kernel

    async def chat(self, prompt: str, images: list[dict] | None = None) -> AsyncIterator["Event"]:
        self.host.hooks_map = self.hooks
        self.host.elicit = self.elicit
        self.host.tools_enabled = self.tools_enabled
        self._turns_running += 1
        try:
            async for event in self.host.chat(prompt, images=images):
                yield event
        finally:
            self._turns_running -= 1
            self.mcp = self.host.mcp
            self._mcp_started = self.host._mcp_started

    async def ask(self, prompt: str) -> str:
        final = {"text": ""}
        async for event in self.chat(prompt):
            if event.type == "turn_completed":
                final["text"] = str(event.data.get("content", ""))
        return final["text"]

    async def mcp_start(self) -> dict[str, str | None]:
        results = await self.host.mcp_start()
        self.mcp = self.host.mcp
        self._mcp_started = self.host._mcp_started
        return results

    async def mcp_reload(self) -> dict[str, str | None]:
        if self._turns_running:
            raise RuntimeError("mcp reload busy: a turn is running; wait for it to finish")
        results = await self.host.mcp_reload()
        self.mcp = self.host.mcp
        self._mcp_started = self.host._mcp_started
        return results

    def mcp_status(self) -> str:
        if self.mcp is None:
            return t("mcp_not_started")
        return self.mcp.status_line()

    async def aclose(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        await self.host.aclose()
        self.mcp = self.host.mcp

    async def __aenter__(self) -> "Oaset":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def create_session(self) -> "Oaset":
        """Start a fresh JSONL session on this agent and return self.

        Unlike the old no-op, the previous transcript stays on disk; this object
        now points at a new session id and empty conversation.
        """
        previous = self.host.session.meta.session_id
        self.host.new_session()
        self.conversation = self.host.conversation
        self._kernel = self.host.kernel
        assert self.host.session.meta.session_id != previous
        return self
