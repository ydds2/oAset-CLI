"""TUI semantics regressions: first-frame language, usage single-source,
model metadata sync, full /reload rebuild and MCP reload cleanup (RESP-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """save_config/load_config must NEVER touch the real ~/.oaset/config.toml."""
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace, script, language="en"):
    cfg = default_config()
    cfg.ui_language = language
    provider = MockProvider(script)
    return OasetApp(cfg=cfg, cwd=workspace, provider=provider, model_id="mock/mock-echo")


async def test_first_frame_renders_configured_language(workspace):
    """A4: language must be applied BEFORE the welcome panel is composed."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")], language="zh")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        from oaset.i18n import t

        assert app.input_area.placeholder == t("input_placeholder")
        assert "❯" in str(app.query_one("#prompt-glyph").render())
        # the i18n language was active when on_mount composed the first frame
        from oaset.i18n import resolve_language

        assert resolve_language() == "zh"


async def test_usage_reads_store_without_double_count(workspace):
    app = make_app(
        workspace,
        [
            MockTurn(content_chunks=["answer"], finish_reason="stop",
                     usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("hi")
        await pilot.press("enter")
        from oaset.tui.widgets.chat import NoticeCard

        deadline_chars = 0
        while deadline_chars < 60 and not app.store.session_usage(app.session.meta.session_id)["entries"]:
            await pilot.pause(0.05)
            deadline_chars += 1
        persisted = app.store.session_usage(app.session.meta.session_id)
        assert persisted["total_tokens"] == 15
        await app.cmd_usage("")  # reads the store off the loop since the review fix
        notices = [n.raw_text for n in app.chat.query(NoticeCard)]
        usage_line = next(n for n in notices if "Usage this session" in n)
        # exactly one source: total must equal the persisted 15, not 30
        assert "total 15 tok" in usage_line, usage_line


async def test_model_switch_updates_session_meta(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        assert app.session.meta.model == "mock/mock-echo"
        # switching is verify-then-swap; stub the probe so this test exercises
        # the atomic apply path (probe semantics have their own tests below)
        async def ok_probe(provider):
            return True, "OK"

        app._probe_provider = ok_probe  # type: ignore[method-assign]
        other = next(mid for mid in app.cfg.models if mid != "mock/mock-echo")
        from oaset.credentials import save_credential

        save_credential(app.cfg.models[other].provider, "sk-test")  # usable target
        await app._switch_model_verified(other)
        assert app.session.meta.model == other
        assert app.cfg.default_model == other
        assert app._kernel.provider is app.provider


async def test_failed_model_switch_keeps_the_running_model(workspace):
    """A 401/unreachable candidate must NOT replace the live runtime (the
    exact screenshot failure: switching to a keyless model broke the session)."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        before = (app.model_cfg.id, app.provider, app.cfg.default_model)

        async def bad_probe(provider):
            return False, "401 Authentication Fails"

        app._probe_provider = bad_probe  # type: ignore[method-assign]
        candidate = next(mid for mid in app.cfg.models if mid != "mock/mock-echo")
        from oaset.credentials import save_credential

        # give it a credential so the failure under test is the PROBE, not the
        # pre-flight guard (which has its own test)
        save_credential(app.cfg.models[candidate].provider, "sk-test")
        await app._switch_model_verified(candidate)
        assert (app.model_cfg.id, app.provider, app.cfg.default_model) == before
        assert app.session.meta.model == "mock/mock-echo"
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "401" in rendered or "401" in app.status_bar.error_text


async def test_reload_rebuilds_provider_fallbacks_hooks_shell(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        # user edits config: disable sandbox default and shrink memory
        app.cfg.shell_sandbox_default = False
        app.cfg.shell_sandbox_memory_mb = 1024
        from oaset.config import save_config

        save_config(app.cfg)
        app.cmd_reload("")
        await pilot.pause(0.1)
        shell = app.registry.ctx.session_state["shell_config"]
        assert shell["sandbox_default"] is False
        assert shell["sandbox_memory_mb"] == 1024
        assert app._kernel.provider is app.provider
        assert app._kernel.fallbacks == app._fallbacks
        assert app._kernel.hooks is not None


async def test_mcp_reload_removes_stale_tools(workspace):
    """Deleted MCP tools must leave the registry after /reload-mcp."""
    from oaset.mcp import McpManager
    from oaset.tools.base import Tool, ToolResult

    class FakeAdapter(Tool):
        name = "mcpfake__do"
        description = "fake"
        permission = "read"
        parameters = {}

        async def run(self, args, ctx):
            return ToolResult("x")

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        # simulate an initial MCP load of one tool
        app.registry.add_tool(FakeAdapter())
        app._mcp_tool_names = {"mcpfake__do"}
        assert "mcpfake__do" in app.registry.tools

        class EmptyManager(McpManager):
            async def start_all(self, connect_timeout=12.0):
                return []

            def adapters(self):
                return []

        app.mcp = EmptyManager(app.mcp.home, app.mcp.cwd)
        app.cmd_reload_mcp("")  # @work: returns a Worker, runs in background
        removed = False
        for _ in range(40):  # poll until the mcp worker finishes (≤2s)
            await pilot.pause(0.05)
            if "mcpfake__do" not in app.registry._all:
                removed = True
                break
        assert removed, "stale MCP tool survived reload"
        assert app._mcp_tool_names == set()


def test_new_catalog_keys_have_both_languages():
    """I18N-01: every migrated UI string must exist in zh AND en."""
    from oaset.i18n import CATALOG

    needed = [
        "picker_palette", "picker_model", "picker_sessions", "picker_theme",
        "picker_mode", "picker_tool", "picker_ask_user",
        "update_usage", "update_missing", "update_failed",
        "budget_exhausted_msg", "budget_exhausted_detail", "budget_exhausted_hint",
        "image_usage", "image_unsupported", "image_attached", "image_ignored_unsupported",
        "no_bg_tasks", "bg_tasks_header", "memory_empty",
        "no_cron_jobs", "no_plugins",
    ]
    for key in needed:
        assert key in CATALOG, f"missing catalog key: {key}"
        assert CATALOG[key].get("zh"), f"{key} has no zh entry"
        assert CATALOG[key].get("en"), f"{key} has no en entry"


def test_hardcoded_strings_migrated_out_of_app():
    """The migrated literals must no longer appear in app.py."""
    source = (Path(__file__).resolve().parents[1] / "src" / "oaset" / "tui" / "app.py").read_text(encoding="utf-8")
    for literal in [
        "预算已用尽",
        "已附加图片",
        "上下文自动压缩",
        "Memory is empty",
        "No background tasks",
        "No scheduled jobs",
        "No plugins. Drop",
        "Permission mode: writes and shell require confirmation",
        "PLAN mode — read-only",
        "AUTO mode — everything runs",
        '"Switch model"',
        '"Restore session"',
        '"Permission mode"',
        '"Toggle tool"',
    ]:
        assert literal not in source, f"hardcoded string still present: {literal!r}"


async def test_missing_arg_prompt_collects_value(workspace):
    """requires_args commands prompt inline instead of showing a usage error."""
    from oaset.tui.widgets.inline import InlineInput

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/title")  # bypass the slash-suggest enter-accept step
        await pilot.pause(0.3)
        app.query_one(InlineInput)  # the prompt must be mounted
        for ch in "my session title":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.session.meta.title == "my session title"


async def test_missing_arg_prompt_cancel_keeps_state(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        before = app.session.meta.title
        app.submit_text("/title")
        await pilot.pause(0.3)
        await pilot.press("q")
        await pilot.pause(0.3)
        assert app.session.meta.title == before


def test_suggest_filters_by_prefix():
    from oaset.tui.commands import suggest

    everything = suggest("")
    assert len(everything) > 10
    assert "help" in [name for name, _ in suggest("/he")]  # slash tolerated
    assert "help" in [name for name, _ in suggest("he")]  # bare prefix too
    names = [name for name, _ in suggest("/se")]
    assert "sessions" in names and "search" in names
    assert suggest("/zzz-no-such") == []


async def test_unknown_event_does_not_crash_turn(workspace):
    """A malformed event (no type) must be skipped with a notice, not raise."""
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._handle_event({"text": "no type here"}, object(), {})
        await pilot.pause(0.1)
        assert any("未知事件" in n.raw_text or "unknown event" in n.raw_text.lower()
                   for n in app.chat.query(NoticeCard))


async def test_plan_mode_from_config_at_startup(workspace):
    cfg = default_config()
    cfg.permission_mode = "plan"
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        assert app.mode == "plan"
        assert app.registry.ctx.mode == "plan"


async def test_notice_without_text_does_not_crash(workspace):
    """Missing notice text must render as empty notice, never KeyError."""
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._handle_event({"type": "notice"}, object(), {})
        await pilot.pause(0.1)
        assert any(n.raw_text == "" for n in app.chat.query(NoticeCard))


async def test_unknown_event_type_is_diagnosed(workspace):
    """Known-schema drift must surface a warning naming the type."""
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._handle_event({"type": "mystery_kind_from_future"}, object(), {})
        await pilot.pause(0.1)
        assert any("mystery_kind_from_future" in n.raw_text
                   for n in app.chat.query(NoticeCard))


async def test_tool_start_without_id_gets_unique_key(workspace):
    """Two id-less tool_start events must produce two distinct cards."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        cards: dict = {}
        turn = object()
        app._handle_event({"type": "tool_call_started", "name": "a"}, turn, cards)
        app._handle_event({"type": "tool_call_started", "name": "b"}, turn, cards)
        assert len(cards) == 2, f"cards collapsed: {list(cards)}"


async def test_reload_re_resolves_model_from_new_config(workspace):
    """Reload must honour config edits: changed context limit propagates; a
    removed model falls back to the new default with a visible notice."""
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        assert app.model_cfg.id == "mock/mock-echo"
        other = next(mid for mid in app.cfg.models if mid != "mock/mock-echo")

        # case A: edit the current model's context limit in config.toml
        app.cfg.models["mock/mock-echo"].max_context_size = 123456
        from oaset.config import save_config

        save_config(app.cfg)
        app.cmd_reload("")
        await pilot.pause(0.1)
        assert app.model_cfg.max_context_size == 123456
        assert app._kernel.context_max_tokens == 123456

        # case B: remove the current model entirely → fallback to new default
        del app.cfg.models["mock/mock-echo"]
        app.cfg.default_model = other
        save_config(app.cfg)
        app.cmd_reload("")
        await pilot.pause(0.1)
        assert app.model_cfg.id == other
        assert app._kernel.provider is app.provider


def _write_plugin(path, marker: str) -> None:
    body = f'''
from oaset.tools.base import Tool, ToolResult

class _P(Tool):
    name = "plugintool"
    description = "plugin tool"
    permission = "read"
    parameters = {{}}

    async def run(self, args, ctx):
        return ToolResult("{marker}")

def register(api):
    api.add_tool(_P())
    api.add_command("plugincmd", "plugin command", lambda app, args: None)
'''
    path.write_text(body, encoding="utf-8")


def test_plugin_hot_reload_updates_tools_and_commands(workspace, monkeypatch, tmp_path):
    """/reload-plugins must retract old contributions and load fresh files.
    The plugin is project-level, so the test pins trust after each write,
    exactly like `oaset trust-plugin` would. oaset_home is isolated so the
    trust record never touches the real user directory."""
    import oaset.plugins as plugins_mod
    from oaset.plugins import PluginCommand

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(plugins_mod, "oaset_home", lambda: fake_home)
    plugin_dir = workspace / ".oaset" / "plugins"
    plugin_dir.mkdir(parents=True)
    plugin_file = plugin_dir / "demo.py"
    _write_plugin(plugin_file, "v1")
    plugins_mod.trust_plugin_file(plugin_file)  # project plugins need trust

    # use the real ToolRegistry for observable add/remove
    from oaset.tools import ToolRegistry

    registry = ToolRegistry()
    commands: dict[str, PluginCommand] = {}
    records: list = []
    log: list[str] = []
    names = plugins_mod.load_plugins(workspace, registry, commands, log.append, loaded=records)
    assert names == ["demo"]
    assert "plugintool" in registry._all
    assert "plugincmd" in commands

    # modify: same tool name, new behaviour → re-trust, then reload swaps
    _write_plugin(plugin_file, "v2")
    plugins_mod.trust_plugin_file(plugin_file)
    names2 = plugins_mod.reload_plugins(workspace, registry, commands, log.append, records)
    assert names2 == ["demo"]
    assert "plugintool" in registry._all

    # delete: reload must retract the tool and command entirely
    plugin_file.unlink()
    names3 = plugins_mod.reload_plugins(workspace, registry, commands, log.append, records)
    assert names3 == []
    assert "plugintool" not in registry._all
    assert "plugincmd" not in commands


async def test_hyphenated_command_dispatches_to_snake_handler(workspace):
    """/reload-plugins must reach cmd_reload_plugins (regression: dispatch
    previously dropped the hyphen→underscore normalization)."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        from oaset.tui import commands as cmdmod

        called = {}

        async def spy(args: str) -> None:
            called["hit"] = True  # do not await the @work original: it returns a Worker

        app.cmd_reload_plugins = spy
        await cmdmod.execute(app, "/reload-plugins")
        await pilot.pause(0.2)
        assert called.get("hit") is True


def test_metadata_aliases_dispatch_and_complete():
    """Per-command aliases must actually work in dispatch and completion,
    not just be display metadata (legacy ALIASES table stays honoured)."""
    from oaset.tui import commands as cmdmod

    amap = cmdmod.alias_map()
    # metadata alias
    assert amap.get("?") == "help"
    # legacy global aliases still resolve
    assert amap.get("q") == "exit" and amap.get("quit") == "exit"
    # completion: typing an alias completes to the canonical command
    assert any(name == "exit" for name, _ in cmdmod.suggest("/q"))
    assert any(name == "help" for name, _ in cmdmod.suggest("?"))


async def test_sdk_honors_tool_toggle_config(workspace):
    """SDK/TUI semantic parity: disabled_tools in config must disable the tool
    in the SDK registry exactly like the TUI does."""
    cfg = default_config()
    cfg.disabled_tools = ["web_fetch"]
    from oaset.sdk import Oaset

    agent = Oaset(config=cfg, cwd=workspace, model="mock/mock-echo")
    assert "web_fetch" in getattr(agent.registry, "disabled", set())
    assert "web_fetch" not in agent.registry.tools
    assert "web_fetch" in agent.registry._all  # disabled, not uninstalled


async def test_update_bare_invocation_opens_picker(workspace):
    """/update with no (or invalid) subaction opens an inline picker instead
    of dumping a usage error — no memorized flags required."""
    import time as _time

    from oaset.tui.widgets.inline import InlinePicker

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update")
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not len(app.query(InlinePicker)):
            await pilot.pause(0.05)
        assert len(app.query(InlinePicker)) == 1
        prompt = app.query_one(InlinePicker)
        offered = {value for value, _ in prompt.options}
        assert {"check", "refresh", "list", "plan"} <= offered
        await pilot.press("q")  # cancel path
        await pilot.pause(0.2)


async def test_image_missing_arg_opens_file_picker(workspace):
    """Bare /image lists workspace images in a picker; selecting one attaches it."""
    import time as _time

    from oaset.tui.widgets.inline import InlinePicker

    (workspace / "photo.png").write_bytes(b"\x89PNG fake")
    (workspace / "notes.txt").write_text("not an image", encoding="utf-8")

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    app.model_cfg.capabilities = list(app.model_cfg.capabilities) + ["image_in"]
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/image")
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not len(app.query(InlinePicker)):
            await pilot.pause(0.05)
        assert len(app.query(InlinePicker)) == 1
        prompt = app.query_one(InlinePicker)
        offered = [value for value, _ in prompt.options]
        assert any(v.endswith("photo.png") for v in offered)
        assert not any(v.endswith("notes.txt") for v in offered)  # suffix filter holds
        await pilot.press("enter")  # pick the first (only) image
        await pilot.pause(0.3)
        assert [p.name for p in app._pending_images] == ["photo.png"]

async def test_pending_image_becomes_provider_part_not_raw_path(workspace):
    """Regression: /image-attached files used to be merged into the message as
    raw Path objects; they must be encoded as OpenAI image_url parts like
    text-mentioned paths are."""
    import base64
    import time as _time

    from oaset.tui.widgets.inline import InlinePicker

    data = b"\x89PNG fake-image-bytes"
    (workspace / "photo.png").write_bytes(data)

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    app.model_cfg.capabilities = list(app.model_cfg.capabilities) + ["image_in"]
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/image")
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not len(app.query(InlinePicker)):
            await pilot.pause(0.05)
        await pilot.press("enter")
        await pilot.pause(0.3)
        app.submit_text("describe this")
        deadline = _time.monotonic() + 6.0
        msgs_with_parts: list = []
        while _time.monotonic() < deadline:
            msgs_with_parts = [
                m for m in app.conversation.messages
                if any(isinstance(p, dict) and p.get("type") == "image_url"
                       for p in getattr(m, "content", None) or [])
            ]
            if msgs_with_parts:
                break
            await pilot.pause(0.05)
        assert msgs_with_parts, "attached image must reach the conversation"
        part = next(p for p in msgs_with_parts[-1].content
                    if isinstance(p, dict) and p.get("type") == "image_url")
        assert part["type"] == "image_url"
        encoded = base64.b64encode(data).decode("ascii")
        assert encoded in part["image_url"]["url"]
        assert not any(isinstance(p, Path) for p in msgs_with_parts[-1].content)


async def test_editor_missing_arg_opens_enum_picker(workspace):
    """Bare /editor offers common editors; a pick persists to config."""
    import time as _time

    from oaset.tui.widgets.inline import InlinePicker

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/editor")
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not len(app.query(InlinePicker)):
            await pilot.pause(0.05)
        prompt = app.query_one(InlinePicker)
        offered = {value for value, _ in prompt.options}
        assert "code --wait" in offered and "vim" in offered
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.cfg.ui.editor  # persisted pick (first option)



# ------------------------------------------------------- narrow layout (resize)


async def test_narrow_screen_class_tracks_terminal_width(workspace):
    """The `narrow` screen class still tracks the width threshold; the plan
    strip lives above the input and stays hidden until a live plan exists."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        assert app.side_bar.display is False
        assert app.screen.has_class("narrow")
        await pilot.resize_terminal(160, 50)
        await pilot.pause(0.2)
        assert not app.screen.has_class("narrow")
        assert app.side_bar.display is False
        await pilot.resize_terminal(99, 30)
        await pilot.pause(0.2)
        assert app.screen.has_class("narrow")


async def test_explicit_toggle_pins_the_plan_strip(workspace):
    """ctrl+t pins the plan strip above the input; it is not a right overlay."""
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        assert app.side_bar.display is False
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.side_bar.display is True
        assert app.side_bar.styles.layer != "overlay"
        await pilot.resize_terminal(120, 40)
        await pilot.pause(0.2)
        assert app.side_bar.display is True
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.side_bar.display is False
        await pilot.resize_terminal(160, 50)
        await pilot.pause(0.2)
        assert app.side_bar.display is False


# ------------------------------------------------------- MCP roots (multi-root)


async def test_roots_lists_workspace_and_extra_roots(workspace):
    """`/roots` shows the fixed workspace plus any extra roots."""
    other = workspace.parent / "roots-list-other"
    other.mkdir(exist_ok=True)
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.registry.ctx.session_state["extra_roots"] = [other.resolve()]
        app.submit_text("/roots")
        await pilot.pause(0.3)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "roots/list" in rendered
        assert str(other.resolve()) in rendered
        assert str(workspace.resolve()) in rendered


async def test_roots_add_validates_dedupes_and_refreshes_handlers(workspace, monkeypatch):
    """`/roots add` rejects non-directories and duplicates, persists the root
    and immediately refreshes the MCP bridges so the next roots/list sees it."""
    import time as _time

    from oaset.tui.widgets.inline import InlineInput

    sub = workspace / "sibling-module"
    sub.mkdir()
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        refreshed: list[int] = []
        monkeypatch.setattr(app, "_attach_sampling_bridge", lambda: refreshed.append(1))

        # missing arg -> inline text form (Esc cancels, nothing stored)
        app.submit_text("/roots add")
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not len(app.query(InlineInput)):
            await pilot.pause(0.05)
        assert len(app.query(InlineInput)) == 1
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.registry.ctx.session_state.get("extra_roots") in (None, [])

        # non-directory rejected
        app.submit_text(f"/roots add {workspace / 'nope'}")
        await pilot.pause(0.3)
        assert app.registry.ctx.session_state.get("extra_roots") in (None, [])

        # valid add persists and refreshes handlers
        app.submit_text(f"/roots add {sub}")
        await pilot.pause(0.3)
        assert app.registry.ctx.session_state["extra_roots"] == [sub.resolve()]
        assert refreshed == [1]

        # duplicate add is refused
        app.submit_text(f"/roots add {sub}")
        await pilot.pause(0.3)
        assert app.registry.ctx.session_state["extra_roots"] == [sub.resolve()]
        assert refreshed == [1]


async def test_roots_remove_and_clear(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        a = (workspace / "root-a")
        b = (workspace / "root-b")
        a.mkdir()
        b.mkdir()
        state = app.registry.ctx.session_state
        state["extra_roots"] = [a.resolve(), b.resolve()]
        app.submit_text("/roots remove 9")
        await pilot.pause(0.3)
        assert len(state["extra_roots"]) == 2  # out-of-range is a warning, not a mutation
        app.submit_text("/roots remove 1")
        await pilot.pause(0.3)
        assert state["extra_roots"] == [b.resolve()]
        app.submit_text("/roots clear")
        await pilot.pause(0.3)
        assert state["extra_roots"] == []


async def test_interrupted_event_is_not_protocol_drift(workspace):
    """A cancelled turn's terminal event is legal: consume it silently.

    Reported on the real machine after Ctrl+X: the TUI printed
    "未处理的事件类型：turn_cancelled（可能存在协议漂移）" right above the correct
    "已被用户中断" notice. The cancellation path owns the user-facing notice,
    so the event itself must not warn.
    """
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._handle_event({"type": "turn_cancelled"}, object(), {})
        await pilot.pause(0.1)
        notices = [n.raw_text for n in app.chat.query(NoticeCard)]
        assert not any("interrupted" in text for text in notices), notices
        assert not any("协议漂移" in text or "protocol drift" in text.lower()
                       for text in notices), notices


async def test_interrupted_does_not_suppress_real_drift_warnings(workspace):
    """Consuming a legal event must not blind the unknown-type diagnostic."""
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace, [MockTurn(content_chunks=["ok"], finish_reason="stop")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._handle_event({"type": "turn_cancelled"}, object(), {})
        app._handle_event({"type": "brand_new_kind"}, object(), {})
        await pilot.pause(0.1)
        assert any("brand_new_kind" in n.raw_text for n in app.chat.query(NoticeCard))


# ------------------------------------------------- optimization round tests

async def test_plan_mode_restores_previous_mode(workspace):
    """A3: leaving plan mode restores what the user had BEFORE entering —
    an auto user must not be silently downgraded to default."""
    app = make_app(workspace, [])
    async with app.run_test(size=(100, 30)):
        app.set_mode("auto")
        app.set_mode("plan")
        assert app.mode == "plan"
        app.restore_mode()
        assert app.mode == "auto"


async def test_fallback_notice_updates_status_bar_model(workspace):
    """A4: after a mid-turn fallback the status bar names the model that is
    actually serving the turn, not the dead primary."""
    from tests.test_app_tui import wait_until

    app = make_app(workspace, [
        MockTurn(content_chunks=["primary died"], finish_reason="stop"),
    ])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("hi")
        await pilot.pause(0.2)
        # simulate the loop's structured fallback notice mid-turn
        from oaset.tui.turn_view import handle_turn_event

        handle_turn_event(app, {"type": "notice", "text": "switched",
                                "fallback": True, "model": "glm/glm-5.3-flash"},
                          object(), {})
        assert await wait_until(pilot, lambda: "glm/glm-5.3-flash" in app.status_bar.left_text)
        app._turn.worker = None
