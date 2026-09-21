"""Launch the real TUI headless and dump screenshots + compositor text.

This is the visual loop the product polish needs: look at the actual first
screen, a live turn, a tool line, and /help — then edit from evidence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "tui-frames"


def screen_text(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


def dump(app, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    text = screen_text(app)
    (OUT / f"{name}.txt").write_text(text, encoding="utf-8")
    try:
        app.save_screenshot(str(OUT / f"{name}.svg"))
    except Exception as exc:
        (OUT / f"{name}.svg.err").write_text(str(exc), encoding="utf-8")
    print(f"=== {name} {app.size} ===")
    print(text)
    print()


async def main() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockToolCall, MockTurn
    from oaset.tui.app import OasetApp

    workspace = ROOT
    cfg = default_config()
    cfg.ui_language = "zh"
    script = [
        MockTurn(content_chunks=["先读 README。\n"],
                 tool_calls=[MockToolCall("read_file", {"path": "README.md"})]),
        MockTurn(content_chunks=[
            "oAset 是本地优先的编码 CLI。\n\n"
            "| 项 | 值 |\n|---|---|\n| 许可 | 确认 |\n| 会话 | JSONL |\n"
        ]),
    ]
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider(script),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        dump(app, "01-idle")
        app.input_area.load_text("这个项目做什么？")
        await pilot.pause(0.1)
        dump(app, "02-draft")
        await pilot.press("enter")
        await pilot.pause(1.2)
        dump(app, "03-after-send")
        await pilot.pause(2.0)
        dump(app, "04-turn-done")
        app.submit_text("/help")
        await pilot.pause(0.4)
        dump(app, "05-help")
        app.submit_text("/skills")
        await pilot.pause(0.6)
        dump(app, "06-skills")


if __name__ == "__main__":
    asyncio.run(main())
