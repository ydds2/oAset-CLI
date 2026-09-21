"""Visual loop for the 2026-09-14 density/liveliness pass.

Reproduces the screenshot complaint: one turn, 8 tool calls in a row, a huge
notice — then dumps compositor text so the fold chip, the input frame and the
notice clamp can be judged on the real screen, not from source.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def screen_text(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


async def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockToolCall, MockTurn
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.tool_card import ToolCard

    script = [
        MockTurn(content_chunks=["干起来了\n"],
                 tool_calls=[
                     MockToolCall("read_file", {"path": f"src/file{i}.py"}) for i in range(8)
                 ]),
        MockTurn(content_chunks=["八个文件都看完了，结论如下。\n\n- 结构没问题\n- 测试缺一层"]),
    ]
    cfg = default_config()
    cfg.ui_language = "zh"
    app = OasetApp(cfg=cfg, cwd=ROOT, provider=MockProvider(script), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        app.submit_text("全屏检查这个项目")
        await pilot.pause(1.0)
        # huge notice wall (the /history-style dump)
        app.chat.add_notice("长输出第 " + "\n长输出第 ".join(str(i) for i in range(1, 31)), "info")
        await pilot.pause(2.5)
        text = screen_text(app)
        out = ROOT / "docs" / "evidence" / "tui-frames"
        out.mkdir(parents=True, exist_ok=True)
        (out / "10-density.txt").write_text(text, encoding="utf-8")
        try:
            app.save_screenshot(str(out / "10-density.svg"))
        except Exception:
            pass
        print(text)
        # assertions the product must hold on the REAL screen
        cards = [c for c in app.chat.query(ToolCard)]
        hidden = [c for c in cards if not c.display]
        from oaset.tui.widgets.chat import FoldChip
        chips = list(app.chat.query(FoldChip))
        print("ASSERT fold-chip:", len(chips) == 1, "| hidden cards:", len(hidden),
              "| visible cards:", len(cards) - len(hidden))
        print("ASSERT chip label on screen:", ("已折叠" in text or "folded" in text))
        print("ASSERT notice clamp:", "还有" in text and "长输出第 30" not in text)
        print("ASSERT input frame rows:", any("╭" in ln or "┌" in ln for ln in text.splitlines()[-6:]))


if __name__ == "__main__":
    asyncio.run(main())
