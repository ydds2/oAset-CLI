"""First-run language pick and /language switch."""

from __future__ import annotations

from oaset.config import default_config, load_config, save_config
from oaset.i18n import resolve_language, set_language, t
from oaset.providers import MockProvider
from oaset.tui.app import OasetApp
from oaset.tui.commands import COMMANDS


def test_new_home_is_unchosen_existing_home_is_chosen(isolated_home, monkeypatch):
    cfg = load_config(create=True)
    assert cfg.language_chosen is False
    assert (isolated_home / "config.toml").is_file()
    # reload of that first-write still remembers "not chosen"
    again = load_config()
    assert again.language_chosen is False

    cfg.ui_language = "en"
    cfg.language_chosen = True
    save_config(cfg)
    loaded = load_config()
    assert loaded.ui_language == "en" and loaded.language_chosen is True


def test_in_memory_default_does_not_prompt():
    assert default_config().language_chosen is True


def test_language_command_is_registered():
    names = {c.name for c in COMMANDS}
    assert "language" in names
    cmd = next(c for c in COMMANDS if c.name == "language")
    assert "lang" in cmd.aliases


async def test_apply_language_persists_and_switches_chrome(workspace, isolated_home):
    cfg = default_config()
    cfg.ui_language = "zh"
    cfg.language_chosen = True
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause(0.05)
        set_language("zh")
        assert "输入" in app.input_area.placeholder or "消息" in app.input_area.placeholder
        app._apply_language("en")
        await pilot.pause(0.05)
        assert resolve_language() == "en"
        assert "message" in app.input_area.placeholder.lower() or "type" in app.input_area.placeholder.lower()
        loaded = load_config()
        assert loaded.ui_language == "en" and loaded.language_chosen is True
        assert t("lang_set").startswith("Language")


async def test_first_run_offers_language_picker(workspace, isolated_home):
    cfg = load_config(create=True)
    assert cfg.language_chosen is False
    cfg.default_model = "mock/mock-echo"
    cfg.default_provider = "mock"
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause(0.2)
        # picker is mounted above the input
        from oaset.tui.widgets.inline import InlinePicker

        pickers = list(app.query(InlinePicker))
        assert pickers, "first launch must ask 中文 / English"
        options = list(getattr(pickers[0], "options", []))
        values = [opt[0] if isinstance(opt, tuple) else str(opt) for opt in options]
        assert "zh" in values and "en" in values
        if not pickers[0].future.done():
            pickers[0].future.set_result("en")
        await pilot.pause(0.15)
        assert app.cfg.ui_language == "en"
        assert app.cfg.language_chosen is True
