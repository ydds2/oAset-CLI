"""Session & history commands (P4-1 split from app.py).

Methods move verbatim; they keep working through `self` (OasetApp
inherits this mixin). Imports here cover what the moved bodies use;
anything else is imported locally inside a method, as before.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from rich.markup import escape
from textual import work

from oaset.history import search_history
from oaset.i18n import t
from oaset.utils import IoQueueBlockedError, io_barrier, one_line, run_io


class SessionAdminMixin:
    """Session & history commands (P4-1 split from app.py)."""

    async def _io_barrier_safe(self) -> bool:
        """Bounded barrier: a wedged IO queue (AV lock, sync client, dead
        network drive) fails VISIBLY instead of hanging the command forever."""
        try:
            await io_barrier()
            return True
        except IoQueueBlockedError as exc:
            self.notify_error(exc, code="io.queue_blocked", source="session",
                              hint=t("io_blocked_hint"))
            return False

    def _abandon_queue(self) -> None:
        """Drop queued messages before a session swap.

        They were typed into the OLD conversation; auto-firing them into the
        fresh one (the old _finish_turn path) sends text the user never aimed
        at the new session.
        """
        if self.input_queue:
            self.chat.add_notice(t("queue_dropped", n=len(self.input_queue)), "info")
            self.input_queue.clear()
        self.status_bar.set_queue_count(0)

    def _reset_session_scoped_ui(self) -> None:
        """Drop UI state that belongs to the session being left.

        Everything here outlived a session switch before: the pinned error kept
        the status bar red and made `/errors` open a failure from a session that
        was no longer on screen, and a pending `/image` attachment was silently
        sent with the first message of the *next* session — after `/new` or
        `/clear`, the user reasonably believed the attachment was gone.
        """
        from oaset.tui.notice import clear_pinned_error

        clear_pinned_error(self)
        # /retry after /new, /clear, /fork or a session switch used to resend
        # the PREVIOUS session's prompt (cross-project leakage) — the retry
        # buffer belongs to the session being left
        self._last_user_prompt = ""
        # tokens/sidebar/goal outlived the swap too: a 100k-token session
        # showed the old small count, the plan sidebar kept the previous
        # session's todos, and a live goal badge contradicted the fresh
        # system prompt (until the next skills refresh injected the stale
        # goal into it).
        self.status_bar.set_tokens(self.conversation.token_estimate())
        self.registry.ctx.session_state.pop("goal", None)
        with contextlib.suppress(Exception):
            self.status_bar.set_goal("")
        with contextlib.suppress(Exception):
            self._refresh_sidebar()
        dropped = len(self._pending_images)
        if dropped:
            self._pending_images.clear()
            self.chat.add_notice(t("image_dropped_on_switch", n=dropped), "info")

    async def _swap_out_turn(self) -> bool:
        """Stop the in-flight turn before swapping session records.

        Returns False when the worker did not stop in time: the swap must be
        aborted or the still-streaming old turn writes into the NEW record.
        """
        # Drain FIRST: dropping the queue up front meant a timed-out switch
        # ("session switch aborted") had already destroyed the user's queued
        # messages and still left the turn running.
        if not await self.drain_turn():
            self.notify_error(t("session_switch_busy"), code="turn.cancel_timeout",
                              source="turn")
            return False
        self._abandon_queue()
        return True

    async def cmd_new(self, args: str) -> None:
        # Cancel and reap the in-flight turn BEFORE swapping records: the old
        # worker kept streaming (and running tools!) against the retired
        # conversation while the UI already showed the new session (S5).
        if not await self._swap_out_turn():
            return
        self._turn.bump()  # an in-flight turn keeps writing to the OLD record
        self.host.new_session()
        self.session = self.host.session
        self.conversation = self.host.conversation
        self._kernel = self.host.kernel
        self.chat.clear_view()
        self._welcomed = True
        self._welcome()
        self._reset_session_scoped_ui()
        self.status_bar.set_session(self.session.meta.session_id)
        # after the reset: clear_view() wipes anything added before it
        self.chat.add_notice(t("session_new_notice"), "info")

    @work(group="command", exclusive=True)
    async def cmd_sessions(self, args: str) -> None:
        """Browse sessions, then act on the chosen one (switch / show / delete)."""
        metas = await run_io(self.store.list_sessions, cwd=str(self.cwd), limit=15)
        if not metas:
            self.chat.add_notice(t("sessions_empty"), "info")
            return
        choice = await self._pick(
            t("picker_sessions"),
            [
                (
                    m.session_id,
                    # titles are user input AND land in markup=True pickers —
                    # a stray "[" used to MarkupError the whole /sessions flow
                    f"{(escape(m.title[:44]) or '(untitled)')} - {m.model} - {m.message_count} msgs - {m.updated_at[:16]}",
                )
                for m in metas
            ],
        )
        if not choice:
            return
        meta = next((m for m in metas if m.session_id == choice), None)
        action = await self._pick(t("sessions_action_title"), [
            ("switch", t("sessions_action_switch")),
            ("show", t("sessions_action_show")),
            ("delete", t("sessions_action_delete")),
        ])
        if action == "switch":
            await self._load_session(choice)
        elif action == "show" and meta is not None:
            self.chat.add_card(
                t("session_details", sid=meta.session_id[-8:], model=meta.model,
                  count=meta.message_count, at=meta.updated_at[:16])
                + f"\n[dim]{escape(meta.title) or '(untitled)'}[/dim]")
        elif action == "delete":
            confirmed = await self._pick(
                f"{t('sessions_action_delete')}: {choice[-8:]}", [
                    ("yes", t("sessions_confirm_yes")),
                    ("no", t("wizard_cancel")),
                ])
            if confirmed != "yes":
                self.chat.add_notice(t("wizard_cancelled"), "info")
                return
            if not await self._io_barrier_safe():
                return  # deletes must not race queued appends
            if await run_io(self.store.delete_session, choice):
                self.chat.add_notice(t("session_deleted_ok", sid=choice[-8:]), "info")
            else:
                self.notify_error(t("session_load_failed", sid=choice[-8:]),
                                  code="session.delete_failed", source="session")

    async def _load_session(self, session_id: str) -> None:
        if not await self._swap_out_turn():
            return  # the old turn must stop before records swap
        self._turn.bump()  # late events from the running turn stay on the old session
        if not await self._io_barrier_safe():
            return  # loads must see every append already queued
        loaded = await run_io(self.store.load, session_id)
        if loaded is None:
            self.notify_error(t("session_load_failed", sid=session_id),
                              code="session.load_failed", source="session",
                              hint=t("session_load_hint"))
            return
        self.host.attach_session(loaded)
        self.session = self.host.session
        self.conversation = self.host.conversation
        self._kernel = self.host.kernel
        self.chat.clear_view()
        self.chat.rehydrate(self.conversation)
        self._reset_session_scoped_ui()
        self.status_bar.set_session(session_id)
        self.chat.add_notice(t("restored_messages", count=len(self.conversation.messages)), "info")

    @work(group="util")
    async def _do_compact(self) -> None:
        from oaset.agent.context import SUMMARY_KEEP_RECENT, conversation_tokens, turn_boundary


        before = conversation_tokens(self.conversation)
        # messages the summary will replace — the same boundary compact_history
        # uses, and what the persisted marker records for a reload
        dropped = turn_boundary(self.conversation.messages, SUMMARY_KEEP_RECENT)
        self.chat.add_notice(t("compacting"), "info")
        try:
            from oaset.agent import compact_history

            summary = await compact_history(self.conversation, self.provider)
        except Exception as exc:
            self.notify_error(exc, source="compact", text=t("compact_failed", err=exc))
            return
        if not summary:
            self.chat.add_notice(t("compact_empty"), "info")
            return
        try:
            if await self._io_barrier_safe():  # marker must follow queued appends
                await run_io(self.store.append_compaction, self.session, summary, dropped)
        except Exception as exc:
            # The summary is only real once its marker is on disk. Reporting
            # "compacted" after a failed append told the user the transcript had
            # been trimmed when the next load brought all of it back.
            self.notify_error(exc, code="session.compact_persist_failed",
                              source="session", text=t("compact_persist_failed"))
            return
        from oaset.agent.context import conversation_tokens

        after = conversation_tokens(self.conversation)
        self.chat.add_notice(t("compacted", before=before, after=after), "info")

    async def cmd_rewind(self, args: str) -> None:
        """/undo N: drop the last N user turns from the transcript."""
        if self._worker_running():
            self.chat.add_notice(t("turn_running"), "warn")
            return
        arg = args.strip()
        # An unusable argument must not silently become "drop 1 turn": the user
        # typed something, so either honour it or say why it is not usable.
        if not arg.isdigit() or int(arg) < 1:
            self.chat.add_notice(t("rewind_usage", value=arg or "?"), "warn")
            return
        turns = int(arg)
        if not await self._io_barrier_safe():
            return  # truncation must happen after every queued append
        try:
            count = await run_io(self.store.rewind_turns, self.session, turns)
        except ValueError as exc:
            self.chat.add_notice(str(exc), "warn")
            return
        self.host.attach_session(self.session)
        self.conversation = self.host.conversation
        self._kernel = self.host.kernel
        self.chat.clear_view()
        self.chat.rehydrate(self.conversation)
        self.status_bar.set_tokens(self.conversation.token_estimate())
        self.chat.add_notice(t("rewound", turns=turns, count=count), "info")

    async def cmd_title(self, args: str) -> None:
        title = args.strip()
        if not title:
            current = self.session.meta.title or "(untitled)"
            self.chat.add_notice(t("title_current", title=current), "info")
            return
        if not await self._io_barrier_safe():
            return  # title rewrite must not race queued appends
        if await run_io(self.store.set_title, self.session.meta.session_id, title):
            self.session.meta.title = title
            self.status_bar.set_session(self.session.meta.session_id)
            self.chat.add_notice(t("title_set", title=title), "info")
        else:
            self.notify_error(t("title_failed"), code="session.title_failed", source="session")

    def cmd_retry(self, args: str) -> None:
        last = getattr(self, "_last_user_prompt", "")
        if not last:
            self.chat.add_notice(t("retry_none"), "warn")
            return
        self.submit_text(last)  # submit_text adds the spacer + message itself

    def cmd_history(self, args: str) -> None:
        msgs = self.conversation.messages
        if not msgs:
            self.chat.add_notice(t("history_empty"), "info")
            return
        lines = []
        for i, m in enumerate(msgs[-40:], 1):
            body = one_line(m.display_text() if hasattr(m, "display_text") else str(m.content or ""), 90)
            glyph = {"user": "›", "assistant": "●", "tool": "⚙"}.get(m.role, "·")
            lines.append(f"{i:>3} {glyph} {body}")
        body = "\n".join(lines)
        self.chat.add_notice(t("history_header", count=len(lines)) + "\n" + body, "info")

    def cmd_compact(self, args: str) -> None:
        if self._worker_running():
            self.chat.add_notice(t("turn_running"), "warn")
            return
        self._do_compact()

    async def cmd_fork(self, args: str) -> None:
        """Fork the current session, or a chosen past one."""
        source = args.strip()
        if not source:
            metas = [m for m in await run_io(self.store.list_sessions, cwd=str(self.cwd), limit=15)
                     if m.session_id != self.session.meta.session_id]
            choice = await self._pick(t("fork_pick"), [
                ("__current__", t("fork_current")),
            ] + [(m.session_id,
                  f"{(m.title[:44] or '(untitled)')} - {m.message_count} msgs")
                 for m in metas])
            if not choice:
                return
            source = "" if choice == "__current__" else choice
        if not await self._swap_out_turn():
            return  # fork reads the record: stop the old turn first
        self._turn.bump()
        if not await self._io_barrier_safe():
            return  # fork reads the transcript: see every queued append
        if source:
            loaded = await run_io(self.store.load, source)
            if loaded is None:
                self.notify_error(t("session_load_failed", sid=source[-8:]),
                                  code="session.load_failed", source="session")
                return
            base = loaded
        else:
            base = self.session
        clone = await run_io(self.store.fork, base)
        clone.conversation.system_prompt = self.conversation.system_prompt
        # Attach through the host, exactly like _load_session: setting the
        # attributes here alone left host.session / host.conversation /
        # kernel.session_id pointing at the OLD session, so snapshot() (ACP,
        # desktop panels) reported the pre-fork session id and message list
        # while the UI showed the clone.
        self.host.attach_session(clone)
        self.session = self.host.session
        self.conversation = self.host.conversation
        self._kernel = self.host.kernel
        self.chat.clear_view()
        self.chat.rehydrate(self.conversation)
        self._reset_session_scoped_ui()
        self.status_bar.set_session(clone.meta.session_id)
        self.chat.add_notice(t("forked_to", sid=clone.meta.session_id[-8:]), "info")

    async def cmd_undo(self, args: str) -> None:
        """Restore a checkpoint — pick one from the list instead of guessing."""
        if self._worker_running():
            self.chat.add_notice(t("turn_running"), "warn")
            return
        if not await self._io_barrier_safe():
            return  # list and restore must see every queued write
        rows = await run_io(self.store.list_checkpoints, self.session)
        if not rows:
            self.chat.add_notice(t("undo_none"), "info")
            return
        choice = await self._pick(t("undo_pick"), [
            (str(row["n"]), t("undo_entry", n=row["n"], at=row["at"][:19],
                              count=row["count"],
                              files=", ".join(Path(f).name for f in row["files"][:3])))
            for row in rows
        ])
        if not choice:
            return
        result = await run_io(self.store.undo_checkpoint, self.session, int(choice))
        if result is None:
            self.chat.add_notice(t("undo_none"), "info")
            return
        number, count = result
        self.chat.add_notice(t("undo_done", n=number, count=count), "info")

    async def cmd_search(self, args: str) -> None:
        """Search past sessions.

        `search_history` is NOT an in-memory index read: its first statement is
        `index_all(home)`, which rglobs every `session_*.jsonl` and writes the
        index. Run on the UI thread that froze the whole TUI for seconds on a
        large history (no repaint, Escape dead, streaming stalled). It now runs
        on the ordered IO thread with a visible "searching" line, exactly like
        /insights does for its disk scan.
        """
        query = args.strip()
        self.chat.add_notice(t("search_running", query=query or t("search_recent")), "info")
        try:
            hits = await run_io(search_history, query, limit=8)
        except Exception as exc:
            self.notify_error(exc, code="history.search_failed", source="history")
            return
        if not hits:
            self.chat.add_notice(t("search_no_match", query=query), "info")
            return
        lines = "\n".join(
            f"[{i}] {h.title} ({h.session_id[-8:]}, {h.role}) {h.snippet}"
            for i, h in enumerate(hits, 1)
        )
        self.chat.add_notice(t("search_header", query=query or "(recent)") + "\n" + lines, "info")
        # The hits used to be a dead end: the list ended with "copy the id and
        # use /sessions". Picking one opens it directly, matching how /sessions
        # and /undo already work.
        choice = await self._pick(
            t("search_open_title"),
            [(h.session_id, f"{h.title or t('status_untitled')} · {h.role} · {h.snippet}")
             for h in hits])
        if not choice:
            self.chat.add_notice(t("search_pull_hint"), "info")
            return
        if choice == self.session.meta.session_id:
            self.chat.add_notice(t("search_already_open"), "info")
            return
        await self._load_session(choice)

    async def cmd_session_delete(self, args: str) -> None:
        sid = args.strip()
        if not sid:
            self.chat.add_notice(t("session_delete_usage"), "warn")
            return
        if self.session.meta.session_id.endswith(sid):
            self.chat.add_notice(t("session_delete_current"), "warn")
            return
        await io_barrier()  # deletes must not race queued appends
        if await run_io(self.store.delete_session, sid):
            self.chat.add_notice(t("session_deleted", sid=sid), "info")
        else:
            self.chat.add_notice(t("session_not_found", sid=sid), "warn")
