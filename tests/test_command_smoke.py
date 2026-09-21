"""Every registered command must actually run: no implemented-but-broken.

For each command in the registry: run it bare, let its prompt/viewer/gate
appear, Escape out of whatever is still open, and require the app to still
be alive. Commands that genuinely exit or reach the network are skipped
with a stated reason — the skip list is short on purpose so a new command
that cannot survive this loop is a finding, not a fixture problem.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.commands import COMMANDS

SKIP = {
    "exit": "would quit the app under test",
    "quit": "would quit the app under test",
    "update": "network: fetches the release catalog",
    "upgrade": "network: downloads and installs a build",
}


async def test_every_command_smokes(workspace):
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        covered = []
        for cmd in COMMANDS:
            if cmd.name in SKIP:
                continue
            covered.append(cmd.name)
            app.submit_text(f"/{cmd.name}")
            await pilot.pause(0.25)
            await pilot.press("escape")   # dismiss any prompt/viewer/gate
            await pilot.pause(0.15)
            assert app.is_running, f"/{cmd.name} killed the app"

        # every non-skipped command really went through the loop
        expected = [c.name for c in COMMANDS if c.name not in SKIP]
        assert covered == expected
        assert len(covered) >= 30, "the registry shrunk unexpectedly"


async def test_key_commands_have_visible_effects(workspace):
    """Beyond 'did not crash': the commands a user actually leans on must
    move the app into the state they promise (v2 of the no-hollow-features
    audit — implementation is only half the contract)."""
    from oaset.tui.widgets.chat import WelcomePanel

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)

        # /language en — the welcome page re-renders in English
        app.submit_text("/language en")
        await pilot.pause(0.4)
        assert "local-first" in app.chat.query(WelcomePanel).first().raw_text

        # /theme oaset-light — the live theme flips
        app.submit_text("/theme oaset-light")
        await pilot.pause(0.3)
        assert app.theme == "oaset-light"

        # /density compact — the density class lands on the app
        app.submit_text("/density compact")
        await pilot.pause(0.3)
        assert app.has_class("compact")

        # /mode plan — the mode reaches the status bar
        app.submit_text("/mode plan")
        await pilot.pause(0.3)
        assert app.status_bar._mode == "plan"

        # /errors with nothing pinned — answers instead of crashing
        app.submit_text("/errors")
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.is_running
