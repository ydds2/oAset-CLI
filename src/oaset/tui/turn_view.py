"""Turn render dispatch (P4-1 completion): agent-loop events -> chat widgets.

Moved verbatim from `tui/app.py` so the app module keeps only lifecycle,
bindings and sinks. `handle_turn_event` is the whole dispatch — the app's
`_handle_event` is a one-line delegate with the same signature, which is the
seam the tests pin. A future DisplayBlock layer replaces the widget calls
inside this module without touching app.py again.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from oaset.events import Event
from oaset.i18n import t
from oaset.utils import run_io

if TYPE_CHECKING:
    from oaset.tui.widgets.tool_card import ToolCard


@dataclass
class ToolRun:
    """One tool call. The card mounts into the chat flow immediately so
    finished modules scroll up with the transcript; only the working strip
    stays pinned while a turn is running."""

    id: str
    name: str
    summary: str = ""
    raw_args: str = ""
    started: float = field(default_factory=time.monotonic)
    is_error: bool = False
    result: str = ""
    elapsed: float | None = None
    anchor: Any | None = None
    card: Any | None = None


def stream_delta(app, turn, kind: str, text: str) -> None:
    """Route a streamed delta into the ACTIVE segment (P2 ordering).

    Tool calls seal segments, so post-tool text opens a new block BELOW
    the cards; new segments join the flusher rotation like the first."""
    seg = app.chat.ensure_assistant()
    if seg is not turn and seg not in app.flusher_turns:
        app.flusher_turns.append(seg)
    if kind == "content":
        seg.push_content(text)
    else:
        seg.push_reasoning(text)


def handle_turn_event(app, event: Any, turn,
                      cards: "dict[str, ToolCard]",
                      turn_started: float = 0.0) -> None:
    # Canonical Event is the only public shape. Flat dicts stay for tests.
    if isinstance(event, Event):
        kind: Any = event.type
        data: dict = dict(event.data)
    elif isinstance(event, dict):
        kind = event.get("type")
        data = event
    else:
        kind = None
        data = {}
    if kind is None:
        app.chat.add_notice(t("event_unknown_skipped"), "warn")
        return
    if kind != "notice":
        # any forward progress retires a stale retry badge
        app.status_bar.set_note("")
    if kind == "turn_started":
        # A legal kernel lifecycle event: it opens the turn window used by
        # per-turn meta (model · elapsed · tokens). No user-visible output.
        if not turn_started:
            turn_started = time.monotonic()
        budget = app.registry.ctx.session_state.get("search_budget")
        if budget is not None and hasattr(budget, "reset"):
            budget.reset()
        return
    if kind == "assistant_message":
        # A legal per-message boundary: flush the streamed buffer so the
        # next message starts clean, without ending the whole turn.
        (app.chat.active_turn or turn).flush()
        return
    if kind == "message_appended":
        # Non-assistant messages (user/tool). A TOOL message is the P2
        # ordering boundary: the segment above the tool cards must seal so
        # post-tool narration starts a new block below them. User messages
        # need nothing — their bubble is rendered on arrival.
        message = data.get("message")
        if getattr(message, "role", "") == "tool":
            app.chat.seal_assistant()
        return
    if kind == "turn_cancelled":
        # A legal terminal event for a cancelled turn. The cancellation
        # path (CancelledError handler) owns the user-facing "已被用户中断"
        # notice, so consuming this silently avoids a duplicate — and
        # avoids the false "protocol drift" warning reported on the real
        # machine after Ctrl+X/Escape cancels a turn.
        with contextlib.suppress(Exception):
            (app.chat.active_turn or turn).flush()
        return
    if kind == "assistant_delta":
        stream_delta(app, turn, "content", data.get("text", ""))
    elif kind == "reasoning_delta":
        stream_delta(app, turn, "reasoning", data.get("text", ""))
    elif kind == "tool_call_started":
        app.chat.seal_assistant()
        from oaset.agent.loop import arguments_summary

        summary = arguments_summary(data.get("arguments", ""))
        tool_id = data.get("id") or f"anon-{uuid.uuid4().hex[:12]}"
        existing = getattr(app, "tool_runs", {}).get(tool_id)
        if existing is not None:
            # A duplicate started id used to overwrite the dict entry and
            # orphan the old card with a spinner that never stops.
            existing.elapsed = time.monotonic() - existing.started
            existing.is_error = True
            card = getattr(existing, "card", None)
            if card is not None:
                with contextlib.suppress(Exception):
                    card.finish(t("tool_duplicate_id"), is_error=True)
        run = app.activity_start(tool_id, data.get("name", "?"), summary,
                                 raw_args=data.get("arguments", ""))
        cards[run.id] = run
        app.active_card = run
    elif kind == "tool_output_delta":
        chunk = data.get("text") or data.get("chunk") or data.get("delta") or ""
        if chunk:
            app.activity_stream(str(chunk))
    elif kind == "tool_denied":
        run = cards.get(str(data.get("id") or ""))
        app.activity_finish(data.get("id") or "", t("tool_denied_msg"), True, run)
    elif kind == "tool_completed":
        run = cards.get(str(data.get("id") or ""))
        result = data.get("result", "")
        app.activity_finish(data.get("id") or "", result,
                            data.get("is_error", False), run)
        if data.get("name") == "todo_write":
            app.registry.ctx.session_state.pop("plan_completed_notified", None)
            app.autoshow_plan()
        if data.get("name") == "skill_create" and not data.get("is_error"):
            refresh = getattr(app, "_refresh_skills_prompt", None)
            if callable(refresh):
                refresh()
        app._refresh_sidebar()
    elif kind == "turn_completed":
        usage = data.get("usage")
        done_card_turn = (app.chat.active_turn or turn)
        done_card_turn.finish(
            usage=usage, elapsed=time.monotonic() - turn_started,
            model=app.model_cfg.id, tool_calls=len(cards))
        # Cost transparency: shown only when the user priced the model in
        # config.toml ([models] price_in/price_out) — never a guess from us.
        priced = app.cfg.models.get(app.model_cfg.id, app.model_cfg)
        cost = priced.cost((usage or {}).get("prompt_tokens"),
                           (usage or {}).get("completion_tokens"))
        if cost is not None:
            shown = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
            done_card_turn.add_meta(" · ≈" + shown)
        done, total = app.plan_progress()
        if total:
            done_card_turn.add_meta(" · " + t("plan_meta", done=done, total=total))
            state = app.registry.ctx.session_state
            if done == total and state.get("plan_seen"):
                if not state.get("plan_completed_notified"):
                    state["plan_completed_notified"] = True
                    app.chat.add_notice(t("plan_complete", n=total), "info")
            state["plan_seen"] = True
        app.chat.seal_assistant()
        app.title = "oAset CLI"
        with contextlib.suppress(Exception):
            app._notify_done(t("notify_turn_done", model=app.model_cfg.id))
        if usage:
            app.status_bar.set_tokens(app.conversation.token_estimate())
            # `run_io` is a plain function returning the executor Future. Called
            # without being awaited it still enqueues the write on the ordered IO
            # thread, but nothing observes it: an exception from the append is
            # reported nowhere (a plain Future, unlike a Task, emits no
            # "never retrieved" warning), and the "usage is on disk" ordering
            # that every other write site relies on is not established. The
            # dispatcher is sync, so park the awaitable for the turn to await.
            app._pending_io = run_io(app.store.append_usage, app.session, usage,
                                     app.model_cfg.id)
    elif kind == "budget_exhausted":
        used = data.get("used")
        budget = data.get("budget")
        msg = t("budget_exhausted_msg")
        if used:
            msg += t("budget_exhausted_detail", used=used, budget=budget)
        msg += t("budget_exhausted_hint")
        app.notify_error(msg, code="agent.budget_exhausted", source="agent")
    elif kind == "auto_compacted":
        before = data.get("before")
        after = data.get("after")
        app.chat.add_notice(
            t("auto_compact_notice", before=before, after=after), "info"
        )
    elif kind == "notice":
        text = data.get("text") or ""
        retry = bool(data.get("retry")) or "retrying" in text
        app.chat.add_notice(text, "warn" if retry else "info")
        if retry:
            app.status_bar.set_note(
                f"↻ {data.get('attempt', '?')}/{data.get('total', '?')}"
                f" · {data.get('seconds', 0)}s"
            )
        if data.get("fallback"):
            model = str(data.get("model") or "")
            prov = model.split("/", 1)[0] if "/" in model else model
            app.status_bar.set_model(model, prov)
        if retry or data.get("fallback"):
            # the provider restarts the answer: whatever streamed so far is
            # a dead prefix — hide it so the live view matches the replace
            # semantics the transcript will persist
            seg = getattr(app.chat, "_active_turn", None)
            if seg is not None:
                with contextlib.suppress(Exception):
                    seg.reset_stream()
    elif kind == "iterations_exhausted":
        app.chat.add_notice(
            t("iterations_exhausted_msg", max=data.get("max", "?")),
            "warn",
        )
    elif kind == "error":
        error = data.get("error")
        hint = data.get("hint") or ""
        text = (getattr(error, "message", str(error)) if error is not None
                else t("unknown_error"))
        app.notify_error(text, code=str(data.get("code") or "agent.provider_error"),
                         hint=hint, source="provider")
        with contextlib.suppress(Exception):
            app._notify_done(t("notify_turn_failed", model=app.model_cfg.id))
    elif kind in ("approval_requested", "approval_resolved", "checkpoint_saved",
                  "fallback_started", "fallback_completed", "phase_changed",
                  "session_started", "session_closed", "context_planned",
                  "context_compacted", "plugin_started", "plugin_failed",
                  "plugin_stopped", "diagnostic_created"):
        if kind in ("fallback_started", "fallback_completed"):
            model = str(data.get("model") or "")
            if model:
                prov = model.split("/", 1)[0] if "/" in model else model
                app.status_bar.set_model(model, prov)
    else:
        app.chat.add_notice(t("event_type_unhandled", kind=str(kind)), "warn")


def flush_tool_activity(app) -> None:
    """Turn ended: cards already sit in chronological order. Clear the
    live-run bookkeeping so the next turn starts clean.

    A run that never got its completion event (Esc interrupted the tool, the
    stream died) is finished here as interrupted. Left alone it kept the
    `tool-running` class forever: a spinner that never stops, a card excluded
    from the `⋯ folded` group, and a permanently "unfinished" entry in
    `/copy tools`.
    """
    from oaset.i18n import t

    for run in list(getattr(app, "tool_runs", {}).values()):
        if run.elapsed is not None:
            continue
        card = getattr(run, "card", None)
        run.elapsed = time.monotonic() - run.started
        run.is_error = True
        if card is not None:
            with contextlib.suppress(Exception):
                card.finish(t("tool_interrupted"), is_error=True)
    app._pending_cards.clear()
    app._pending_runs.clear()
    app.active_card = None


def mount_grouped_tools(app) -> bool:
    """Kept as a no-op: live turns mount one line per call immediately
    Grouping after the fact would yank cards the user just saw."""
    return False
