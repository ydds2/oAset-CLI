"""User-perspective command UX: typos, aliases, destructive gates, feedback.

These pin the user-perspective command UX findings (docs/PLAN.zh-CN.md) so the next
command added cannot silently regress them:

- a typo answers in the user's language and names the closest real command
- aliases are declared in the registry (so /help lists them) and resolve
- a destructive command given its argument confirms before acting
- commands that change state visibly say so (/new, /clear)
- a crashed command surfaces instead of vanishing into a fire-and-forget task
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers.mock import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.commands import alias_map
from oaset.tui.widgets.chat import NoticeCard
from oaset.tui.widgets.inline import InlinePromptBase
from oaset.tui.widgets.palette import InlineCommandConfirm


def make_app(workspace):
    cfg = default_config()
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


def _notices(app) -> list[str]:
    return [n.raw_text for n in app.chat.query(NoticeCard)]


def test_aliases_are_declared_in_the_registry() -> None:
    """One source of truth: an alias exists exactly when a command declares it."""
    aliases = alias_map()
    assert aliases.get("?") == "help" and aliases.get("h") == "help"
    assert aliases.get("quit") == "exit" and aliases.get("q") == "exit"
    assert aliases.get("md") == "model"
    assert aliases.get("resume") == "sessions"
    assert aliases.get("cls") == "clear"


def test_alias_appears_in_help_listing() -> None:
    """The old hidden table made /quit work but invisible; declared ones show."""
    from oaset.tui.commands import COMMANDS

    exit_cmd = next(c for c in COMMANDS if c.name == "exit")
    assert "quit" in exit_cmd.aliases


async def test_unknown_command_suggests_the_closest_match(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/modle")
        await pilot.pause(0.3)
        text = " ".join(_notices(app))
        assert "modle" in text, text
        assert "model" in text, "a near miss must name the real command"
        # and it is localized (product default is zh in tests)
        assert "未知命令" in text, text


async def test_unknown_command_without_a_match_offers_help(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/zzzzqqqq")
        await pilot.pause(0.3)
        text = " ".join(_notices(app))
        assert "zzzzqqqq" in text and "/help" in text, text


async def test_alias_reaches_the_real_command(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = len(list(app.chat.query("Static.chat-card")))
        app.submit_text("/h")  # alias for help
        await pilot.pause(0.4)
        assert len(list(app.chat.query("Static.chat-card"))) > before, \
            "/h must run /help"


async def test_destructive_command_with_argument_confirms_first(workspace):
    """`/session-delete <id>` acts immediately, so it must confirm first."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/session-delete some-session-id")
        await pilot.pause(0.4)
        assert list(app.query(InlineCommandConfirm)), \
            "destructive command with an argument must show the confirm page"
        # cancelling must leave everything untouched
        app.query(InlineCommandConfirm).first()._resolve(None)
        await pilot.pause(0.3)
        assert not list(app.query(InlineCommandConfirm))


async def test_destructive_command_without_argument_uses_its_own_picker(workspace):
    """No double confirmation: the picker (which checkpoint/which session) is it."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/undo")
        await pilot.pause(0.4)
        assert not list(app.query(InlineCommandConfirm)), \
            "no-arg /undo already asks via its picker; add no second page"
        for prompt in list(app.query(InlinePromptBase)):
            prompt._resolve(None)
            await pilot.pause(0.1)


async def test_new_and_clear_report_what_happened(workspace):
    """/new and /clear used to change state with zero user-visible output."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/new")
        await pilot.pause(0.3)
        assert any("新会话" in n for n in _notices(app)), _notices(app)

        app.submit_text("/clear")
        await pilot.pause(0.3)
        assert any("清空" in n for n in _notices(app)), _notices(app)


async def test_crashed_command_surfaces_instead_of_vanishing(workspace):
    """A raising handler must produce a notice, not silence."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)

        def boom(_args):
            raise RuntimeError("simulated command crash")

        app.cmd_help = boom  # type: ignore[method-assign]
        app.submit_text("/help")
        await pilot.pause(0.4)
        text = " ".join(_notices(app))
        assert "crashed" in text or "simulated command crash" in text or "抛错" in text, text


async def test_palette_confirmation_is_not_asked_twice(workspace):
    """The palette showed the page; the dispatcher must not stack another."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        from oaset.tui import commands

        await commands.execute(app, "/session-delete some-id", confirmed=True)
        await pilot.pause(0.3)
        assert not list(app.query(InlineCommandConfirm)), \
            "an already-confirmed dispatch must not ask again"
        for prompt in list(app.query(InlinePromptBase)):
            prompt._resolve(None)
            await pilot.pause(0.1)


async def test_export_writes_into_the_session_workspace(workspace, tmp_path, monkeypatch):
    """/export used to resolve against the process CWD (it landed in the repo root).

    Reproduced while auditing commands: the sweep exported into the checkout
    because the app's workspace and the process CWD differ.
    """
    app = make_app(workspace)
    elsewhere = tmp_path / "launched-from"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # pretend the TUI was started from another dir
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/export")
        await pilot.pause(0.4)
        exported = list(workspace.glob("oaset-session-*.md"))
        assert exported, f"export must land in the session workspace {workspace}"
        assert not list(elsewhere.glob("oaset-session-*.md")), \
            "export must not use the process working directory"
