"""Growth features (2026-09-14): cost transparency, notifications,
shift-drag native selection, /context."""

from __future__ import annotations

import io

from oaset.config import default_config, load_config, save_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp


def make_app(workspace, cfg=None, turns=None) -> OasetApp:
    cfg = cfg or default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider(turns or [MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


def test_pricing_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    cfg = load_config()
    cfg.models["mock/mock-echo"].price_in = 0.5
    cfg.models["mock/mock-echo"].price_out = 1.5
    save_config(cfg)
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "price_in" in text and "price_out" in text
    again = load_config().models["mock/mock-echo"]
    assert again.price_in == 0.5 and again.price_out == 1.5
    # the estimator: 1M in + 1M out at 0.5/1.5 -> $2.0
    assert abs(again.cost(1_000_000, 1_000_000) - 2.0) < 1e-9
    assert default_config().models["mock/mock-echo"].cost(999, 999) is None, (
        "unpriced models show nothing, we never guess")


async def test_turn_meta_shows_cost_only_when_priced(workspace):
    cfg = default_config()
    cfg.models["mock/mock-echo"].price_in = 1.0
    cfg.models["mock/mock-echo"].price_out = 2.0
    turns = [MockTurn(content_chunks=["done"], usage={
        "prompt_tokens": 100_000, "completion_tokens": 50_000, "total_tokens": 150_000})]
    app = make_app(workspace, cfg=cfg, turns=turns)  # 0.1*1 + 0.05*2 = $0.20
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("hello")
        await pilot.pause(1.5)
        from oaset.tui.widgets.chat import AssistantTurn

        meta = app.chat.query(AssistantTurn)[-1].meta_text()
        assert "≈$0.20" in meta, meta

    # unpriced: no cost text at all
    app2 = make_app(workspace, turns=turns)
    async with app2.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app2.submit_text("hello")
        await pilot.pause(1.5)
        from oaset.tui.widgets.chat import AssistantTurn

        assert "$" not in app2.chat.query(AssistantTurn)[-1].meta_text()


async def test_notify_bell_and_desktop(workspace):
    from oaset.providers import MockProvider

    ring: list[int] = []
    cfg = default_config()
    cfg.ui.notify = "bell"
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider(
        [MockTurn(content_chunks=["x"])]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.bell = lambda: ring.append(1)  # type: ignore[method-assign]
        app.submit_text("hi")
        await pilot.pause(1.2)
        assert len(ring) == 1, "one bell per finished turn"

    buf = io.StringIO()
    cfg2 = default_config()
    cfg2.ui.notify = "desktop"
    app2 = OasetApp(cfg=cfg2, cwd=workspace, provider=MockProvider(
        [MockTurn(content_chunks=["x"])]), model_id="mock/mock-echo")
    async with app2.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app2._notify_stream = buf  # type: ignore[assignment]
        app2.submit_text("hi")
        await pilot.pause(1.2)
    out = buf.getvalue()
    assert "\x1b]9;" in out and "\x1b]777;notify;oAset;" in out, (
        "desktop mode emits OSC 9 + OSC 777 toasts")


async def test_shift_drag_stays_out_of_the_way(workspace):
    """Shift+drag belongs to the terminal's native selection: our block
    selection must not start (the community's #1 selection complaint,
    resolved by borrowing the host's implementation)."""
    from types import SimpleNamespace

    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        shift_event = SimpleNamespace(shift=True, button=1, screen_x=5, screen_y=5)
        await app.chat.on_mouse_down(shift_event)
        assert app.chat._selection == [] and app.chat._selecting is False
        assert not app.chat._sel_anchor


async def test_context_command_breaks_down_the_window(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("hello")
        await pilot.pause(1.2)
        app.cmd_context("")
        await pilot.pause(0.3)
        strips = app.screen._compositor.render_strips()
        cards = "\n".join("".join(seg.text for seg in strip) for strip in strips)
        assert "system prompt" in cards or "系统提示" in cards
        assert "tool schemas" in cards or "工具定义" in cards
        assert "tok" in cards
        assert "%" in cards, "the window percentage is the actionable number"
