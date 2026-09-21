"""Fresh-install first-run: ONE prompt at a time, screen never jams.

Repro of the 2026-09-15 report: after a factory reset (no key + language not
chosen), the language picker and the auto-login form were scheduled together,
raced for the single prompt slot, the superseded picker leaked mounted, and
arrow keys stopped selecting (they went to whichever prompt won the race).

The auto-login form has since been removed entirely (startup stays quiet; the
welcome page badges a keyless model), so this file also pins that contract:
after the language choice the prompt slot frees and nothing else pops up.

Driving note: after an ``Input`` is focused, ``pilot.pause`` can wait forever
for screen-idle (cursor blink keeps re-queuing), so this test drives the app
with raw sleeps + ``post_message`` — which is also the honest way to assert
the message pump is actually alive.
"""

from __future__ import annotations

import asyncio

from textual import events

from oaset.config import default_config
from oaset.tui.app import OasetApp
from oaset.tui.widgets.inline import InlineInput, InlinePicker


async def _spin(predicate, timeout=6.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        if predicate():
            return True
    return False


def test_doctor_bootstraps_config_on_a_fresh_home(isolated_home, capsys, monkeypatch):
    """A downloaded-from-GitHub install runs doctor first: it must CREATE the
    default config (fresh installs used to see 'config-file ✗' as a critical
    failure) and point at the setup path instead of failing silently."""
    import httpx

    from oaset.cli import main
    from oaset.config import config_path

    # doctor probes the default model's endpoint on purpose; this test is about
    # the config bootstrap, so the probe answers locally (a live one cost up to
    # 4s and made the result depend on the network)
    monkeypatch.setattr(httpx, "head", lambda *a, **k: httpx.Response(200))
    assert not config_path().exists()
    main(["doctor"])
    assert config_path().exists(), "doctor must bootstrap the default config"
    out = capsys.readouterr().out
    assert "config-file" in out
    assert "setup" in out  # the missing-key hint names the wizard


async def test_first_run_is_one_prompt_then_quiet(workspace):
    cfg = default_config()
    cfg.language_chosen = False
    app = OasetApp(cfg=cfg, cwd=workspace)
    async with app.run_test(size=(100, 24)) as pilot:
        # Phase 1: the language picker comes up alone and owns the keyboard.
        await _spin(lambda: app._active_prompt is not None)
        pickers = list(app.query(InlinePicker))
        assert pickers, "language picker must come up first"
        assert isinstance(app._active_prompt, InlinePicker)
        assert app.focused is pickers[0]

        # ↑/↓ move the selection (the reported-dead path).
        await pilot.press("down")
        assert pickers[0].index == 1

        # Phase 2: after the language, ONE skippable setup offer (fresh
        # install reached from a downloaded repo must be able to finish
        # configuration without knowing slash commands). The offer can mount
        # before the language picker's unmount is observable, so wait for the
        # offer itself rather than for an empty slot.
        def _offers():
            return [p for p in app.query(InlinePicker)
                    if [v for v, _l in p.options] == ["now", "later"]]

        pickers[0].future.set_result("en")
        assert await _spin(lambda: bool(_offers()))
        assert app.cfg.ui_language == "en" and app.cfg.language_chosen
        offer = _offers()[-1]

        # …and skipping it ends the prompts: no login form, no error popup.
        offer.future.set_result("later")
        assert await _spin(lambda: app._active_prompt is None)
        assert not list(app.query(InlineInput))

        # The screen still processes events: keys land in the main input.
        main_input = app.query_one("#input")
        assert await _spin(lambda: app.focused is main_input)
        for ch in "abc":
            app.post_message(events.Key(ch, ch))
        assert await _spin(lambda: main_input.text == "abc")

        # A click into the app must not kill it (reported exit-on-click).
        app.post_message(events.MouseDown(
            None, 50, 12, 0, 0, 1, False, False, False, screen_x=50, screen_y=12))
        app.post_message(events.MouseUp(
            None, 50, 12, 0, 0, 1, False, False, False, screen_x=50, screen_y=12))
        await asyncio.sleep(0.3)
        assert app.is_running
        assert app._active_prompt is None
