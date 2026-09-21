"""Agent loop: stream → tool calls → results → repeat, with retries,
iteration budget, mid-stream injection, and clean interruption."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from oaset.agent.messages import Conversation, Message, ToolCallReq
from oaset.agent.verify import (
    implies_system,
    is_mutation,
    is_verify,
    mutation_paths,
    needs_verify,
    nudge_text,
)
from oaset.i18n import t
from oaset.providers.base import ProviderError
from oaset.tools.base import DENIAL_PREFIXES

Emit = Callable[[dict[str, Any]], None]
# on_message may be sync or return an awaitable (the TUI persists on the
# ordered IO thread); loop._append awaits awaitable results.
OnMessage = Callable[[Message], Awaitable[None] | None]

RETRY_BACKOFF = [1, 2, 4]


class AgentLoop:
    """One instance per conversation. run() streams a full user turn.

    Events (dicts) are emitted synchronously to `emit`:
      turn_start, content_delta, reasoning_delta, message_done,
      tool_start, tool_end, tool_denied, turn_done, interrupted,
      notice, iterations_exhausted, error
    """

    def __init__(
        self,
        provider: Any,
        registry: Any | None,
        conversation: Conversation,
        max_iterations: int = 0,
        on_message: OnMessage | None = None,
        injections: list[str] | None = None,
        context_max_tokens: int | None = None,
        hooks: Any | None = None,
        auto_compact: bool = True,
        budget_tokens: int = 0,
        used_before: int = 0,
        on_compact=None,
        fallbacks: list[Any] | None = None,
        max_tool_calls: int = 0,
    ):
        self.provider = provider
        self.fallbacks: list[Any] = list(fallbacks or [])
        self.budget_tokens = int(budget_tokens or 0)
        self.used_before = int(used_before or 0)
        self.on_compact = on_compact  # persist the compaction marker
        self.budget_exhausted = False
        self.auto_compact = auto_compact
        self.registry = registry
        self.conversation = conversation
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.on_message = on_message
        self.injections = injections if injections is not None else []
        self.context_max_tokens = context_max_tokens
        self.hooks = hooks
        self.last_usage: dict[str, int] | None = None
        # evidence receipts live in __init__ too: tools can finish outside
        # run()'s per-turn reset (tests, embedded drivers)
        self._evidence_entries: list[dict] = []
        self._current: Message | None = None
        self._open_tool_ids: set[str] = set()
        self._compacted_once = False
        self._tool_calls_used = 0
        self._last_call_key: tuple[str, str] | None = None
        self._repeat_count = 0
        self._mutated_paths: list[str] = []
        # mutations THIS turn only — the requirements-completeness gate
        # keys on real work happening now, not on edits carried in from an
        # interrupted turn (a pure-planning follow-up may legitimately end
        # with a pending plan)
        self._mutated_this_turn: list[str] = []
        self._verified = False
        self._verify_nudged = False
        self._turn_prompt: str = ""  # feeds the verify-gate stack checklist
        self.on_provider_change: Callable[[Any, list[Any]], None] | None = None

    # ------------------------------------------------------------------ run

    async def run(self, user_text: str, emit: Emit, images: list[dict[str, Any]] | None = None) -> None:
        self._turn_prompt = user_text or ""
        if self.budget_tokens and self.used_before >= self.budget_tokens:
            # the latch alone was per-loop (a fresh loop every turn), so the
            # blocked branch never fired in production; seeding from the
            # conversation makes /goal budget an actual boundary
            self.budget_exhausted = True
        if self.budget_exhausted:
            emit({"type": "budget_exhausted", "used": self.used_before or None,
                  "budget": self.budget_tokens, "blocked": True})
            return
        self._tool_calls_used = 0
        self._last_call_key = None
        self._repeat_count = 0
        # edits left unverified by an INTERRUPTED turn ride in from
        # session_state so this turn's gate names them again
        self._mutated_paths = [str(p) for p in
                               self._pending_state().get(self._PENDING_KEY) or []]
        self._verified = False
        self._verify_nudged = False
        self._mutated_this_turn = []
        self._evidence_entries = []
        content: Any = user_text
        if images:
            content = [{"type": "text", "text": user_text}, *images]
        await self._append(Message(role="user", content=content), emit, "message_done")
        emit({"type": "turn_start", "user_text": user_text})
        await self._fire_hook("on_user_message", user_text=user_text)
        await self._fire_hook("on_turn_start", user_text=user_text)
        try:
            await self._turn(emit)
        except asyncio.CancelledError:
            await self._finalize_interrupt(emit)
            raise

    # -------- verify-gate carry across turns ---------------------------
    # A mid-turn-Esc used to drop _mutated_paths with the loop instance (a
    # fresh loop every turn), so the edited-but-never-verified files were
    # never named again. session_state survives the swap; interrupted
    # turns KEEP their pending list, clean completions clear it.
    _PENDING_KEY = "verify_pending_paths"

    def _pending_state(self) -> dict:
        ctx = getattr(self.registry, "ctx", None)
        return ctx.session_state if ctx is not None else {}

    def _sync_pending(self, clear: bool = False) -> None:
        state = self._pending_state()
        if clear:
            state.pop(self._PENDING_KEY, None)
            return
        merged = [str(p) for p in state.get(self._PENDING_KEY) or []]
        merged += [str(p) for p in self._mutated_paths]
        state[self._PENDING_KEY] = list(dict.fromkeys(merged))

    async def _fire_hook(self, event: str, **payload: Any) -> None:
        if self.hooks is not None:
            try:
                await self.hooks.fire(event, **payload)
            except Exception:
                pass

    # ----------------------------------------------------------------- turn

    async def _turn(self, emit: Emit) -> None:
        iteration = 0
        while True:
            if self.registry is not None and self.registry.ctx is not None:
                bg = self.registry.ctx.session_state.get("background_tasks")
                if bg is not None:
                    for task in bg.drain_completed():
                        self.injections.append(
                            f"[background {task.id} {task.status}: {task.description}]"
                        )
            while self.injections:
                text = self.injections.pop(0)
                await self._append(Message(role="user", content=f"[mid-stream] {text}"), emit, "message_done")
                emit({"type": "notice", "text": t("steer_injected")})
            iteration += 1
            # max_iterations <= 0 (the default) means unlimited: the turn runs
            # until the model stops calling tools. A positive value is an
            # explicit opt-in safety cap.
            if self.max_iterations > 0 and iteration > self.max_iterations:
                self._close_exhausted_turn(emit)
                return
            await self._maybe_auto_compact(emit)
            assistant = await self._stream_once(emit)
            if assistant.tool_calls:
                await self._append(assistant, emit, "message_done")
                await self._execute_tools(assistant.tool_calls, emit)
                if self.max_iterations > 0 and iteration >= self.max_iterations:
                    self._close_exhausted_turn(emit)
                    return
                continue
            # open todos are unmet REQUIREMENTS only once real work happened:
            # a pure-planning turn legitimately ends with a pending plan
            unmet = self._unmet_scenarios() if self._mutated_this_turn else []
            evidence_missing = self._evidence_mode() and bool(self._mutated_paths)                 and not self._evidence_entries
            if (needs_verify(self._mutated_paths, self._verified)
                    or evidence_missing or unmet) and not self._verify_nudged:
                capped = self.max_iterations > 0 and iteration >= self.max_iterations
                if not capped:
                    self._verify_nudged = True
                    await self._append(assistant, emit, "message_done")
                    await self._append(
                        Message(role="user", content=nudge_text(
                            self._mutated_paths,
                            system=implies_system(self._turn_prompt),
                            unmet=unmet)),
                        emit,
                        "message_done",
                    )
                    emit({"type": "notice", "text": t("verify_gate_notice")})
                    continue
            await self._append(assistant, emit, "message_done")
            if self._evidence_entries:
                emit({"type": "evidence", "items": self._evidence_entries})
            emit(
                {
                    "type": "turn_done",
                    "content": assistant.content or "",
                    "usage": self.last_usage,
                }
            )
            # the gate already had its one nudge this turn; a completed
            # answer closes the matter (only interrupts carry it forward)
            self._sync_pending(clear=True)
            await self._fire_hook("on_turn_end", content=str(assistant.content or ""))
            if self.budget_tokens and self.last_usage:
                used = int(self.last_usage.get("total_tokens") or 0) + self.used_before
                if used and used >= self.budget_tokens and not self.budget_exhausted:
                    self.budget_exhausted = True
                    emit({"type": "budget_exhausted",
                          "used": used, "budget": self.budget_tokens})
            return

    # -------------------------------------------------------------- streaming

    async def _stream_once(self, emit: Emit,
                           _carry: tuple[str, str] = ("", "")) -> Message:
        payload = self._wire_payload()
        # 流式断点：中断前已生成的部分内容。REPLACE, never append: the
        # provider restarts the answer on every retry — appending the old
        # prefix to the regenerated one duplicated everything the dead
        # attempt had already shown. The partial survives only when nothing
        # newer completes (terminal failure path below).
        preserved, preserved_reasoning = _carry
        for attempt in range(len(RETRY_BACKOFF) + 1):
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            signature: list[str] = []
            redacted = False
            self._current = None
            # sync the /think level onto the ACTIVE provider IMMEDIATELY
            # before its call: fallback switches swap self.provider, and
            # parallel sub-agents sharing this provider used to overwrite
            # each other's level during our backoff sleep
            _ctx = getattr(self.registry, "ctx", None)
            level = str((_ctx.session_state or {}).get("thinking_level") or "") \
                if _ctx is not None else ""
            if self.provider is not None and hasattr(self.provider, "thinking_level"):
                self.provider.thinking_level = level  # type: ignore[attr-defined]
            try:
                async for event in self.provider.stream(payload, self._tool_schemas()):
                    kind = type(event).__name__
                    if kind == "ContentDelta":
                        content.append(event.text)
                        emit({"type": "content_delta", "text": event.text})
                    elif kind == "ReasoningDelta":
                        reasoning.append(event.text)
                        emit({"type": "reasoning_delta", "text": event.text})
                    elif kind == "ToolCallEmitted":
                        calls[event.index] = {
                            "id": event.id,
                            "name": event.name,
                            "arguments": event.arguments,
                            "signature": getattr(event, "signature", None),
                        }
                    elif kind == "ThinkingSignature":
                        signature.append(event.text)
                    elif kind == "ThinkingRedacted":
                        redacted = True
                    elif kind == "StreamDone":
                        _finish = event.finish_reason
                        if event.usage:
                            self.last_usage = event.usage
                message = Message(
                    role="assistant",
                    # the winning attempt owns the answer; the carried
                    # partial is the floor when this attempt stayed empty
                    content="".join(content) or (preserved or None),
                    tool_calls=[
                        ToolCallReq(
                            id=calls[i]["id"],
                            name=calls[i]["name"],
                            arguments=calls[i]["arguments"],
                            signature=calls[i].get("signature"),
                        )
                        for i in sorted(calls)
                    ]
                    or None,
                    reasoning="".join(reasoning) or (preserved_reasoning or None),
                    thinking_signature="".join(signature) or None,
                    thinking_redacted=redacted,
                )
                self._current = None
                return message
            except asyncio.CancelledError:
                # `preserved` counts too: an Esc during the retry backoff
                # (or before the next attempt's first delta) used to drop
                # what the user already watched stream — the transcript
                # silently lost it while the screen kept showing it
                if content or reasoning or preserved:
                    self._current = Message(
                        role="assistant",
                        content="".join(content) or (preserved or None),
                        reasoning="".join(reasoning)
                        or (preserved_reasoning or None),
                        partial=True,
                    )
                raise
            except ProviderError as exc:
                if content:
                    # this attempt regenerated the SAME answer further than
                    # the last one — it supersedes the carried partial
                    preserved = "".join(content)
                    preserved_reasoning = "".join(reasoning)
                    emit({"type": "notice",
                          "text": t("retry_preserved", n=len(preserved))})
                if not exc.retryable or attempt >= len(RETRY_BACKOFF):
                    if self._switch_fallback(emit):
                        # the fallback restarts the answer too, but the
                        # partial must survive it (it is the floor if the
                        # fallback dies before emitting anything)
                        return await self._stream_once(
                            emit, _carry=(preserved, preserved_reasoning))
                    await self._fire_hook("on_error", category=exc.category, message=exc.message)
                    emit({"type": "error", "error": exc, "hint": exc.hint()})
                    if preserved:
                        return Message(
                            role="assistant",
                            content=preserved or None,
                            reasoning=preserved_reasoning or None,
                            partial=True,
                        )
                    raise
                emit(
                    {
                        "type": "notice",
                        "text": t("provider_retry", category=exc.category,
                                  seconds=RETRY_BACKOFF[attempt],
                                  attempt=attempt + 1, total=len(RETRY_BACKOFF)),
                        # structured so the TUI can badge the status bar even
                        # when the localized text is not the en "retrying"
                        "retry": True,
                        "attempt": attempt + 1,
                        "total": len(RETRY_BACKOFF),
                        "seconds": RETRY_BACKOFF[attempt],
                    }
                )
                try:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                except asyncio.CancelledError:
                    # the backoff dominates the retry window's wall clock —
                    # an Esc here must still land the held partial
                    if preserved:
                        self._current = Message(
                            role="assistant",
                            content=preserved or None,
                            reasoning=preserved_reasoning or None,
                            partial=True,
                        )
                    raise
        raise ProviderError("server", "all retry attempts exhausted", retryable=False)

    def _switch_fallback(self, emit: Emit) -> bool:
        """After retries are exhausted, activate the next
        fallback provider and keep the turn alive."""
        while self.fallbacks:
            candidate = self.fallbacks.pop(0)
            if candidate is self.provider:
                continue
            self.provider = candidate
            name = getattr(candidate, "model_id", "") or type(candidate).__name__
            emit({
                "type": "notice",
                "text": t("fallback_switch", model=name),
                # structured: the TUI updates the status-bar model display to
                # the ACTIVE provider instead of keep showing the dead one
                "fallback": True,
                "model": name,
            })
            if self.on_provider_change is not None:
                self.on_provider_change(self.provider, self.fallbacks)
            return True
        return False

    def _wire_payload(self) -> list[dict[str, Any]]:
        if self.context_max_tokens:
            from oaset.agent.context import trim_for_context

            return trim_for_context(self.conversation, self.context_max_tokens)
        return self.conversation.wire_messages()

    def _tool_schemas(self) -> list[dict[str, Any]] | None:
        if self.registry is None:
            return None
        return self.registry.schemas()

    async def _maybe_auto_compact(self, emit: Emit) -> None:
        """自动压缩：上下文用量超过 85% 且非首轮时自动摘要旧行史。"""
        if not self.auto_compact or self.provider is None or not self.context_max_tokens:
            return
        from oaset.agent.context import (
            SUMMARY_KEEP_RECENT,
            compact_history,
            conversation_tokens,
            turn_boundary,
        )

        used = conversation_tokens(self.conversation)
        if used <= int(self.context_max_tokens * 0.85):
            return
        if getattr(self, "_compacted_once", False):
            return  # one auto-compaction per run() is enough
        self._compacted_once = True
        # the boundary is computed before compaction, on the same list
        # compact_history sees, so the persisted marker records exactly which
        # messages the summary replaced (a reload replays that count)
        dropped = turn_boundary(self.conversation.messages, SUMMARY_KEEP_RECENT)
        emit({"type": "auto_compact", "before": used})
        try:
            summary = await compact_history(self.conversation, self.provider)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit({"type": "notice", "text": t("auto_compact_failed", err=exc)})
            return
        emit({"type": "auto_compact", "after": conversation_tokens(self.conversation),
              "summary": summary[:200]})
        if self.on_compact is not None and summary:
            # without this the summary lived only in RAM: a reload brought the
            # pre-compaction history back, contradicting "the JSONL is the
            # single source of truth / snapshot == restored session". An empty
            # summary is NOT persisted — it replaced nothing, and the marker
            # would have rewritten history on the next load.
            try:
                outcome = self.on_compact(str(summary), dropped)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                pass

    # ----------------------------------------------------------------- tools

    async def _execute_tools(self, calls: list[ToolCallReq], emit: Emit) -> None:
        """Run a batch of tool calls.

        Starts and finishes stay in call order (wire-valid transcript). The
        actual dispatch of independent read tools runs in parallel so a
        search+fetch turn is not serialized behind our own event loop.
        """
        if len(calls) <= 1:
            for call in calls:
                await self._execute_tool(call, emit)
            return
        started: list[tuple[ToolCallReq, str | None]] = []
        for call in calls:
            await self._begin_tool(call, emit)
            started.append((call, self._loop_guard(call)))
        parallel = all(
            guard is None and _tool_is_parallel_safe(self.registry, call.name)
            for call, guard in started
        )
        def _crashed(exc: BaseException) -> tuple[str, bool, None, None]:
            return (f"[tool crashed] {type(exc).__name__}: {exc}", True, None, None)

        if parallel:
            raw = await asyncio.gather(
                *(self._dispatch_tool(call) for call, _guard in started),
                return_exceptions=True,
            )
            outcomes = [_crashed(o) if isinstance(o, BaseException) else o for o in raw]
        else:
            outcomes = []
            for call, guard in started:
                if guard is not None:
                    outcomes.append((guard, True, None))
                    continue
                try:
                    outcomes.append(await self._dispatch_tool(call))
                except Exception as exc:  # every call still gets a tool reply
                    outcomes.append(_crashed(exc))
        for (call, guard), outcome in zip(started, outcomes, strict=True):
            if guard is not None:
                result, is_error, image, exit_code = guard, True, None, None
            else:
                result, is_error, image, exit_code = outcome
            await self._finish_tool(call, result, is_error, emit, image=image,
                                    exit_code=exit_code)

    async def _execute_tool(self, call: ToolCallReq, emit: Emit) -> None:
        await self._begin_tool(call, emit)
        guard = self._loop_guard(call)
        if guard is not None:
            result, is_error, image, exit_code = guard, True, None, None
        else:
            result, is_error, image, exit_code = await self._dispatch_tool(call)
        await self._finish_tool(call, result, is_error, emit, image=image,
                                exit_code=exit_code)

    async def _begin_tool(self, call: ToolCallReq, emit: Emit) -> None:
        emit(
            {
                "type": "tool_start",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )
        await self._fire_hook("on_tool_start", tool_name=call.name, tool_args=call.arguments)
        self._open_tool_ids.add(call.id)

    async def _dispatch_tool(self, call: ToolCallReq) -> tuple[str, bool, bytes | None, int | None]:
        if self.registry is None:
            return "[no tools available]", True, None, None
        result_obj = await self.registry.dispatch(call.name, call.arguments)
        return (result_obj.output, result_obj.is_error,
                getattr(result_obj, "image", None),
                getattr(result_obj, "exit_code", None))

    def _evidence_mode(self) -> bool:
        if self.registry is None or self.registry.ctx is None:
            return False
        return bool(self.registry.ctx.session_state.get("evidence"))

    def _unmet_scenarios(self) -> list[str]:
        """Open todo items are unmet requirements (the completeness loop).

        /evidence raises the bar: "done" must account for every scenario the
        plan listed, not just the happy path the model chose to mention.
        """
        if self.registry is None or self.registry.ctx is None:
            return []
        todos = self.registry.ctx.session_state.get("todos") or []
        return [str(t.get("content", "")) for t in todos
                if isinstance(t, dict) and t.get("status") != "completed"]

    def _close_exhausted_turn(self, emit: Emit) -> None:
        """Emit the cap notice AND close the turn.

        Returning after only the notice left headless surfaces waiting on
        turn_completed to return "" silently, and the door's usage/evidence
        persistence (which keys on turn_completed) never ran for the turn.
        """
        emit({"type": "iterations_exhausted", "max": self.max_iterations})
        # the cap closed the turn (the user was told); like a clean
        # completion it does not carry pending mutations forward
        self._sync_pending(clear=True)
        if self._evidence_entries:
            emit({"type": "evidence", "items": self._evidence_entries})
        emit({"type": "turn_done", "content": "", "usage": self.last_usage})

    def _collect_evidence(self, call: ToolCallReq, result: str, is_error: bool,
                          exit_code: int | None, artifact: str | None = None,
                          kind: str | None = None) -> None:
        """Record verification receipts while /evidence is on.

        Every successful verification call becomes an entry — the tool name,
        a clamped summary, `ok` from the exit code and, when the output was
        spilled or a screenshot captured, the on-disk artifact the receipt
        points at.
        """
        if not self._evidence_mode() or is_error:
            return
        if not is_verify(call.name):
            return
        failed = exit_code not in (None, 0)
        entry = {"kind": kind or ("diagnostics" if call.name != "run_shell" else "run"),
                 "tool": call.name,
                 "summary": " ".join(str(result).split())[:200],
                 "ok": not failed}
        if artifact:
            entry["artifact"] = artifact
        self._evidence_entries.append(entry)

    async def _finish_tool(self, call: ToolCallReq, result: str, is_error: bool,
                           emit: Emit, image: bytes | None = None,
                           exit_code: int | None = None) -> None:
        if result.startswith(DENIAL_PREFIXES):
            emit({"type": "tool_denied", "id": call.id, "name": call.name})
        out_dir = None
        if self.registry is not None and self.registry.ctx is not None:
            out_dir = self.registry.ctx.session_state.get("tool_output_dir")
        if out_dir and len(result) > 4000:
            with contextlib.suppress(Exception):
                side = await asyncio.to_thread(_side_write, out_dir, call.name, result)
                result += t("spill_saved_note", path=side)
        emit({"type": "tool_end", "id": call.id, "name": call.name, "result": result, "is_error": is_error})
        side = None
        kind = None
        if self._evidence_mode() and out_dir:
            # under /evidence, a screenshot IS a receipt: persist the PNG so
            # the audit chain can show what the agent actually saw
            if image:
                import time as _time

                with contextlib.suppress(Exception):
                    shot_dir = Path(out_dir) / "evidence"
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    shot = shot_dir / f"{call.name}-{_time.strftime('%H%M%S')}-{call.id[-6:]}.png"
                    await asyncio.to_thread(shot.write_bytes, image)
                    side = str(shot)
                    kind = "screenshot"
        self._collect_evidence(call, result, is_error, exit_code, artifact=side, kind=kind)
        if is_mutation(call.name, call.arguments, is_error):
            _paths = mutation_paths(call.name, call.arguments)
            self._mutated_paths.extend(_paths)
            self._mutated_this_turn.extend(_paths)
            # a NEW mutation invalidates proof from an earlier verification:
            # _verified used to stick, so "edit → test → edit → done" passed
            # with the second edit never verified
            self._verified = False
            self._sync_pending()
        elif (is_verify(call.name) and not is_error
              and not str(result).startswith(DENIAL_PREFIXES)
              and exit_code in (None, 0)):
            # a non-zero exit is not verification: run_shell returns failing
            # builds as ordinary output, so only the exit code tells them apart
            self._verified = True
            self._sync_pending(clear=True)
        await self._fire_hook("on_tool_end", tool_name=call.name, is_error=str(is_error))
        content: str | list
        if image:
            import base64
            payload = base64.b64encode(image).decode("ascii")
            content = [
                {"type": "text", "text": result or ""},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{payload}"}},
            ]
        else:
            content = result
        await self._append(
            Message(
                role="tool",
                tool_call_id=call.id,
                content=content,
                error=is_error,
            ),
            emit,
            "message_done",
        )
        # The id is dropped only AFTER the tool message is in the transcript:
        # a cancellation in the window between discard and append used to
        # leave the assistant's tool_calls with no matching tool message —
        # wire-invalid on resume (P4-4).
        self._open_tool_ids.discard(call.id)

    def _loop_guard(self, call: ToolCallReq) -> str | None:
        """Runaway guards for one turn (P0-3 in docs/PLAN.zh-CN.md): a
        tool-call budget plus a circuit breaker for identical consecutive
        calls. Both return a wire-valid tool error, never an exception, so
        the turn stays resumable and the model can wind down."""
        self._tool_calls_used += 1
        if self.max_tool_calls > 0 and self._tool_calls_used > self.max_tool_calls:
            return (f"[tool call budget exhausted for this turn "
                    f"(max {self.max_tool_calls}); stop calling tools and "
                    "answer from what you have]")
        key = (call.name, call.arguments)
        if key == self._last_call_key:
            self._repeat_count += 1
        else:
            self._last_call_key = key
            self._repeat_count = 1
        if self._repeat_count >= 3:
            return ("[the same tool call with identical arguments has now been "
                    "made three times in a row; change the approach or answer "
                    "from what you already have]")
        return None

    # ------------------------------------------------------------- lifecycle

    async def _append(self, message: Message, emit: Emit, done_event: str | None = None) -> None:
        self.conversation.append(message)
        if self.on_message:
            try:
                outcome = self.on_message(message)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                # persistence failing used to be SILENT: the transcript
                # quietly lost lines while everything looked healthy. An
                # emit is not always available here, but when it is, the
                # user must hear about the loss.
                if emit is not None:
                    with contextlib.suppress(Exception):
                        emit({"type": "notice",
                              "text": t("persist_failed",
                                        error=f"{type(exc).__name__}: {exc}")})
        if done_event:
            emit({"type": done_event, "message": message})

    async def _finalize_interrupt(self, emit: Emit) -> None:
        """Leave the conversation wire-valid after cancellation.

        - a partially streamed assistant message is appended (partial=True)
        - any tool call awaiting its result gets a synthetic tool message
        """
        if self._current is not None:
            await self._append(self._current, emit, "message_done")
            self._current = None
        for call_id in sorted(self._open_tool_ids):
            await self._append(
                Message(role="tool", tool_call_id=call_id, content="[interrupted by user]"),
                emit,
                "message_done",
            )
        self._open_tool_ids.clear()
        emit({"type": "interrupted"})


def _side_write(out_dir: str, name: str, result: str) -> str:
    """Persist an oversized tool result next to the transcript. Runs on a
    worker thread (``asyncio.to_thread`` in ``_execute_tool``) — disk latency
    must never stall the turn loop (S2 in docs/PLAN.zh-CN.md)."""
    from oaset.utils import write_side_output

    return write_side_output(out_dir, name, result)


_PARALLEL_SAFE = frozenset({
    "read_file", "list_dir", "glob", "grep", "web_search", "web_fetch",
    "lsp_diagnostics", "repo_map", "git_status", "search_history",
})


def _tool_is_parallel_safe(registry: Any, name: str) -> bool:
    if name in _PARALLEL_SAFE:
        return True
    if registry is None:
        return False
    tool = getattr(registry, "tools", {}).get(name)
    return bool(tool is not None and getattr(tool, "permission", "") == "read")


def arguments_summary(arguments: str, limit: int = 120) -> str:
    """Human one-liner of tool arguments for tool cards.

    Nested JSON (todo lists, payloads) is collapsed to a count, never dumped
    inline — a full array in the title wraps the transcript.
    """
    from oaset.utils import one_line

    try:
        data = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return one_line(arguments, limit)
    if isinstance(data, dict):
        parts: list[str] = []
        for key, value in data.items():
            if isinstance(value, str):
                text = one_line(value, 40)
            elif isinstance(value, list):
                text = f"[{len(value)}]"
            elif isinstance(value, dict):
                text = "{…}"
            else:
                text = json.dumps(value, ensure_ascii=False)
            parts.append(f"{key}={text}")
        return one_line(", ".join(parts), limit)
    if isinstance(data, list):
        return one_line(f"[{len(data)}]", limit)
    return one_line(str(data), limit)
