"""Community plugin market UI (/market).

Four tabs like DeepSeek's market dialog: 发现 (discover) / 可安装
(installable) / 已安装 (installed) / 来源 (sources), one active source at a
time. Browsing and installing always go through the active source; installs
reuse the Update Center's policy/sha256/transaction machinery, so local_only
refuses and every artifact is checksum-verified before it lands in
~/.oaset/plugins.
"""

from __future__ import annotations

from rich.markup import escape as _esc
from textual import work

from oaset.i18n import t
from oaset.market import MarketManager
from oaset.update import UpdateError


class MarketUiMixin:
    """Community plugin market UI (/market)."""

    def _market_manager(self) -> MarketManager:
        return MarketManager(self.cfg, fetch=getattr(self, "_market_fetch", None))

    def _market_overview(self, entries_count: int | None = None) -> str:
        market = self._market_manager()
        active = market.active()
        installed = market.installed()
        counts = (
            f"{t('mkt_tab_discover')} {'·' if entries_count is None else entries_count} · "
            f"{t('mkt_tab_installed')} {len(installed)} · "
            f"{t('mkt_tab_sources')} {len(market.sources())}"
        )
        lines = [
            f"[b]{t('mkt_title')}[/b]",
            f"[dim]{t('mkt_subtitle')}[/dim]",
            "",
            counts,
            f"{t('mkt_active_source')}: [b]{active.name}[/b]  {active.url}",
        ]
        return "\n".join(lines)

    @work(group="market")
    async def cmd_market(self, args: str) -> None:
        """`/market` — community plugin market (discover/install/installed/sources)."""
        sub = args.strip().lower()
        if sub == "sources":
            await self._market_sources()
            return
        if sub == "installed":
            await self._market_installed()
            return
        self.chat.add_card(self._market_overview())
        choice = await self._pick(t("mkt_pick_tab"), [
            ("discover", t("mkt_tab_discover")),
            ("installable", t("mkt_tab_installable")),
            ("installed", t("mkt_tab_installed")),
            ("sources", t("mkt_tab_sources")),
        ])
        if choice == "discover":
            await self._market_browse(installable_only=False)
        elif choice == "installable":
            await self._market_browse(installable_only=True)
        elif choice == "installed":
            await self._market_installed()
        elif choice == "sources":
            await self._market_sources()

    async def _market_browse(self, *, installable_only: bool) -> None:
        market = self._market_manager()
        self.chat.add_notice(t("mkt_fetching"), "info")
        try:
            entries, notes = await market.discover()
        except UpdateError as exc:
            self.notify_error(exc, code=exc.code, source="market")
            return
        for note in notes:
            if note.startswith("network_blocked"):
                self.notify_error(UpdateError("network_blocked", note), code="network_blocked",
                                  source="market")
                return
            # other refresh failures fall back to the cache silently — the
            # user must know the catalog they see may be stale
            self.chat.add_notice(t("market_stale_note", note=note), "warn")
        if installable_only:
            entries = market.installable(entries)
        if not entries:
            self.chat.add_notice(
                t("mkt_empty_installable" if installable_only else "mkt_empty_discover"), "info")
            return
        options = [
            (e.name, f"{e.name} v{e.version} — {e.description[:60]}")
            for e in entries
        ]
        name = await self._pick(
            t("mkt_pick_installable" if installable_only else "mkt_pick_install"), options)
        if not name:
            return
        entry = next(e for e in entries if e.name == name)
        confirmed = await self._pick(
            t("mkt_confirm_install", name=name, version=entry.version, source=market.active().name),
            [("yes", t("wizard_save")), ("no", t("wizard_cancel"))],
        )
        if confirmed != "yes":
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        await self._market_install(name)

    async def _market_install(self, name: str) -> None:
        market = self._market_manager()
        try:
            await market.install(name)
        except UpdateError as exc:
            self.notify_error(exc, code=exc.code, source="market")
            return
        self.chat.add_notice(t("mkt_installed_done", name=name), "info")

    async def _market_installed(self) -> None:
        market = self._market_manager()
        installed = market.installed()
        if not installed:
            self.chat.add_notice(t("mkt_nothing_installed"), "info")
            return
        options = []
        for item in installed:
            version = f" v{item['version']}" if item["version"] else ""
            perm = f"  [{', '.join(item['permissions'])}]" if item["permissions"] else ""
            options.append((item["name"], f"{item['name']}{version}{perm}"))
        name = await self._pick(t("mkt_pick_manage"), options)
        if not name:
            return
        confirmed = await self._pick(
            t("mkt_confirm_uninstall", name=name),
            [("yes", t("wizard_save")), ("no", t("wizard_cancel"))],
        )
        if confirmed != "yes":
            self.chat.add_notice(t("wizard_cancelled"), "info")
            return
        try:
            import asyncio as _aio

            await _aio.to_thread(market.uninstall, name)  # directory removal off the loop
        except UpdateError as exc:
            self.notify_error(exc, code=exc.code, source="market")
            return
        self.chat.add_notice(t("mkt_uninstalled", name=name), "info")

    async def _market_sources(self) -> None:
        market = self._market_manager()
        sources = market.sources()
        active = market.active()
        self.chat.add_card("\n".join(
            [f"[b]{t('mkt_tab_sources')}[/b]  ({t('mkt_active_source')}: {_esc(active.name)})"]
            + [f"{'●' if s.name == active.name else '○'} {_esc(s.name)}  {_esc(s.url)}" for s in sources]
        ))
        action = await self._pick(t("mkt_pick_source_action"), [
            ("use", t("mkt_source_use")),
            ("add", t("mkt_source_add")),
            ("remove", t("mkt_source_remove")),
        ])
        if action == "use":
            options = [(s.name, f"{'●' if s.name == active.name else '○'} {s.name}") for s in sources]
            name = await self._pick(t("mkt_source_use"), options)
            if not name:
                return
            market.use_source(name)
            self.chat.add_notice(t("mkt_source_now_active", name=name), "info")
        elif action == "add":
            url = await self._input(t("mkt_source_add"), "https://")
            if not url:
                return
            name = await self._input(t("mkt_source_name"), "")
            if not name:
                return
            try:
                market.add_source(name, url)
            except UpdateError as exc:
                self.notify_error(exc, code=exc.code, source="market")
                return
            self.chat.add_notice(t("mkt_source_added", name=name), "info")
        elif action == "remove":
            removable = [(s.name, s.name) for s in sources if s.name != "official"]
            if not removable:
                self.chat.add_notice(t("mkt_unknown", name="-"), "info")
                return
            name = await self._pick(t("mkt_source_remove"), removable)
            if not name:
                return
            market.remove_source(name)
            self.chat.add_notice(t("mkt_source_removed", name=name), "info")
