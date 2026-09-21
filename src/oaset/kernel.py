"""Agent kernel — the ONLY public orchestration boundary (Phase B).

Every runtime surface (TUI, SDK, headless CLI, batch, gateway) consumes
canonical :class:`oaset.events.Event` envelopes through this facade; none of
them may construct :class:`oaset.agent.AgentLoop` directly:

    kernel = AgentKernel(provider, registry, conversation, fallbacks=[...])
    async for event in kernel.chat("prompt"):
        ...  # event.type == "assistant_delta", event.sequence == 3, ...

The kernel owns: envelope sequencing, session identity, the one-turn-per-
session guard, the event bus, and loop construction. ``AgentLoop`` stays the
internal execution engine; its raw dict dialect is normalized here and
nowhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from oaset.agent import AgentLoop, Conversation
from oaset.errors import SessionBusyError
from oaset.events import Event, normalize_loop_event
from oaset.network import NetworkPolicy

__all__ = ["AgentKernel", "EventBus", "Event", "stream_events"]


class EventBus:
    """Minimal typed pub/sub. Subscribers on '*' receive every event.

    Handler exceptions are NEVER silently swallowed (they used to hide bugs):
    they are recorded and re-published as ``diagnostic_created`` events, with
    recursion guarded (a failing diagnostic handler is only recorded)."""

    def __init__(self, max_recorded: int = 20) -> None:
        self._subs: dict[str, list[Callable[[Event], Any]]] = defaultdict(list)
        self._dispatching = False
        self._diag_sequence = 0
        self.handler_errors: deque[dict[str, Any]] = deque(maxlen=max_recorded)

    def on(self, event_type: str, handler: Callable[[Event], Any]) -> Callable[[], None]:
        self._subs[event_type].append(handler)
        return lambda: self._subs[event_type].remove(handler)

    def emit(self, event: Event) -> None:
        self._dispatching = True
        try:
            for handler in [*self._subs.get(event.type, []), *self._subs.get("*", [])]:
                try:
                    handler(event)
                except Exception as exc:
                    self._record(event, handler, exc)
        finally:
            self._dispatching = False

    def _record(self, event: Event, handler: Callable[[Any], Any], exc: Exception) -> None:
        record = {
            "event_type": event.type,
            "handler": getattr(handler, "__qualname__", repr(handler)),
            "error": f"{type(exc).__name__}: {exc}",
        }
        self.handler_errors.append(record)
        # Re-publish as a diagnostic. This runs while emit()'s dispatch flag is
        # still set, so the diagnostic goes out through a guarded mini-dispatch:
        # a failing diagnostic handler is recorded in-place, never re-emitted.
        diagnostic = Event(
            schema_version=1,
            event_id=f"diag-{id(event)}-{len(self.handler_errors)}",
            session_id=event.session_id,
            sequence=self._diag_sequence,
            timestamp=event.timestamp,
            type="diagnostic_created",
            data={"kind": "handler_error", **record},
        )
        self._diag_sequence += 1
        for handler in [*self._subs.get("diagnostic_created", []),
                        *self._subs.get("*", [])]:
            try:
                handler(diagnostic)
            except Exception as diag_exc:
                self.handler_errors.append({
                    "event_type": "diagnostic_created",
                    "handler": getattr(handler, "__qualname__", repr(handler)),
                    "error": f"{type(diag_exc).__name__}: {diag_exc}",
                })


async def stream_events(run_fn) -> AsyncIterator[Any]:
    """Drive ``run_fn(emit)`` in a producer task, yielding emitted events live.

    Exceptions raised by the producer are re-raised on the consumer side.
    If the consumer abandons the generator early, the producer is cancelled
    (AgentLoop's cancellation path preserves partial output and clean wire).
    """
    queue: asyncio.Queue = asyncio.Queue()
    done: dict[str, Any] = {}

    async def produce() -> None:
        try:
            await run_fn(lambda event: queue.put_nowait(event))
        except BaseException as exc:  # re-raised on the consumer side
            queue.put_nowait(exc)
        finally:
            queue.put_nowait(done)

    task = asyncio.ensure_future(produce())
    try:
        while True:
            item = await queue.get()
            if item is done:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    except (GeneratorExit, asyncio.CancelledError):
        task.cancel()
        raise
    finally:
        if task.done() is False:
            task.cancel()
            with contextlib.suppress(Exception):
                await task  # the loop's cancellation path finalizes the wire


class AgentKernel:
    """One kernel per session; each chat() runs an isolated agent loop.

    Guarantees (Phase B):
    - every yielded event is a canonical :class:`Event` with a monotonically
      increasing ``sequence`` and this kernel's ``session_id``;
    - one in-flight turn per session: a second concurrent chat() raises
      :class:`SessionBusyError` (``code="session_busy"``) without touching
      the conversation;
    - fallback providers are passed through to the loop, so SDK/CLI/TUI
      share the TUI's fallback semantics;
    - subscriber crashes surface as ``diagnostic_created`` events, never as
      silent swallowed exceptions.
    """

    def __init__(
        self,
        provider: Any,
        registry: Any,
        conversation: Conversation,
        policy: NetworkPolicy | None = None,
        max_iterations: int = 0,
        context_max_tokens: int | None = None,
        auto_compact: bool = True,
        hooks: Any | None = None,
        on_message: Callable[[Any], Any] | None = None,  # sync or async callback
        on_compact: Callable[[str, int], Any] | None = None,  # summary, dropped
        budget_tokens: int = 0,
        fallbacks: list[Any] | None = None,
        session_id: str | None = None,
        max_tool_calls: int = 0,
    ):
        self.provider = provider
        self.fallbacks: list[Any] = list(fallbacks or [])
        self.registry = registry
        self.conversation = conversation
        self.policy = policy
        self.max_iterations = max_iterations
        self.budget_tokens = int(budget_tokens or 0)
        self.context_max_tokens = context_max_tokens
        self.auto_compact = auto_compact
        self.hooks = hooks
        self.on_message = on_message
        self.on_compact = on_compact
        self.max_tool_calls = max_tool_calls
        self.session_id = session_id or f"session-{id(self):x}"
        self.injections: list[str] = []
        self.bus = EventBus()
        self._sequence = 0
        self._turn_active = False

    def subscribe(self, event_type: str, handler: Callable[[Event], Any]) -> Callable[[], None]:
        return self.bus.on(event_type, handler)

    def inject(self, text: str) -> None:
        self.injections.append(text)

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    def _next_event(self, raw: dict[str, Any]) -> Event:
        event = normalize_loop_event(raw, session_id=self.session_id, sequence=self._sequence)
        self._sequence += 1
        return event

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Usage of the most recent turn (None before the first turn)."""
        loop = self.__dict__.get("_last_loop")
        return getattr(loop, "last_usage", None) if loop is not None else None

    def _build_loop(self) -> AgentLoop:
        loop = AgentLoop(
            # budget is a cross-turn boundary: seed from what the
            # conversation already holds (a per-turn latch alone never
            # blocked anything in production)
            used_before=self.conversation.token_estimate(),
            provider=self.provider,
            registry=self.registry,
            conversation=self.conversation,
            max_iterations=self.max_iterations,
            on_message=self.on_message,
            on_compact=self.on_compact,
            budget_tokens=self.budget_tokens,
            injections=self.injections,
            context_max_tokens=self.context_max_tokens,
            hooks=self.hooks,
            auto_compact=self.auto_compact,
            fallbacks=self.fallbacks,
            max_tool_calls=self.max_tool_calls,
        )
        loop.on_provider_change = self._adopt_provider
        self._last_loop = loop
        return loop

    def _adopt_provider(self, provider: Any, fallbacks: list[Any]) -> None:
        """Keep the next turn on the provider that actually served this one."""
        self.provider = provider
        self.fallbacks = list(fallbacks)

    async def chat(
        self, prompt: str, images: list[dict] | None = None
    ) -> AsyncIterator[Event]:
        """Run one user turn, yielding canonical Events the moment they are
        emitted.

        The loop runs in a producer task and events cross a queue as they
        happen. Abandoning the generator cancels the run (the loop's own
        cancellation handling preserves partial output). A second concurrent
        chat() on this kernel raises SessionBusyError.
        """
        if self._turn_active:
            raise SessionBusyError()
        self._turn_active = True
        try:
            loop = self._build_loop()

            async def produce(emit):
                await loop.run(
                    prompt,
                    lambda event: emit(self._next_event(event)),
                    images=images,
                )

            async for event in stream_events(produce):
                self.bus.emit(event)
                yield event
        finally:
            self._turn_active = False

    async def ask(self, prompt: str) -> str:
        """Convenience: full turn, returns the final assistant text."""
        final = {"text": ""}
        async for event in self.chat(prompt):
            if event.type == "turn_completed":
                final["text"] = str(event.data.get("content", ""))
        return final["text"]
