"""Input/content commands (P4-1 split from app.py).

Methods move verbatim; they keep working through `self` (OasetApp
inherits this mixin). Imports here cover what the moved bodies use;
anything else is imported locally inside a method, as before.
"""

from __future__ import annotations

import contextlib
import subprocess

from textual import work
from textual.widgets import Static

from oaset.config import save_config
from oaset.i18n import t
from oaset.memory import read_memory
from oaset.tui.glyphs import TODO_GLYPHS as TODO_GLYPH
from oaset.tui.widgets.status_bar import fmt_k
from oaset.utils import one_line, run_io

# How much of the persisted memory a chat notice shows before it points the
# user at the full-screen viewer. Matches the "clamp long output, keep it
# reachable" rule the notice card already follows.
MEMORY_PREVIEW_CHARS = 2000


class ContentCmdsMixin:
    """Input/content commands (P4-1 split from app.py)."""

    def cmd_dequeue(self, args: str) -> None:
        """Drop every queued message (P1-6): Enter-while-running holds text."""
        n = len(self.input_queue)
        self.input_queue.clear()
        self.status_bar.set_queue_count(0)
        self.chat.add_notice(t("dequeue_cleared", n=n) if n else t("dequeue_empty"), "info")

    async def cmd_view(self, args: str) -> None:
        """Open a message full-screen so the terminal's NATIVE character-level
        selection works — block-granularity is all the chat log can offer."""
        which = args.strip().lower() or "answer"
        title, text = self._view_payload(which)
        if not text.strip():
            self.chat.add_notice(t("view_empty", which=which), "warn")
            return
        from oaset.tui.viewer import view_text

        await view_text(self, f"{title} · {self.session.meta.session_id[-8:]}", text)

    def _view_payload(self, which: str) -> tuple[str, str]:
        msgs = self.conversation.messages
        if which == "user":
            for m in reversed(msgs):
                if m.role == "user":
                    body = m.display_text()
                    if isinstance(body, str) and body.strip():
                        return (t("view_kind_user"), body)
            return (t("view_kind_user"), "")
        if which in ("tools", "tool"):
            for m in reversed(msgs):
                if m.role == "tool" and isinstance(m.content, str) and m.content.strip():
                    return (t("view_kind_tool"), m.content)
            return (t("view_kind_tool"), "")
        for m in reversed(msgs):
            if m.role == "assistant" and isinstance(m.content, str) and m.content.strip():
                return (t("view_kind_answer"), m.content)
        return (t("view_kind_answer"), "")

    def cmd_clear(self, args: str) -> None:
        self.chat.clear_view()
        self._welcome()
        # /clear reads as "start over": a pinned error from the cleared view or
        # an attachment staged for a message that will never be sent must not
        # survive it.
        self._reset_session_scoped_ui()
        self.chat.add_notice(t("cleared_notice"), "info")

    def cmd_todo(self, args: str) -> None:
        self.action_toggle_sidebar()

    @work(group="command", exclusive=True)
    async def cmd_theme(self, args: str) -> None:
        if args.strip():
            self._apply_theme(args.strip())
            return
        choice = await self.prompt_missing_arg("theme")
        if choice:
            self._apply_theme(choice)

    @work(group="command", exclusive=True)
    async def cmd_language(self, args: str) -> None:
        choice = args.strip().lower()
        if choice in ("zh-cn", "zh_cn", "cn", "chinese", "中文"):
            choice = "zh"
        if choice in ("eng", "english"):
            choice = "en"
        if choice not in ("zh", "en"):
            picked = await self.prompt_missing_arg("language")
            if not picked:
                return
            choice = picked
        self._apply_language(choice)

    def _persist_config(self, *, what: str) -> bool:
        """Write config.toml, reporting failure instead of swallowing it.

        `/language`, `/theme`, `/density` and `/tools-toggle` all used
        `except Exception: pass` and then announced success. The setting looked
        applied, reverted on restart, and the user had no way to tell that
        anything had gone wrong.
        """
        try:
            save_config(self.cfg)
        except Exception as exc:
            self.notify_error(exc, code="config.write_failed", source="config",
                              text=t("config_write_failed", what=what))
            return False
        return True

    def _apply_language(self, lang: str) -> None:
        """Persist zh/en, refresh chrome that already painted in the old language."""
        from oaset.i18n import set_language

        if lang not in ("zh", "en"):
            self.chat.add_notice(t("lang_usage"), "warn")
            return
        set_language(lang)
        self.cfg.ui_language = lang
        self.cfg.language_chosen = True
        persisted = self._persist_config(what="ui.language")
        with contextlib.suppress(Exception):
            self.input_area.placeholder = t("input_placeholder")
        self.status_bar._refresh()
        self._refresh_welcome()  # the page painted in the old language
        if persisted:
            self.chat.add_notice(t("lang_set"), "info")

    @work(group="language", exclusive=True)
    async def _first_run_flow(self) -> None:
        """Fresh install: language, then ONE skippable offer to finish setup.

        A downloaded-from-GitHub install should be able to reach a usable
        state without knowing any slash command; but nothing is forced —
        Esc/稍后 leaves the welcome page's 暂无配置 guidance in charge, and
        the offer appears only inside the very first run."""
        import os

        first_run = not self.cfg.language_chosen and not os.environ.get("OASET_LANG")
        try:
            if first_run:
                await self._offer_language()
            if first_run and self._model_needs_key():
                await self._offer_setup()
        finally:
            # The flow is over: hand the keyboard back to the composer no
            # matter what the nested prompts restored focus to (two prompts
            # unwinding could leave focus on neither the input nor anything
            # usable — the user just saw "type a question" and could not).
            if first_run:
                self.call_after_refresh(self._restore_focus, None)

    async def _offer_setup(self) -> None:
        choice = await self._pick(t("first_setup_title"), [
            ("now", t("first_setup_now")),
            ("later", t("first_setup_later")),
        ])
        if choice == "now":
            await self._onboarding_flow()

    async def _offer_language(self) -> None:
        """First launch: pick 中文 or English before the chrome settles."""
        choice = await self._pick(
            t("lang_pick_title"),
            [("zh", t("lang_pick_zh")), ("en", t("lang_pick_en"))],
        )
        if choice in ("zh", "en"):
            self._apply_language(choice)

    def cmd_init(self, args: str) -> None:
        if self._worker_running():
            self.chat.add_notice(t("turn_running"), "warn")
            return
        # The instruction that drives the turn is user-visible output in the
        # model's language — it must follow the UI language like every text.
        self.chat.add_notice(t("init_analyzing"), "info")
        self.submit_text(t("init_prompt"))

    @staticmethod
    def _goal_block(goal: dict) -> str:
        return ("# Current goal\n" + str(goal.get("text", ""))
                + "\nWork toward this goal until it is complete; report progress each turn.")

    def _apply_goal_to_prompt(self) -> None:
        """Re-derive the system prompt (skills + the session's goal).

        The goal used to be `self.system_prompt += goal_block`, which had two
        failure modes that this single rebuild removes: setting a second goal
        left BOTH blocks in the prompt (the model got contradictory
        objectives, and the prompt grew on every change), and any skills
        refresh rebuilt the prompt from scratch and dropped the goal block
        entirely while `session_state["goal"]` still claimed a goal was active.
        """
        from oaset.agent import initial_system_prompt

        system_prompt, _ = initial_system_prompt(self.cwd)
        goal = self.registry.ctx.session_state.get("goal")
        if goal and goal.get("text"):
            system_prompt = f"{system_prompt}\n\n{self._goal_block(goal)}"
        self.system_prompt = system_prompt
        self.conversation.system_prompt = system_prompt
        if getattr(self, "host", None) is not None:
            self.host.system_prompt = system_prompt
            self.host.conversation.system_prompt = system_prompt

    def cmd_goal(self, args: str) -> None:
        """A persistent objective with an optional token budget."""
        arg = args.strip()
        state = self.registry.ctx.session_state
        if not arg or arg in ("status", "show"):
            goal = state.get("goal")
            if not goal:
                self.chat.add_notice(t("goal_none"), "info")
                return
            used = self._kernel.last_usage.get("total_tokens", 0) if isinstance(self._kernel.last_usage, dict) else 0
            budget = goal.get("budget_tokens") or 0
            budget_line = f" · {fmt_k(used)}/{budget} tok" if budget else f" · {fmt_k(used)} tok"
            self.chat.add_notice(t("goal_active_line", text=goal["text"]) + budget_line, "info")
            return
        if arg in ("stop", "clear"):
            state.pop("goal", None)
            self._kernel.budget_tokens = 0
            self.status_bar.set_goal(None)
            self._apply_goal_to_prompt()
            self.chat.add_notice(t("goal_cleared"), "info")
            return
        if arg.startswith("budget"):
            parts = arg.split(None, 1)
            goal = state.get("goal")
            if goal is None:
                self.chat.add_notice(t("goal_first"), "warn")
                return
            try:
                tokens = int(float(parts[1]) * 1000) if len(parts) > 1 else 0
            except ValueError:
                self.chat.add_notice(t("goal_budget_usage"), "warn")
                return
            if tokens < 0:
                # a negative budget makes `used >= budget` true forever: every
                # later turn was refused with "budget exhausted" until /goal stop
                self.chat.add_notice(t("goal_budget_negative"), "warn")
                return
            goal["budget_tokens"] = tokens
            self._kernel.budget_tokens = tokens
            self.chat.add_notice(t("goal_budget_set", tokens=tokens), "info")
            return
        goal = {"text": arg, "budget_tokens": 0}
        state["goal"] = goal
        self._apply_goal_to_prompt()
        self.status_bar.set_goal(arg)
        self.chat.add_notice(t("goal_set", text=arg), "info")

    async def action_external_editor(self) -> None:
        """Edit the draft in an external editor.

        The editor runs on a worker thread — blocking the event loop here
        would freeze streaming, timers and Esc for as long as the editor is
        open."""
        import asyncio
        import os as _os
        import subprocess
        import tempfile
        from pathlib import Path as _Path

        editor = self.cfg.ui.editor or _os.environ.get("VISUAL") or _os.environ.get("EDITOR") or ""
        if not editor:
            self.chat.add_notice(t("editor_none"), "warn")
            return
        draft = self.input_area.text
        fh = tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8")
        fh.write(draft)
        fh.close()
        try:
            with self.suspend():
                try:
                    await asyncio.to_thread(
                        subprocess.run, [*editor.split(), fh.name], check=False)
                except OSError as exc:
                    self.notify_error(exc, source="editor")
                    return
            from oaset.tui._win_mouse_fix import reapply_console_mode

            reapply_console_mode()
            text = _Path(fh.name).read_text(encoding="utf-8")
            self.input_area.load_text(text)
        finally:
            with contextlib.suppress(OSError):
                _os.unlink(fh.name)

    def cmd_image(self, args: str) -> None:
        """Attach an image file to the next message (multipart, image_in models)."""
        from oaset.tui.commands import arg_spec_for
        from oaset.utils import expand_path

        raw = args.strip().strip('"')
        sub = raw.lower()
        if sub in ("clear", "off"):
            dropped = len(self._pending_images)
            self._pending_images.clear()
            self.chat.add_notice(t("image_cleared", n=dropped), "info")
            return
        if sub in ("list", "ls", ""):
            # bare /image shows what is queued instead of an error: the list
            # was previously invisible and silently consumed later
            pending = list(self._pending_images)
            if not pending:
                self.chat.add_notice(t("image_usage"), "warn")
            else:
                body = "\n".join(f"  · {p}" for p in pending)
                self.chat.add_notice(
                    t("image_pending", n=len(pending)) + "\n" + body, "info")
            return
        spec = arg_spec_for("image")
        suffixes = tuple(spec.filter_suffixes) if spec else ()
        path = expand_path(raw, self.cwd)
        reason = self._path_rejection(path, suffixes)
        if reason is not None:
            self.chat.add_notice(reason, "warn")
            return
        if not self.model_cfg.has("image_in"):
            self.chat.add_notice(t("image_unsupported", model=self.model_cfg.id), "warn")
            return
        self._pending_images.append(path)
        # attachments were invisible after one notice and silently consumed
        # by the NEXT message (even many turns later): always say how many
        # are pending and how to drop them
        self.chat.add_notice(
            t("image_attached", name=path.name)
            + f"  ({t('image_pending_now', n=len(self._pending_images))})", "info")

    async def _attach_clipboard_image(self, png: bytes) -> None:
        """Save a clipboard PNG under ~/.oaset/pastes and queue it for the next send."""
        import asyncio as _aio

        from oaset.tui.paste import prune_pastes
        from oaset.utils import oaset_home

        if not self.model_cfg.has("image_in"):
            self.chat.add_notice(t("image_unsupported", model=self.model_cfg.id), "warn")
            return
        folder = oaset_home() / "pastes"
        import time as _time
        import uuid as _uuid

        # unique per capture: two screenshots in the same second used to
        # overwrite each other while both attachments pointed at the survivor
        path = folder / (_time.strftime("clip-%Y%m%d-%H%M%S")
                         + f"-{_uuid.uuid4().hex[:6]}.png")

        def _write() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)

        try:
            await _aio.to_thread(_write)
        except OSError as exc:
            self.chat.add_notice(t("paste_inline_fallback", n=len(png)), "warn")
            self.notify_error(exc, source="clipboard", code="clipboard.paste_write_failed")
            return
        prune_pastes(folder)  # clipboard images pile up otherwise
        self._pending_images.append(path)
        self.chat.add_notice(t("image_attached", name=path.name), "info")

    async def cmd_worktree(self, args: str) -> None:
        """`/worktree [new <name>|list|remove <name>]` — git worktree isolation.

        Every branch shells out to git (a full checkout for `new`): it runs in
        a worker thread because 20s of subprocess.run on the event loop froze
        the whole TUI (no repaint, no Esc).
        """
        import asyncio as _aio

        from oaset.worktree import WorktreeError, create_worktree, list_worktrees, remove_worktree

        parts = args.strip().split(None, 1)
        action = (parts[0] if parts else "list").lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            if action in ("new", "add", "create"):
                if not rest:
                    self.chat.add_notice(t("worktree_usage"), "warn")
                    return
                dest = await _aio.to_thread(create_worktree, self.cwd, rest)
                self.chat.add_notice(t("worktree_created", path=str(dest)), "info")
            elif action in ("rm", "remove", "delete"):
                if not rest:
                    self.chat.add_notice(t("worktree_usage"), "warn")
                    return
                dest = await _aio.to_thread(remove_worktree, self.cwd, rest)
                self.chat.add_notice(t("worktree_removed", path=str(dest)), "info")
            else:
                rows = await _aio.to_thread(list_worktrees, self.cwd)
                if not rows:
                    self.chat.add_notice(t("worktree_empty"), "info")
                    return
                lines = [t("worktree_header")]
                from rich.markup import escape as _esc

                for row in rows:
                    lines.append(f"  {_esc(str(row.get('worktree', '')))}  {_esc(str(row.get('branch', '')))}")
                self.chat.add_card("\n".join(lines))
        except WorktreeError as exc:
            self.notify_error(exc, code="worktree.failed", source="git")
        except (subprocess.TimeoutExpired, OSError) as exc:
            # big-repo `git worktree add` hits the 20s timeout as routine;
            # a bare TimeoutExpired used to surface as "internal error"
            self.notify_error(exc, code="worktree.failed", source="git",
                              text=t("worktree_io_failed", err=str(exc)))

    async def cmd_copy(self, args: str) -> None:
        """`/copy [selection|last|tools|all|input]`.

        No argument is the common case and must stay non-blocking: it copies the
        selection when there is one, else the draft - exactly what Ctrl+Shift+C
        does. Naming a target reaches anything else; an unknown target lists the
        targets instead of opening a menu (a menu would block a scripted call).
        """
        target = args.strip().lower() or ("selection" if self.selected_text() else "input")

        if target in ("input", "draft"):
            text = self.input_area.text
        elif target in ("selection", "selected", "sel"):
            text = self.selected_text()
            if not text:
                self.chat.add_notice(t("copy_no_selection"), "warn")
                return
        elif target in ("last", "reply", "answer"):
            text = self._last_assistant_text()
        elif target in ("tools", "tool", "outputs"):
            import asyncio as _aio

            text = await _aio.to_thread(self.chat.tool_outputs_text)
        elif target in ("all", "transcript", "conversation"):
            import asyncio as _aio

            text = await _aio.to_thread(self.chat.transcript_text)
        else:
            self.chat.add_notice(t("copy_unknown_target", target=target), "warn")
            return

        if not text:
            self.chat.add_notice(t("copy_nothing"), "warn")
            return
        await self._copy_text(text)

    async def cmd_paste(self, args: str) -> None:
        # feedback lives in action_paste_clipboard, which is the only place
        # that knows whether the clipboard had anything
        await self.input_area.action_paste_clipboard()

    def cmd_steer(self, args: str) -> None:
        text = args.strip()
        if not text:
            self.chat.add_notice(t("steer_usage"), "warn")
            return
        if not self._worker_running():
            self.chat.add_notice(t("steer_hint"), "warn")
            return
        self._kernel.injections.append(text)
        self.chat.add_user(text)
        self.chat.add_notice(t("steered"), "info")

    def cmd_context(self, args: str) -> None:
        """/context — what is eating the context window right now.

        The long-session question is never "how big is the model" but "which
        part of MY context is huge": this breaks it into system prompt (with
        the skills/memory/project blocks inside it), tool schemas, and
        messages by role, against the window with a compact hint past 75%.
        """
        import json as _json

        from oaset.utils import estimate_tokens

        prompt_tokens = estimate_tokens(self.system_prompt or "")
        tools_json = _json.dumps([tool.schema() for tool in self.registry.tools.values()])
        tools_tokens = estimate_tokens(tools_json)
        roles = {"user": 0, "assistant": 0, "tool": 0}
        msg_tokens = 0
        for m in self.conversation.messages:
            roles[m.role] = roles.get(m.role, 0) + 1
            body = m.display_text() if hasattr(m, "display_text") else str(m.content or "")
            msg_tokens += estimate_tokens(body)
        total = prompt_tokens + tools_tokens + msg_tokens
        max_ctx = self.model_cfg.max_context_size or 1
        pct = min(100.0, 100.0 * total / max_ctx)
        lines = [
            f"[b]/context — {t('context_title')}[/b]",
            f"  {t('context_prompt')}  {prompt_tokens:>7} tok",
            f"  {t('context_tools', n=len(self.registry.tools))}  {tools_tokens:>7} tok",
            f"  {t('context_messages', user=roles['user'], assistant=roles['assistant'], tool=roles['tool'])}  {msg_tokens:>7} tok",
            "",
            "  " + t("context_total", tokens=f"{total:,}", max=f"{max_ctx:,}", pct=pct),
        ]
        if pct >= 75:
            lines.append("  [yellow]" + t("context_compact_hint") + "[/yellow]")
        # real provider accounting: cache hits are the one number that tells
        # you whether the stable prefix is actually being reused. Last turn
        # = "is it hitting right now"; session sums = "what has it saved me
        # so far" — both from one transcript read (usage_summary).
        try:
            summary = self.store.usage_summary(self.session) if self.session else {}
        except Exception:
            summary = {}
        last = summary.get("last") or {}
        read = int((last or {}).get("cache_read_tokens") or 0)
        write = int((last or {}).get("cache_write_tokens") or 0)
        if read or write:
            prompt_real = int((last or {}).get("prompt_tokens") or 0)
            pct_hit = (100.0 * read / prompt_real) if prompt_real else 0.0
            lines.append("  " + t("context_cache", read=f"{read:,}",
                                   write=f"{write:,}", pct=f"{pct_hit:.0f}"))
        sess_read = int(summary.get("cache_read_tokens") or 0)
        sess_prompt = int(summary.get("prompt_tokens") or 0)
        if sess_read and sess_prompt:
            sess_pct = min(100.0, 100.0 * sess_read / sess_prompt)
            turns = int(summary.get("entries") or 0)
            lines.append("  " + t("context_cache_session", read=f"{sess_read:,}",
                                   pct=f"{sess_pct:.0f}", turns=turns))
            # a low rate on a long session is worth a diagnosis, not just a
            # number — each cause names its own fix
            if sess_pct < 50.0 and turns >= 3:
                lines.append("  [yellow]"
                             + t("context_cache_low", pct=f"{sess_pct:.0f}")
                             + "[/yellow]")
        self.chat.add_card("\n".join(lines))

    def cmd_evidence(self, args: str) -> None:
        """/evidence on|off — the Evidence-Gated Done switch.

        While on, every successful verification call is recorded as a receipt
        in the session transcript, open todo items count as unmet
        requirements, and "done" claims without receipts are pushed back by
        the verify gate. The receipts replay via `oaset evidence <session>`.
        """
        state = self.registry.ctx.session_state
        choice = args.strip().lower()
        if choice in ("on", "off"):
            state["evidence"] = choice == "on"
        current = bool(state.get("evidence"))
        entries = self.store.evidence_entries(self.session.meta.session_id) \
            if self.session else []
        status = t("evidence_status_on") if current else t("evidence_status_off")
        self.chat.add_notice(status + "  " +
                             t("evidence_count", count=len(entries)), "info")

    async def cmd_memory(self, args: str) -> None:
        """Show persisted memory.

        Long memory used to be cut at 2000 characters with nothing telling the
        user that content had been dropped, and no way to see the rest — a
        silent truncation of exactly the text they asked to read. It now shows
        a clamped preview plus the same `/view` escape hatch `/errors` has.
        """
        global_mem = read_memory("global")
        user_mem = read_memory("user")
        if not global_mem and not user_mem:
            self.chat.add_notice(t("memory_empty"), "info")
            return
        body = ""
        if global_mem:
            body += f"[MEMORY.md]\n{global_mem}\n"
        if user_mem:
            body += f"[USER.md]\n{user_mem}"
        if args.strip() in ("all", "full"):
            from oaset.tui.viewer import view_text

            await view_text(self, t("memory_viewer_title"), body)
            return
        if len(body) > MEMORY_PREVIEW_CHARS:
            shown = body[:MEMORY_PREVIEW_CHARS].rstrip()
            self.chat.add_notice(
                shown + "\n" + t("memory_truncated", n=len(body) - len(shown)), "info")
            self.chat.add_notice(t("memory_view_hint"), "info")
            return
        self.chat.add_notice(body, "info")

    def autoshow_plan(self) -> None:
        """Pin the plan strip above the input while steps remain."""
        if self._sidebar_pref is False:
            return
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        """Compact plan strip above the input. Pinned while work remains."""
        sidebar = self.side_bar
        if self.registry is None or not sidebar.is_attached or sidebar._closing:
            return  # fast-boot window or teardown race: nothing to mount into
        todos = list(self.registry.ctx.session_state.get("todos", []) or [])
        done, total = self.plan_progress()
        live = total > 0 and done < total
        if self._sidebar_pref is False or (not live and self._sidebar_pref is not True):
            sidebar.display = False
            return
        sidebar.display = True
        sidebar.remove_children()
        if not todos:
            sidebar.mount(Static(f"{t('sidebar_todos')}  {t('sidebar_no_todos')}",
                                 markup=False))
            return
        width = max(24, (self.size.width or 80) - 6)
        ordered = sorted(todos, key=lambda td: 0 if td.get("status") == "in_progress" else 1)
        lines = [f"{t('sidebar_todos')} {done}/{total}"]
        for todo in ordered[:5]:
            status = todo.get("status", "pending")
            glyph = TODO_GLYPH.get(status, "○")
            mark = "▸ " if status == "in_progress" else "  "
            lines.append(f"{mark}{glyph} {one_line(str(todo.get('content', '')), width)}")
        extra = len(ordered) - 5
        if extra > 0:
            lines.append(f"  … +{extra}")
        sidebar.mount(Static("\n".join(lines), markup=False))

    def _tool_backend_notes(self) -> dict[str, str]:
        """Why a registered tool cannot actually run here — better a visible
        "unavailable: reason" than a command that exists but cannot work."""
        notes: dict[str, str] = {}
        try:
            from oaset.lsp import load_lsp_config

            if not load_lsp_config():
                notes["lsp_diagnostics"] = "no [lsp] servers in config.toml"
        except Exception:
            pass
        try:
            from oaset.computer.cdp import _find_browser_executable

            if not _find_browser_executable():
                for name in ("browser_open", "browser_observe", "browser_click",
                             "browser_type", "browser_download", "browser_upload",
                             "browser_screenshot", "browser_close"):
                    notes[name] = "needs local Edge/Chrome"
        except Exception:
            pass
        import os as _os

        if _os.name != "nt":
            for name in ("desktop_windows", "desktop_find", "desktop_click",
                         "desktop_type", "desktop_screenshot"):
                notes[name] = "Windows-only (UIA)"
        return notes

    def cmd_tools(self, args: str) -> None:
        disabled = sorted(getattr(self.registry, "disabled", set()))
        notes = self._tool_backend_notes()
        lines = "\n".join(
            f"· {name}"
            + ("  [disabled]" if name in disabled else "")
            + (f"  [⚠ {note}]" if (note := notes.get(name)) else "")
            for name in sorted(self.registry._all)
        )
        self.chat.add_notice(t("tools_header") + "\n" + lines, "info")

    @work(group="command", exclusive=True)
    async def cmd_tools_toggle(self, args: str) -> None:
        options = [
            (name, f"{'disable' if name not in self.registry.disabled else 'enable'} {name}")
            for name in sorted(self.registry._all)
        ]
        choice = await self._pick(t("picker_tool"), options)
        if not choice:
            return
        disabled = set(self.registry.disabled)
        if choice in disabled:
            disabled.discard(choice)
        else:
            disabled.add(choice)
        self.registry.set_toggles(enabled=self.cfg.enabled_tools or None, disabled=disabled)
        self.cfg.disabled_tools = sorted(disabled)
        persisted = self._persist_config(what="disabled_tools")
        self.chat.add_notice(t("tool_toggled", name=choice, state=t("state_" + ("dis" if choice in disabled else "") + "enabled")), "info")
        if not persisted:
            self.chat.add_notice(t("tool_toggle_not_persisted"), "warn")

    async def cmd_density(self, args: str) -> None:
        """Cozy (default) vs compact chat spacing; persisted to config.toml."""
        choice = args.strip().lower()
        if not choice:
            # An empty argument means "tell me the current value", never "flip
            # the persisted setting": `/density` with no argument used to
            # silently switch the user to compact.
            self.chat.add_notice(
                t("density_current", mode=self.cfg.ui.density or "cozy"), "info")
            return
        if choice not in ("cozy", "compact"):
            self.chat.add_notice(t("density_usage"), "warn")
            return
        from oaset.config import save_config

        self.cfg.ui.density = choice
        self.remove_class("compact")
        if choice == "compact":
            self.add_class("compact")
        try:
            await run_io(save_config, self.cfg)
        except Exception as exc:
            self.notify_error(exc, source="config", code="config.write_failed")
            return
        self.chat.add_notice(t("density_set", mode=choice), "info")

    def cmd_plans(self, args: str) -> None:
        """/plans — list idea-forge plan documents in this workspace.

        The skill writes confirmed plans to .oaset/plans/<slug>.md; without
        a listing surface they were write-only. Newest first, one line per
        plan, /view to read one.
        """
        import time as _time

        plans_dir = self.cwd / ".oaset" / "plans"
        rows: list[tuple[float, str, str]] = []
        if plans_dir.is_dir():
            for path in sorted(plans_dir.glob("*.md")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                title = ""
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    for line in text.splitlines():
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
                except OSError:
                    pass
                rows.append((stat.st_mtime, path.name, title))
        if not rows:
            self.chat.add_notice(t("plans_empty"), "info")
            return
        rows.sort(reverse=True)
        lines = [f"[b]/plans — {t('plans_title')}[/b]", ""]
        for _mtime, name, title in rows[:20]:
            when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(_mtime))
            lines.append("  " + t("plans_row", name=name, when=when,
                                   title=title or "—"))
        lines.append("")
        lines.append(f"[dim]/view {plans_dir / '<name>'}[/dim]")
        self.chat.add_card("\n".join(lines))
