"""MCP server admin UI (P4-1 split from app.py).

Methods move verbatim; they keep working through `self` (OasetApp
inherits this mixin). Imports here cover what the moved bodies use;
anything else is imported locally inside a method, as before.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from textual import work

from oaset.i18n import t
from oaset.tui.notice import UiNotice
from oaset.utils import oaset_home


def _split_args(raw: str) -> list[str]:
    """Quote-aware argv split that survives Windows paths.

    `--config "C:\\Program Files\\app.json"` is ONE argument, not four —
    naive .split() made quoted paths unenterable. shlex is not used on
    purpose: posix=True eats backslashes, posix=False keeps the quotes.
    """
    import re

    parts = re.findall(r'"[^"]*"|\S+', raw or "")
    out: list[str] = []
    for part in parts:
        if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
            out.append(part[1:-1])
        else:
            out.append(part)
    return out


def _parse_tool_filter(raw: str) -> tuple[list[str], list[str]]:
    """`+name` enables, `-name` disables, a bare token ENABLES.

    Bare tokens used to be dropped silently while the save still reported
    success — the user believed the tool surface was narrowed when nothing
    had been written at all.
    """
    plus: list[str] = []
    minus: list[str] = []
    for token in raw.split():
        if token.startswith("+"):
            plus.append(token[1:])
        elif token.startswith("-"):
            minus.append(token[1:])
        else:
            plus.append(token)
    return plus, minus


class McpAdminUiMixin:
    """MCP server admin UI (P4-1 split from app.py)."""

    def _attach_sampling_bridge(self) -> None:
        """Let MCP servers request gated completions, user input and the
        workspace root through injected handlers."""
        from oaset.mcp.bridges import make_elicitation_bridge, make_roots_handler
        from oaset.mcp.sampling import make_sampling_bridge

        gate = self.registry.gate

        def log(s: str) -> None:
            self.chat.add_notice(s, "info")
        sampling = make_sampling_bridge(self.provider, gate, log=log)
        elicitation = make_elicitation_bridge(gate, self._elicit_field, log=log)
        extra_roots = self.registry.ctx.session_state.get("extra_roots")
        roots = make_roots_handler(gate, self.cwd, extra_roots=extra_roots, log=log)
        if self.mcp is None:
            return
        for conn in self.mcp.connections.values():
            conn.sampling_handler = sampling
            conn.elicitation_handler = elicitation
            conn.roots_handler = roots

    @work(group="mcp")
    async def cmd_reload_mcp(self, args: str) -> None:
        await self._reload_mcp_now()

    async def _reload_mcp_now(self) -> None:
        # Drain first: restarting servers under a streaming turn would yank
        # tools out from under in-flight calls. Cancel + wait gives a definite
        # "the old turn is no longer writing" answer; a timeout refuses.
        if self._turn.active:
            self.notify_user(UiNotice(
                t("mcp_reload_draining"), "warn", code="mcp.reload_draining",
                hint=t("mcp_reload_draining_hint"), source="mcp"))
        drained = await self.drain_turn()
        if not drained:
            self.notify_user(UiNotice(
                t("mcp_reload_busy"), "warn", code="mcp.reload_busy",
                hint=t("mcp_reload_busy_hint"), source="mcp"))
            return
        if self.mcp is not None:
            with contextlib.suppress(Exception):
                await self.mcp.shutdown()
        # drop tools owned by the OLD manager first: without this, servers/tools
        # removed from mcp.json kept pointing at closed connections forever
        for name in getattr(self, "_mcp_tool_names", set()):
            self.registry.remove_tool(name)
        from oaset.mcp import McpManager

        self.mcp: McpManager | None = McpManager(oaset_home(), self.cwd, network_mode=self.cfg.network_mode)
        await self.mcp.start_all()
        adapters = self.mcp.adapters()
        self._mcp_tool_names = set()
        for adapter in adapters:
            self.registry.add_tool(adapter)
            self._mcp_tool_names.add(adapter.name)
        self._attach_sampling_bridge()
        self.chat.add_notice(t("mcp_reloaded") + "\n" + (self.mcp.status_line() if self.mcp is not None else t("mcp_not_started")), "info" if adapters else "warn")

    async def cmd_roots(self, args: str) -> None:
        """Multi-root MCP roots/list: add, remove or clear extra roots."""
        state = self.registry.ctx.session_state
        extra: list[Path] = list(state.get("extra_roots") or [])
        parts = args.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "add":
            if not rest:
                rest = (await self.prompt_missing_arg("roots")) or ""
                if not rest:
                    return
            from oaset.utils import expand_path

            path = Path(expand_path(rest, self.cwd)).resolve()
            if not path.is_dir():
                self.chat.add_notice(t("roots_not_dir", path=str(path)), "warn")
                return
            known = {str(self.cwd.resolve())} | {str(p.resolve()) for p in extra}
            if str(path) in known:
                self.chat.add_notice(t("roots_duplicate", path=str(path)), "warn")
                return
            extra.append(path)
            state["extra_roots"] = extra
            self._attach_sampling_bridge()
            self.chat.add_notice(t("roots_added", path=str(path)), "info")
            return

        if sub == "remove":
            if not rest.isdigit():
                self.chat.add_notice(t("roots_remove_usage"), "warn")
                return
            idx = int(rest)
            if not extra or idx < 1 or idx > len(extra):
                self.chat.add_notice(t("roots_bad_index", n=idx, max=len(extra)), "warn")
                return
            removed = extra.pop(idx - 1)
            state["extra_roots"] = extra
            self._attach_sampling_bridge()
            self.chat.add_notice(t("roots_removed", path=str(removed)), "info")
            return

        if sub == "clear":
            state["extra_roots"] = []
            self._attach_sampling_bridge()
            self.chat.add_notice(t("roots_cleared"), "info")
            return

        from rich.markup import escape as _esc

        lines = [f"[b]{t('roots_header')}[/b]",
                 f"  · {_esc(str(self.cwd.resolve()))}  ({t('roots_workspace')})"]
        for i, root in enumerate(extra, start=1):
            lines.append(f"  {i}. {_esc(str(root))}")
        self.chat.add_card("\n".join(lines))

    @work(group="mcp")
    async def cmd_mcp(self, args: str) -> None:
        """`/mcp` — status by default, full management surface with no args."""
        sub = args.strip().lower()
        if sub == "list":
            self._mcp_list()
            return
        if sub in ("add", "new"):
            await self._mcp_add_flow()
            return
        if sub == "remove":
            await self._mcp_remove_flow()
            return
        if sub == "reload":
            await self._reload_mcp_now()
            return
        if sub == "roots":
            await self._roots_menu()
            return
        if sub in ("enable", "disable"):
            await self._mcp_toggle_flow(sub == "enable")
            return
        if sub:
            self.chat.add_notice((self.mcp.status_line() if self.mcp is not None else t("mcp_not_started")), "info")
            return
        choice = await self._pick(t("mcp_menu_title"), [
            ("list", t("mcp_menu_list")),
            ("add", t("mcp_menu_add")),
            ("toggle", t("mcp_menu_toggle")),
            ("remove", t("mcp_menu_remove")),
            ("reload", t("mcp_menu_reload")),
            ("roots", t("mcp_menu_roots")),
        ])
        if choice == "list":
            self._mcp_list()
        elif choice == "add":
            await self._mcp_add_flow()
        elif choice == "toggle":
            await self._mcp_toggle_flow(None)
        elif choice == "remove":
            await self._mcp_remove_flow()
        elif choice == "reload":
            await self._reload_mcp_now()
        elif choice == "roots":
            await self._roots_menu()

    async def _mcp_add_flow(self) -> None:
        from oaset.tui.mcp_admin import McpAdminError, add_server, mcp_path, server_entry

        scope = await self._mcp_scope()
        if not scope:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        name = await self._input(t("mcp_add_name"))
        if not name:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        transport = await self._pick(t("mcp_add_transport"), [
            ("stdio", t("mcp_transport_stdio")),
            ("http", t("mcp_transport_http")),
        ])
        if not transport:
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        try:
            if transport == "http":
                url = await self._input(t("mcp_add_url"), "https://")
                if url is None:
                    return
                entry = server_entry(url=url)
            else:
                command = await self._input(t("mcp_add_command"), "npx")
                if command is None:
                    return
                raw_args = await self._input(t("mcp_add_args"), "")
                if raw_args is None:
                    return
                entry = server_entry(command=command, args=_split_args(raw_args))
            filt = await self._input(t("mcp_add_tools"), "")
            if filt:
                plus, minus = _parse_tool_filter(filt)
                if plus:
                    entry["enabledTools"] = plus
                if minus:
                    entry["disabledTools"] = minus
            add_server(mcp_path(oaset_home(), self.cwd, scope), name, entry)
        except McpAdminError as exc:
            self.notify_error(exc, code=exc.code, source="mcp")
            return
        self.chat.add_notice(t("mcp_saved", name=name, scope=scope), "info")
        await self._mcp_apply_prompt()

    async def _mcp_toggle_flow(self, enable: bool | None) -> None:
        from oaset.tui.mcp_admin import (
            McpAdminError,
            list_servers,
            mcp_path,
            set_enabled,
        )

        rows = list_servers(oaset_home(), self.cwd)
        if not rows:
            self.chat.add_notice(t("mcp_empty"), "info")
            return
        choice = await self._pick(
            t("mcp_menu_toggle"),
            [(f"{row.scope}:{row.name}",
              f"{'●' if row.enabled else '○'} {row.name} [{row.transport}] ({row.scope})")
             for row in rows])
        if not choice:
            return
        scope, _, name = choice.partition(":")
        row = next(r for r in rows if r.name == name and r.scope == scope)
        target_enabled = (not row.enabled) if enable is None else enable
        try:
            set_enabled(mcp_path(oaset_home(), self.cwd, scope), name, target_enabled)
        except McpAdminError as exc:
            self.notify_error(exc, code=exc.code, source="mcp")
            return
        self.chat.add_notice(
            t("mcp_enabled_msg" if target_enabled else "mcp_disabled_msg", name=name), "info")
        await self._mcp_apply_prompt()

    async def _mcp_remove_flow(self) -> None:
        from oaset.tui.mcp_admin import (
            McpAdminError,
            list_servers,
            mcp_path,
            remove_server,
        )

        rows = list_servers(oaset_home(), self.cwd)
        if not rows:
            self.chat.add_notice(t("mcp_empty"), "info")
            return
        choice = await self._pick(
            t("mcp_menu_remove"),
            [(f"{row.scope}:{row.name}", f"{row.name} [{row.transport}] ({row.scope})")
             for row in rows])
        if not choice:
            return
        scope, _, name = choice.partition(":")
        confirmed = await self._pick(f"{t('mcp_menu_remove')}: {name}", [
            ("yes", t("wizard_save")),
            ("no", t("wizard_cancel")),
        ])
        if confirmed != "yes":
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        try:
            remove_server(mcp_path(oaset_home(), self.cwd, scope), name)
        except McpAdminError as exc:
            self.notify_error(exc, code=exc.code, source="mcp")
            return
        self.chat.add_notice(t("mcp_removed", name=name, scope=scope), "info")
        await self._mcp_apply_prompt()
