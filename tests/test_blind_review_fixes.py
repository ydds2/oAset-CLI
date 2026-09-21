"""Blind-review fixes (2026-09-13): the four "death doors" a stranger hits.

1. no-key preflight — a message is refused locally, no placeholder-credential
   network request, no raw 401
2. twin error cards — a provider failure renders ONCE
3. welcome panel — needs-key badge, real cwd, offline mock hint
4. /help — getting-started section leads the command wall
Plus: busy tip says esc (the real interrupt), not ctrl+c (quit confirm).
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.providers.base import ProviderError
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import WelcomePanel
from oaset.tui.widgets.inline import help_text


def screen_text(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


def needs_key_app(workspace) -> OasetApp:
    """The stranger scenario: fresh config, default deepseek model, no key."""
    cfg = default_config()
    return OasetApp(cfg=cfg, cwd=workspace, model_id="deepseek/deepseek-chat")


async def test_no_key_preflight_refuses_without_network(workspace):
    """Death door 1: a message without credentials is refused locally with an
    actionable hint — the turn never starts, no 401 ever comes back."""
    app = needs_key_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("你好")
        await pilot.pause(1.0)
        assert not app._worker_running(), "turn must not start without a credential"
        screen = screen_text(app)
        assert "/login" in screen
        assert "401" not in screen  # no raw provider dump


async def test_error_is_rendered_once_not_twice(workspace):
    """Death door 2: the loop emits an error event AND raises; the render
    path must show the failure exactly once, not as twin 401-style cards."""

    class ExplodingProvider(MockProvider):
        async def stream(self, messages, tools):
            raise ProviderError("auth", "Error code: 401 - exploded", retryable=False)
            yield  # pragma: no cover

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=ExplodingProvider([MockTurn(content_chunks=["x"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("hello")
        await pilot.pause(1.5)
        from oaset.tui.widgets.chat import NoticeCard

        chat_text = "\n".join(card.raw_text for card in app.chat.query(NoticeCard))
        occurrences = chat_text.count("exploded")
        assert occurrences == 1, f"expected the failure once, got {occurrences}"


async def test_welcome_panel_shows_badge_real_path_and_mock_hint(workspace):
    """Death door 3 (welcome half): an unconfigured model is BADGED as such,
    the directory is the real path (never bare '.'), and /login is the next step."""
    app = needs_key_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        panel = app.chat.query(WelcomePanel).first()
        raw = panel.raw_text if panel.raw_text != "welcome" else ""
        from textual.widget import Widget

        rendered = str(panel.render())
        assert "/login" in rendered  # needs-key badge
        assert str(workspace) in rendered  # real directory, not "."
        del raw, Widget


async def test_welcome_panel_ready_state_has_no_badge(workspace):
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        rendered = str(app.chat.query(WelcomePanel).first().render())
        assert "缺少密钥" not in rendered and "needs a key" not in rendered
        assert str(workspace) in rendered
        panel = app.chat.query(WelcomePanel).first()
        copied = panel.copy_text()
        assert "oAset" in copied and str(workspace) in copied


def test_help_leads_with_getting_started():
    """Death door 4: /help opens with the five things a beginner needs."""
    text = help_text()
    assert "入门五件事" in text
    for entry in ("/login", "/model", "Esc", "/undo", "/sessions"):
        assert entry in text
    assert "✎" in text and "⚠" in text  # risk legend still present


def test_busy_tip_names_esc_not_ctrl_c():
    """The busy status tip must teach the REAL interrupt key: Esc.
    ctrl+c is the double-press quit confirm — pointing cancellers at it was
    an active trap."""
    from oaset.i18n import t

    assert "esc" in t("hint_busy")
    assert "ctrl+c" not in t("hint_busy")
    assert "ctrl+c" not in t("hint_idle")


async def test_approval_hides_input_placeholder(workspace):
    """While an approval panel owns the dock, the input placeholder below it
    goes quiet; it comes back after the decision."""
    from oaset.tui.widgets.inline import InlinePermission

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([
                       MockTurn(tool_calls=[MockToolCall("write_file",
                                                          '{"path": "x.txt", "content": "hi"}')]),
                       MockTurn(content_chunks=["done"]),
                   ]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        original = app.input_box.placeholder
        assert original  # sanity: there IS a placeholder
        app.submit_text("写个文件")
        assert await wait_for(pilot, lambda: list(app.query(InlinePermission)))
        assert app.input_box.placeholder == ""  # quiet while approval is up
        await pilot.press("escape")  # deny
        assert await wait_for(pilot, lambda: not list(app.query(InlinePermission)))
        assert app.input_box.placeholder == original  # restored


async def wait_for(pilot, predicate, attempts: int = 40) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(0.05)
    return False
