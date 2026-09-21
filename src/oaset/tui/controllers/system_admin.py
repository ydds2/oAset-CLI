"""System, update, plugin/skill/agent admin (P4-1 split from app.py).

Methods move verbatim; they keep working through `self` (OasetApp
inherits this mixin). Imports here cover what the moved bodies use;
anything else is imported locally inside a method, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.markup import escape as _esc
from textual import work

from oaset import __version__
from oaset import cron as cronmod
from oaset.config import save_config
from oaset.credentials import list_credentials
from oaset.hooks import HookRunner
from oaset.i18n import t
from oaset.runtime_info import describe_source
from oaset.skills import find_skill, load_skills
from oaset.tui.commands import Command
from oaset.tui.notice import UiNotice
from oaset.tui.themes import load_custom_themes, normalize_theme, register_themes
from oaset.tui.widgets.inline import InlineInput
from oaset.tui.widgets.status_bar import fmt_k
from oaset.utils import oaset_home

# Read-only update-center actions run straight from the picker; apply and
# rollback are separate actions with their own confirmation and permission gate.
UPDATE_ACTIONS = ("check", "refresh", "list", "plan", "apply", "rollback", "history")
UPDATE_APPLY_COMMAND = Command(
    "update apply", "Install catalog targets", "<name…>", category="system",
    risk="danger", effect="下载并替换插件/配置/核心包（可用 rollback 撤销）")
UPDATE_ROLLBACK_COMMAND = Command(
    "update rollback", "Restore a recorded apply", "[target|last]", category="system",
    risk="danger", effect="按历史记录恢复上一次 apply 之前的文件状态")


class SystemAdminMixin:
    """System, update, plugin/skill/agent admin (P4-1 split from app.py)."""

    if TYPE_CHECKING:
        # Host attributes these methods mutate; the real definitions (with
        # full types) live in OasetApp.__init__. Declaring them here keeps
        # base-class inference in app.py well-typed.
        _fallbacks: list[Any]
        model_cfg: Any
        provider: Any

    @work(group="command", exclusive=True)
    async def cmd_update(self, args: str) -> None:
        try:
            from oaset.update import UpdateManager
        except ImportError:
            self.chat.add_notice(t("update_missing"), "warn")
            return
        action = args.strip().split(None, 1)[0].lower() if args.strip() else ""
        rest = args.strip().split(None, 1)[1].strip() if len(args.strip().split(None, 1)) > 1 else ""
        if action not in UPDATE_ACTIONS:
            # one schema, one form: the palette and the typed path agree
            action = await self.prompt_missing_arg("update") or ""
            if not action:
                return
        tokens = rest.replace(",", " ").split()
        from oaset.update import KINDS
        # kind/detail tokens are only meaningful for read-only actions; for
        # apply/rollback the rest is a target list, taken verbatim (a target
        # named `model` or `core` must not be swallowed as a filter).
        read_only = action in ("check", "refresh", "list", "plan")
        kind = next((tok.lower() for tok in tokens if tok.lower() in KINDS), None) \
            if read_only else None
        detail = any(tok.lower().lstrip("-") == "detail" for tok in tokens) if read_only else False
        rest = " ".join(tok for tok in tokens
                        if not read_only
                        or (tok.lower() not in KINDS and tok.lower().lstrip("-") != "detail"))
        try:
            manager = UpdateManager(self.cfg)
            if action == "apply":
                await self._update_apply(manager, rest)
                return
            if action == "rollback":
                await self._update_rollback(manager, rest)
                return
            if action == "history":
                self.chat.add_card(self._update_history(manager))
                return
            result = await manager.run(action, kind=kind)
        except Exception as exc:
            self.notify_error(exc, source="update", text=t("update_failed", err=exc))
            return
        if detail and hasattr(result, "render") and not hasattr(result, "targets"):
            self.chat.add_card(result.render(detail=True))
        else:
            self.chat.add_card(result.render() if hasattr(result, "render") else str(result))

    async def _update_apply(self, manager, preset: str) -> None:
        """Install catalog targets. A separate, gated action — never implied.

        The read-only actions (check/list/plan/refresh) stay automatic; apply
        always shows the exact `oaset update apply …` command line and then goes
        through the same permission panel as every other write.
        """
        plan = manager.plan()
        if not plan.targets:
            self.notify_user(UiNotice(
                t("update_nothing_to_apply"), "info", code="update.no_targets",
                hint=t("update_check_first"), source="update"))
            return
        targets = [x for x in preset.replace(",", " ").split() if x]
        if not targets:
            options = [(e.name, f"{e.kind}/{e.name} → {e.version}" + (
                f"  — {e.description}" if e.description else "")) for e in plan.targets]
            choice = await self._pick(t("picker_update_apply"), options)
            if not choice:
                return
            targets = [choice]
        command_line = "oaset update apply " + " ".join(targets)
        if not await self._confirm_command(command_line, UPDATE_APPLY_COMMAND):
            return
        granted = await self.registry.gate.request(
            "update_apply", "write",
            t("update_apply_summary", targets=", ".join(targets)),
            preview=t("update_apply_preview"))
        if granted not in ("allow", "always"):
            self.notify_user(UiNotice(t("update_apply_denied"), "warn",
                                      code="update.apply_denied", source="update"))
            return
        from oaset.update import UpdateError

        try:
            result = await manager.apply(targets=targets)
        except UpdateError as exc:
            self.notify_error(exc, source="update")
            return
        self._report_update(result, source="update.apply")

    async def _update_rollback(self, manager, preset: str) -> None:
        """Roll back a recorded apply. Also separately confirmed and gated."""
        from oaset.update import UpdateError

        target = preset.strip()
        if not target:
            target = await self._run_prompt(
                InlineInput(t("update_rollback_prompt"), t("update_rollback_last"))) or ""
            if not target:
                return
            target = target.strip()
        command_line = "oaset update rollback " + (target or "last")
        if not await self._confirm_command(command_line, UPDATE_ROLLBACK_COMMAND):
            return
        granted = await self.registry.gate.request(
            "update_rollback", "write",
            t("update_rollback_summary", target=target or "last"),
            preview=t("update_apply_preview"))
        if granted not in ("allow", "always"):
            self.notify_user(UiNotice(t("update_rollback_denied"), "warn",
                                      code="update.rollback_denied", source="update"))
            return
        try:
            result = await manager.rollback(target or "last")
        except UpdateError as exc:
            self.notify_error(exc, source="update")
            return
        self._report_update(result, source="update.rollback")

    def cmd_status(self, args: str) -> None:

        from oaset.agent.context import conversation_tokens


        used = conversation_tokens(self.conversation)
        total = self.model_cfg.max_context_size or 0
        pct = (used / total * 100) if total else 0.0
        tools = len(self.registry.tools)
        disabled = len(getattr(self.registry, "disabled", set()) or set())
        # Pad by DISPLAY width, not by character count: "工作目录" is four
        # characters but eight columns, so `:<5` left the Chinese labels
        # one column wider than the English ones and every row after the first
        # was misaligned. rich.cells.cell_len is the same measure the renderer
        # uses.
        from rich.cells import cell_len

        def row(label: str, value: str) -> str:
            pad = " " * max(1, 6 - cell_len(label))
            return f"  {label}{pad}{value}"

        lines = [
            f"[b]{t('status_header')}[/b]",
            row(t("status_session"),
                f"…{self.session.meta.session_id[-12:]}  "
                f"{self.session.meta.title or t('status_untitled')}"),
            row(t("status_model"), f"{self.model_cfg.id}  ({self.model_cfg.provider})"),
            row(t("status_mode"),
                f"{self.mode}   "
                + t("status_network_language", network=self.cfg.network_mode,
                    language=self.cfg.ui_language)),
            row(t("status_context"), f"{pct:.1f}% ({fmt_k(used)}/{fmt_k(total)})"),
            # Which code is this? An editable checkout and a frozen exe can
            # differ by weeks, and "the fix is in but I see the old build" is
            # otherwise very hard to notice.
            row(t("status_source"), escape(describe_source())),
            row(t("status_tools"),
                t("status_tools_detail", active=tools, disabled=disabled,
                  allowed=len(self.cfg.allow_tools))),
            row(t("status_mcp"), t("status_mcp_detail",
                                   count=len(self.mcp.adapters()) if self.mcp else 0)),
            row(t("status_cwd"), _esc(str(self.cwd))),
        ]
        self.chat.add_card("\n".join(lines))

    def cmd_reload(self, args: str) -> None:
        from oaset.config import build_provider_chain, load_config, shell_config
        from oaset.i18n import set_language

        try:
            cfg = load_config()
        except Exception as exc:
            self.notify_error(exc, source="config", text=t("reload_failed", err=exc),
                              hint=t("reload_failed_hint"))
            return
        self.cfg = cfg
        set_language(cfg.ui_language)
        register_themes(self, oaset_home())
        self._custom_themes = set(load_custom_themes(oaset_home()))
        self.theme = normalize_theme(cfg.ui.theme, extra=self._custom_themes)
        # Full runtime rebuild: the old /reload only refreshed cfg/language/theme,
        # leaving the live provider, fallback chain, hooks and shell backend stale.
        # Re-resolve the model first: config may have switched default_model,
        # model capabilities or context limits; keep the old one if it vanished.
        from oaset.config import resolve_model as _resolve_model

        try:
            self.model_cfg = _resolve_model(cfg, self.model_cfg.id)
        except KeyError:
            try:
                self.model_cfg = _resolve_model(cfg, cfg.default_model)
            except KeyError as exc:
                self.notify_error(exc, source="config", text=t("reload_failed", err=exc))
                return
            self.chat.add_notice(
                f"model {self.model_cfg.id} was removed from config; switched to {cfg.default_model}", "warn")
        self._kernel.context_max_tokens = self.model_cfg.max_context_size
        try:
            provider, fallbacks = build_provider_chain(cfg, self.model_cfg)
        except Exception as exc:
            # Fail-safe: the previous provider chain keeps serving the session
            # (including any turn still streaming) instead of being torn down.
            self.notify_user(UiNotice(
                t("reload_provider_failed", err=exc), "warn",
                code="reload.provider_failed", hint=t("reload_failed_hint"),
                source="config"))
        else:
            old_close = getattr(self.provider, "aclose", None)
            if old_close is not None:
                # Never close the client a live turn is streaming through: park
                # the close and let that turn's finally block release it.
                if self._turn.active:
                    previous = self._deferred_provider_close
                    self._deferred_provider_close = self._chain_close(previous, old_close)
                    self.notify_user(UiNotice(
                        t("reload_deferred_close"), "info",
                        code="reload.deferred_close", source="reload"))
                else:
                    with contextlib.suppress(Exception):
                        self.run_worker(old_close(), group="util", exclusive=False)
            self.provider = provider
            self._fallbacks = fallbacks
            self._kernel.provider = provider
            self._kernel.fallbacks = fallbacks
            self._kernel.hooks = HookRunner(cfg.hooks, self.cwd, network_mode=cfg.network_mode)
            if "subagent_runner" in self.registry.ctx.session_state:
                self.registry.ctx.session_state["subagent_runner"] = self._make_subagent_runner(
                    provider, self.model_cfg.max_context_size)
        self.registry.ctx.session_state["shell_config"] = shell_config(cfg)
        self.registry.ctx.network_mode = cfg.network_mode
        self.status_bar.set_model(self.model_cfg.id, self.model_cfg.provider)
        self.chat.add_notice(t("reloaded"), "info")

    def cmd_doctor(self, args: str) -> None:
        import platform
        import sys as _sys

        from oaset.history import _connect, _fts_available

        conn = _connect()
        try:
            fts_ok = _fts_available(conn)
        finally:
            conn.close()

        checks = [
            t("doctor_card_version", version=__version__),
            t("doctor_card_python", python=_sys.version.split()[0],
              system=f"{platform.system()} {platform.release()}"),
            t("doctor_card_config", path=oaset_home() / 'config.toml'),
            t("doctor_card_credentials", count=len(list_credentials())),
            t("doctor_card_fts5", state=(t("doctor_fts_ok") if fts_ok
                                         else t("doctor_fts_missing"))),
            t("doctor_card_tools", active=len(self.registry.tools),
              allowed=len(self.cfg.allow_tools)),
            t("doctor_card_mcp", count=len(self.mcp.adapters()) if self.mcp else 0,
              path=oaset_home() / 'mcp.json'),
            t("doctor_card_network", mode=self.cfg.network_mode),
        ]
        self.chat.add_card("[b]oAset doctor[/b]\n" + "\n".join(checks))

    def cmd_version(self, args: str) -> None:
        import platform
        import sys as _sys

        from oaset.runtime_info import describe_source

        self.chat.add_notice(
            f"oAset CLI {__version__} · python {_sys.version.split()[0]} · "
            f"{platform.system()} {platform.release()}"
            f"\n{describe_source()}",
            "info",
        )

    def cmd_tasks(self, args: str) -> None:
        reg = self.registry.ctx.session_state.get("background_tasks")
        tasks = reg.list(limit=20) if reg else []
        if not tasks:
            self.chat.add_notice(t("no_bg_tasks"), "info")
            return
        self.chat.add_notice(t("bg_tasks_header") + "\n"
                        + "\n".join(task.summary() for task in tasks), "info")

    @work(group="command", exclusive=True)
    async def cmd_agents(self, args: str) -> None:
        """`/agents [name]` lists/loads; bare opens the management surface."""
        sub = args.strip()
        if sub:
            parts = sub.split(None, 1)
            verb = parts[0].lower()
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb == "new":
                await self._agent_create_flow(rest)
                return
            if verb in ("delete", "remove", "rm"):
                await self._agent_delete_flow(rest)
                return
            if verb in ("show", "info"):
                name = rest or await self._agent_name_picker()
                if name:
                    self._agent_show(name)
                return
            if verb == "reload":
                self._agents_reload()
                return
            self._agent_show(sub)
            return

        from oaset.agents import load_agents





        agents = load_agents(self.cwd)
        self._agents_reload(agents)
        rows = [(f"show:{a.name}", self._agent_row(a)) for a in agents]
        rows.append(("__new__", t("agents_menu_new")))
        rows.append(("__usage__", t("agents_usage")))
        choice = await self._pick(t("agents_menu_title"), rows)
        if not choice:
            return
        if choice == "__new__":
            await self._agent_create_flow("")
        elif choice == "__usage__":
            self._agents_usage()
        elif choice.startswith("show:"):
            self._agent_show(choice[5:])

    async def _agent_create_flow(self, preset_name: str) -> None:
        from oaset.agents import create_agent

        name = preset_name or await self._input(t("agents_new_name"))
        if not name:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        description = await self._input(t("agents_new_description"), "")
        if description is None:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        model = await self._input(t("agents_new_model"), "")
        if model is None:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        scope = await self._pick(t("mcp_scope_title"), [
            ("project", t("mcp_scope_project")),
            ("user", t("mcp_scope_user")),
        ])
        if not scope:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        decision = await self.registry.gate.request(
            "agents_create", "write",
            t("agents_create_summary", name=name, scope=scope),
            preview=f"{scope}/agents/{name}.md")
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("agents_denied"), "warn",
                                      code="agents.denied", source="agents"))
            return
        try:
            path = create_agent(self.cwd, name, description, scope=scope, model=model)
        except (ValueError, OSError) as exc:
            self.notify_error(exc, code="agents.create_failed", source="agents")
            return
        self.chat.add_notice(t("agents_created", name=name, path=str(path)), "info")
        self._agents_reload()

    async def _agent_delete_flow(self, preset_name: str) -> None:
        from oaset.agents import delete_agent

        name = preset_name or await self._agent_name_picker()
        if not name:
            return
        from oaset.agents import find_agent



        agent = find_agent(self.cwd, name)
        if agent is None:
            self.chat.add_notice(t("agents_unknown", name=name), "warn")
            return
        decision = await self.registry.gate.request(
            "agents_delete", "danger",
            t("agents_delete_summary", name=name), preview=str(agent.path))
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("agents_denied"), "warn",
                                      code="agents.denied", source="agents"))
            return
        from oaset.agents import find_agent


        agent = find_agent(self.cwd, name)
        if agent is not None and getattr(agent, "scope", "") == "builtin":
            self.chat.add_notice(t("agents_builtin_undeletable", name=name), "warn")
            return
        removed = None
        try:
            removed = delete_agent(self.cwd, name)
        except OSError as exc:
            # a locked .md file is routine on Windows — a notice, not a crash
            self.notify_error(exc, code="agents.delete_failed", source="agents")
            return
        if removed is None:
            self.chat.add_notice(t("agents_unknown", name=name), "warn")
            return
        self.chat.add_notice(t("agents_deleted", name=name, path=str(removed)), "info")
        self._agents_reload()

    def cmd_cron(self, args: str) -> None:
        jobs = cronmod.load_jobs()
        if not jobs:
            self.chat.add_notice(t("no_cron_jobs"), "info")
            return
        lines = "\n".join(
            f"[{j.id[-8:]}] {'✓' if j.enabled else '✗'} {j.name} · {j.schedule} · last {j.last_run or 'never'}"
            for j in jobs
        )
        self.chat.add_notice(t("cron_header") + "\n" + lines, "info")

    @work(group="command", exclusive=True)
    async def cmd_desktop(self, args: str) -> None:
        """List desktop windows through the gated UIA provider (phase 1)."""
        from oaset.computer.desktop import (
            DesktopError,
            GatedDesktopPage,
            UiaDesktopPage,
            _current_backend,
        )

        page = GatedDesktopPage(
            UiaDesktopPage(_current_backend()), self.registry.gate,
            log=lambda s: self.chat.add_notice(s, "info"))
        try:
            windows = await page.list_windows()
        except DesktopError as exc:
            self.chat.add_notice(f"[{exc.code}] {exc}", "warn")
            return
        if not windows:
            self.chat.add_notice(t("desktop_no_windows"), "info")
            return
        lines = [f"[b]{t('desktop_windows_header')}[/b]"]
        for w in windows[:20]:
            rect = w.rect or {}
            lines.append(
                f"· {_esc(w.title[:40]):40} pid={w.pid:<7} "
                f"({rect.get('x', 0)},{rect.get('y', 0)} {rect.get('w', 0)}x{rect.get('h', 0)} "
                f"@{rect.get('scale_factor', 1)}x d{rect.get('display_id', 0)})"
                + (" ●" if w.foreground else ""))
        self.chat.add_card("\n".join(lines))

    @work(group="command", exclusive=True)
    async def cmd_selfmaint(self, args: str) -> None:
        """体验计划: one budget-capped self-maintenance run (check → plan →
        gated model turn → hash-gated apply when opted in).

        The apply decision is never taken here: `SelfMaintenance` re-checks the
        durable config opt-in and raises the same inline permission panel every
        other write goes through.
        """
        if not self.cfg.maintenance.enabled and args.strip() != "enable":
            choice = await self._pick(
                t("selfmaint_disabled_title"),
                [("enable", t("selfmaint_enable_option")),
                 ("cancel", t("cancel"))],
            )
            if choice != "enable":
                return
            self.cfg.maintenance.enabled = True
            from oaset.config import save_config

            save_config(self.cfg)
            self.chat.add_notice(t("selfmaint_enabled_readonly"), "info")
            # The option says "enable (read-only by default)". Without this
            # return the command fell through and immediately spent tokens on a
            # full self-maintenance run — the opposite of what was chosen.
            return
        if args.strip() == "enable":
            self.cfg.maintenance.enabled = True
            from oaset.config import save_config

            save_config(self.cfg)
            self.chat.add_notice(t("selfmaint_enabled"), "info")
            return
        from oaset.maintenance import MaintenanceError, SelfMaintenance

        self.chat.add_notice(t("selfmaint_running"), "info")
        maint = SelfMaintenance(self.cfg, self.cwd, provider=self.provider,
                                gate=self.registry.gate)
        try:
            result = await maint.run()
        except MaintenanceError as exc:
            self.chat.add_notice(
                t("selfmaint_refused", code=exc.code, err=exc)
                + (f"\n{exc.hint}" if exc.hint else ""), "error")
            return
        except asyncio.CancelledError:
            self.chat.add_notice(t("selfmaint_cancelled"), "warn")
            raise
        except Exception as exc:
            self.notify_error(exc, source="maintenance",
                              text=t("selfmaint_failed", err=f"{type(exc).__name__}: {exc}"))
            return
        self.chat.add_card(result.render())

    def cmd_plugins(self, args: str) -> None:
        records = getattr(self, "_plugin_records", None) or []
        if not self._loaded_plugins:
            self.chat.add_notice(t("no_plugins"), "info")
            return
        lines = []
        for record in records:
            manifest = getattr(record, "manifest", None) or {}
            head = f"· {record.name}"
            if manifest:
                head += f"  v{manifest.get('version', '?')}"
                if manifest.get("_invalid"):
                    head += "  " + t("manifest_invalid", err=manifest["_invalid"])
                perms = manifest.get("permissions") or []
                if perms:
                    head += f"  permissions=[{', '.join(perms)}]"
                unknown = manifest.get("_unknown_keys") or []
                if unknown:
                    head += f"  unknown_keys={unknown}"
            tools = ", ".join(record.tools) or "-"
            cmds = ", ".join(record.commands) or "-"
            lines.append(f"{head}\n    tools: {tools} · commands: {cmds}")
        failures = getattr(self, "_plugin_failures", None) or []
        if failures:
            # failed plugins used to be invisible after the one info line
            lines.append("[b]" + t("plugins_failed_header") + "[/b]")
            lines.extend(f"  ✗ {_esc(str(f))}" for f in failures)
        self.chat.add_notice(t("plugins_list", names=", ".join(self._loaded_plugins))
                             + "\n" + "\n".join(lines), "warn" if failures else "info")

    def activity_stream(self, chunk: str) -> None:
        """Live tool output into the running card peek (not just a hidden buffer)."""
        run = self.active_card
        if run is None or not hasattr(run, "result"):
            return
        run.result += chunk
        card = getattr(run, "card", None)
        if card is not None:
            card.append_stream(chunk)
            if "\n" in chunk or "\r" in chunk:
                card.flush()

    async def _warmup_shell(self) -> None:
        """Probe and cache the usable shell off the UI thread; surface fallback."""
        from oaset.utils import detect_shell, shell_diagnosis

        shell = await asyncio.to_thread(detect_shell)
        reason = shell_diagnosis()
        if reason:
            self.notify_user(UiNotice(
                t("shell_fallback", shell=shell, reason=reason),
                level="warn", code="shell.unusable", source="shell"))

    def _drain_background_notices(self) -> None:
        """Surface finished background tasks as notices at the next prompt."""
        if self.registry is None:  # fast-boot window
            return
        reg = self.registry.ctx.session_state.get("background_tasks")
        if reg is None:
            return
        seen: set[str] = getattr(self, "_bg_seen", set())
        for task in reg.list():
            if task.status != "running" and task.id not in seen:
                seen.add(task.id)
                code = f" exit={task.exit_code}" if task.exit_code is not None else ""
                self.chat.add_notice(t("bg_done", task_id=task.id, status=task.status, desc=task.description) + code, "info")

    def cmd_editor(self, args: str) -> None:
        """/editor <command>: the external editor Ctrl+G opens (moved here from
        app.py to respect the app line budget)."""
        import os as _os

        from oaset.config import save_config

        arg = args.strip()
        if not arg:
            current = self.cfg.ui.editor or _os.environ.get("VISUAL") or _os.environ.get("EDITOR") or "(none)"
            self.chat.add_notice(t("editor_current", editor=current), "info")
            return
        self.cfg.ui.editor = arg
        save_config(self.cfg)
        self.chat.add_notice(t("editor_set", editor=arg), "info")

    @work(group="command", exclusive=True)
    async def cmd_reload_plugins(self, args: str) -> None:
        """Hot reload: retract old plugin tools/commands/modules, load fresh.

        Completes the `oaset update apply` loop — newly installed plugins are
        usable without restarting the TUI.
        """
        from oaset.plugins import reload_plugins

        names = reload_plugins(
            self.cwd, self.registry, self.plugin_commands,
            log=lambda t: self.chat.add_notice(t, "info"),
            loaded=self._plugin_records)
        self._loaded_plugins = names
        self.chat.add_notice(t("plugins_reloaded", count=len(names)), "info")

    @work(group="command", exclusive=True)
    async def cmd_skills(self, args: str) -> None:
        """`/skills [name]` loads a skill; bare `/skills` is the full surface."""
        sub = args.strip()
        if sub:
            parts = sub.split(None, 1)
            verb = parts[0].lower()
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb == "new":
                await self._skill_create_flow(rest)
                return
            if verb in ("delete", "remove", "rm"):
                await self._skill_delete_flow(rest)
                return
            if verb in ("show", "info"):
                name = rest or await self._skill_name_picker()
                if name:
                    self._skill_show(name)
                return
            self._skill_load(sub)
            return
        skills = load_skills(self.cwd)
        rows = [(f"load:{s.name}", f"{s.name} - {s.description or '(no description)'}")
                for s in skills]
        rows.append(("__new__", t("skills_menu_new")))
        rows.append(("__reload__", t("skills_menu_reload")))
        if not skills:
            rows.insert(0, ("__none__", t("no_skills")))
        choice = await self._pick(t("skills_menu_title"), rows)
        if not choice or choice == "__none__":
            return
        if choice == "__new__":
            await self._skill_create_flow("")
        elif choice == "__reload__":
            self.cmd_reload_skills("")
        elif choice.startswith("load:"):
            self._skill_load(choice[5:])

    async def _skill_create_flow(self, preset_name: str) -> None:
        """Create a skill skeleton - gated as a write (it writes a file)."""
        from oaset.skills import create_skill

        name = preset_name or await self._input(t("skills_new_name"))
        if not name:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        description = await self._input(t("skills_new_description"), "")
        if description is None:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        scope = await self._pick(t("mcp_scope_title"), [
            ("project", t("mcp_scope_project")),
            ("user", t("mcp_scope_user")),
        ])
        if not scope:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        decision = await self.registry.gate.request(
            "skills_create", "write",
            t("skills_create_summary", name=name, scope=scope),
            preview=f"{scope}/skills/{name}/SKILL.md")
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("skills_denied"), "warn",
                                      code="skills.denied", source="skills"))
            return
        try:
            path = create_skill(self.cwd, name, description, scope=scope)
        except (ValueError, OSError) as exc:
            self.notify_error(exc, code="skills.create_failed", source="skills")
            return
        self.chat.add_notice(t("skills_created", name=name, path=str(path)), "info")
        self.cmd_reload_skills("")

    async def _skill_delete_flow(self, preset_name: str) -> None:
        from oaset.skills import delete_skill

        name = preset_name or await self._skill_name_picker()
        if not name:
            return
        skill = find_skill(self.cwd, name)
        if skill is None:
            self.chat.add_notice(t("skill_not_found", name=name), "warn")
            return
        target = skill.path.parent if skill.path.name == "SKILL.md" else skill.path
        scope_note = (t("skills_delete_scope_dir") if skill.path.name == "SKILL.md" else "")
        decision = await self.registry.gate.request(
            "skills_delete", "danger",
            t("skills_delete_summary", name=name),
            preview="\n".join(p for p in (str(target), scope_note) if p))
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("skills_denied"), "warn",
                                      code="skills.denied", source="skills"))
            return
        try:
            removed = delete_skill(self.cwd, name)
        except ValueError as exc:
            self.notify_error(exc, code="skills.delete_failed", source="skills")
            return
        except OSError as exc:
            self.notify_error(exc, code="skills.delete_failed", source="skills")
            return
        if removed is None:
            self.chat.add_notice(t("skill_not_found", name=name), "warn")
            return
        self.chat.add_notice(t("skills_deleted", name=name, path=str(removed)), "info")
        self.cmd_reload_skills("")

    @work(group="command", exclusive=True)
    async def cmd_settings(self, args: str) -> None:
        """`/settings` — configure network, tools, hooks and rules from the TUI.

        Everything here used to require hand-editing config.toml; each action
        writes through the same save_config path and reports what changed.
        """
        sub = args.strip().lower()
        if sub == "network":
            await self._settings_network()
            return
        if sub in ("tools", "tool"):
            self.cmd_tools_toggle("")
            return
        if sub in ("hooks", "hook"):
            await self._settings_hooks()
            return
        if sub in ("rules", "permissions"):
            self._settings_rules()
            return
        if sub in ("show", "config"):
            self.cmd_config("")
            return
        choice = await self._pick(t("settings_menu_title"), [
            ("network", t("settings_network")),
            ("tools", t("settings_tools")),
            ("hooks", t("settings_hooks")),
            ("rules", t("settings_rules")),
            ("show", t("settings_show")),
        ])
        if choice == "network":
            await self._settings_network()
        elif choice == "tools":
            self.cmd_tools_toggle("")
        elif choice == "hooks":
            await self._settings_hooks()
        elif choice == "rules":
            self._settings_rules()
        elif choice == "show":
            self.cmd_config("")

    async def _settings_network(self) -> None:
        """Pick the egress policy; the chosen mode is applied and persisted."""
        from oaset.network import normalize_mode

        current = normalize_mode(self.cfg.network_mode)
        choice = await self._pick(t("network_title"), [
            ("local_only", t("network_local_only")),
            ("pull_only", t("network_pull_only")),
            ("full", t("network_full")),
        ])
        if not choice:
            return
        mode = normalize_mode(choice)
        if mode == current:
            self.chat.add_notice(t("network_set", mode=mode), "info")
            return
        # Egress policy is a security boundary, so changing it goes through the
        # same gate as the other privileged operations in this menu. `full` is
        # the dangerous direction: it is what enables pushing content to third
        # parties and remote shell backends, so it is confirmed as such.
        decision = await self.registry.gate.request(
            "settings_network", "danger" if mode == "full" else "write",
            t("settings_network_summary", old=current, new=mode))
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("settings_network_denied"), "warn",
                                      code="settings.network_denied",
                                      source="settings"))
            return
        self.cfg.network_mode = mode
        self.registry.ctx.network_mode = mode
        try:
            save_config(self.cfg)
        except Exception as exc:
            self.notify_error(exc, code="config.write_failed", source="config")
            return
        self.chat.add_notice(t("network_set", mode=mode), "info")

    async def _settings_hooks(self) -> None:
        """Edit one lifecycle hook action (shell command or HTTP endpoint)."""
        from oaset.hooks import EVENTS

        event = await self._pick(t("settings_hooks_title"), [
            (name, f"{name}  ->  {self.cfg.hooks.get(name, '(unset)')[:48]}")
            for name in EVENTS
        ])
        if not event:
            return
        action = await self._input(
            t("settings_hook_edit", event=event),
            self.cfg.hooks.get(event, ""))
        if action is None:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        new_action = action.strip()
        if new_action == (self.cfg.hooks.get(event, "") or "").strip():
            self.chat.add_notice(t("settings_hook_unchanged", event=event), "info")
            return
        # A hook is a shell command (or HTTP call) the kernel runs on every
        # matching lifecycle event: persisting one is persistent code execution,
        # so it is confirmed at `danger` rather than written silently. Creating
        # a sub-agent file is gated too — this cannot be the softer path.
        decision = await self.registry.gate.request(
            "settings_hooks", "danger",
            t("settings_hook_summary", event=event),
            preview=new_action or t("settings_hook_preview_clear"))
        if str(decision).lower() not in ("allow", "always"):
            self.notify_user(UiNotice(t("settings_hook_denied", event=event), "warn",
                                      code="settings.hook_denied", source="settings"))
            return
        if new_action:
            self.cfg.hooks[event] = new_action
            message = t("settings_hook_set", event=event)
        else:
            self.cfg.hooks.pop(event, None)
            message = t("settings_hook_cleared", event=event)
        try:
            save_config(self.cfg)
        except Exception as exc:
            self.notify_error(exc, code="config.write_failed", source="config")
            return
        self.chat.add_notice(f"{message} · {t('settings_saved')}", "info")

    def _settings_rules(self) -> None:
        """Read-only view of the parsed permission rules."""
        rules = getattr(self.cfg, "permission_rules", []) or []
        if not rules:
            self.chat.add_notice(t("settings_rules_empty"), "info")
            return
        from rich.markup import escape as _esc

        lines = [f"[b]{t('settings_rules_title')}[/b]"]
        for rule in rules:
            # patterns are user-defined and land in a markup card
            lines.append(f"  {rule.decision:<5} {_esc(str(rule.pattern))}"
                         f"  [dim]({_esc(str(rule.scope))})[/dim]")
        self.chat.add_card("\n".join(lines))
