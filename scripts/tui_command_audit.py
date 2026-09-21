"""User-perspective audit: dispatch every slash command and record what a user gets.

For each command in the registry this boots a fresh TUI (isolated OASET_HOME,
mock provider) and drives the REAL user path - `submit_text("/name")` - then
records whether the user saw feedback, got guided by a prompt, hit silence, or
hit an exception. Output is a markdown table plus JSON so findings can be
reviewed and re-run after fixes.

Run:  .venv/Scripts/python.exe scripts/tui_command_audit.py [--json OUT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Side effects (config, credentials, sessions, cron, logs) are isolated by
# _isolate_home() before anything reads them. Imported as a library by the
# command-contract test, the caller keeps its own OASET_HOME.
_HOME: Path | None = None


def _isolate_home() -> Path:
    """Point OASET_HOME at a throwaway dir (auditing must never touch ~/.oaset)."""
    global _HOME
    if _HOME is None:
        _HOME = Path(tempfile.mkdtemp(prefix="oaset-audit-"))
        os.environ["OASET_HOME"] = str(_HOME)
    return _HOME

from oaset.config import default_config  # noqa: E402
from oaset.tui.app import OasetApp  # noqa: E402
from oaset.tui.commands import COMMANDS  # noqa: E402
from oaset.tui.widgets.chat import NoticeCard  # noqa: E402
from oaset.tui.widgets.inline import InlinePromptBase  # noqa: E402

# Commands that cannot be run in a headless sweep without ending the session or
# driving the real desktop; audited separately and listed as such.
SKIP = {
    "exit": "terminates the TUI session",
}

# requires_args commands: the sweep sends no argument on purpose to check the
# guidance path (the inline prompt) the user actually meets.
PROBE_ARGS: dict[str, str] = {}


def _snapshot(app) -> dict:
    """Everything a user can perceive change, not just chat notices.

    Sidebar visibility and the input box matter too: /todo and /clear are not
    silent when a panel opens or the banner redraws, and /paste lands in the
    input field. Counting only notices produced false SILENT verdicts before.
    """
    try:
        notices = [n.raw_text for n in app.chat.query(NoticeCard)]
    except Exception:
        notices = []
    def _sidebar() -> bool:
        try:
            return bool(getattr(app, "side_bar", None) and app.side_bar.display)
        except Exception:
            return False
    def _input_len() -> int:
        try:
            return len(app.input_area._input.value or "") if hasattr(app.input_area, "_input")                 else len(getattr(app.input_area, "text", "") or "")
        except Exception:
            return 0
    return {
        "notices": notices,
        "notice_count": len(notices),
        "cards": len(list(app.chat.query("Static.chat-card"))),
        "tool_cards": len(list(app.chat.query("ToolCard"))),
        "prompts": len(list(app.query(InlinePromptBase))),
        "model": getattr(getattr(app, "model_cfg", None), "id", ""),
        "theme": getattr(getattr(app, "cfg", None), "ui", None) and app.cfg.ui.theme,
        "mode": getattr(app, "mode", ""),
        "session": getattr(getattr(app, "session", None), "id", ""),
        "sidebar": _sidebar(),
        "input_len": _input_len(),
    }


def _classify(before: dict, after: dict, error: str | None) -> str:
    if error:
        return "ERROR"
    if after["prompts"] > before["prompts"]:
        return "PROMPT"  # asked the user something (guidance or confirmation)
    changed = (
        after["notice_count"] > before["notice_count"]
        or after["cards"] > before["cards"]
        or after["tool_cards"] > before["tool_cards"]
        or after["model"] != before["model"]
        or after["theme"] != before["theme"]
        or after["mode"] != before["mode"]
        or after["session"] != before["session"]
        or after["sidebar"] != before["sidebar"]
        or after["input_len"] != before["input_len"]
    )
    return "FEEDBACK" if changed else "SILENT"


async def audit_one(cmd, workspace: Path, timeout: float = 20.0) -> dict:
    from oaset.providers.mock import MockProvider, MockTurn

    cfg = default_config()
    cfg.ui_language = "zh"  # audit the product default language
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    entry = {"name": cmd.name, "category": cmd.category, "risk": cmd.risk,
             "requires_args": cmd.requires_args, "effect": cmd.effect,
             "usage": cmd.usage, "outcome": "", "detail": "", "notices": []}
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = _snapshot(app)
        line = "/" + cmd.name
        if cmd.name in PROBE_ARGS:
            line += " " + PROBE_ARGS[cmd.name]
        error = None
        try:
            app.submit_text(line)
            await asyncio.wait_for(pilot.pause(0.6), timeout=timeout)
        except asyncio.TimeoutError:
            error = f"TIMEOUT after {timeout}s (command never returned)"
        except Exception as exc:  # noqa: BLE001 - audit must survive anything
            error = f"{type(exc).__name__}: {exc}"
        after = _snapshot(app)
        entry["outcome"] = _classify(before, after, error)
        entry["detail"] = error or ""
        entry["notices"] = [n for n in after["notices"] if n not in before["notices"]][:3]
        # leave any prompt we opened (cancel) so the app can shut down cleanly
        try:
            for prompt in list(app.query(InlinePromptBase)):
                prompt._resolve(None)
                await pilot.pause(0.1)
        except Exception:
            pass
    return entry


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    parser.add_argument("--markdown", default="", help="write the audit report as Markdown")
    parser.add_argument("--only", default="", help="comma-separated command names")
    args = parser.parse_args()

    home = _isolate_home()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    results = await audit_all(home, only=only)
    report = _report(results, home)

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nJSON ->", args.json)
    if args.markdown:
        Path(args.markdown).write_text(_render_markdown(report), encoding="utf-8")
        print("Markdown ->", args.markdown)

    print("\n== 汇总 ==")
    for k, v in report["summary"].items():
        print(f"  {k:<9} {v}")
    print("\n== 需要修（SILENT / ERROR）==")
    for r in results:
        if r["outcome"] in ("SILENT", "ERROR"):
            print(f"  [{r['outcome']}] /{r['name']} — {r['detail'] or '无任何可见反馈'}")
    return 0


async def audit_all(home: Path, *, only: set[str] | None = None,
                    timeout: float = 20.0) -> list[dict]:
    """Dispatch every command once and classify what the user saw.

    Shared by the CLI report and the CI contract test so both measure the same
    thing: the real user path, one fresh app per command.
    """
    only = only or set()
    commands = [c for c in COMMANDS if not only or c.name in only]
    results: list[dict] = []
    for cmd in commands:
        if cmd.name in SKIP:
            results.append({"name": cmd.name, "category": cmd.category, "risk": cmd.risk,
                            "requires_args": cmd.requires_args, "effect": cmd.effect,
                            "usage": cmd.usage, "outcome": "SKIPPED",
                            "detail": SKIP[cmd.name], "notices": []})
            continue
        ws = home / ("ws-" + cmd.name)
        ws.mkdir(parents=True, exist_ok=True)
        results.append(await audit_one(cmd, ws, timeout=timeout))
        print(f"  {cmd.name:<16} {results[-1]['outcome']:<9} {results[-1]['detail'][:60]}")
    return results


def _report(results: list[dict], home: Path) -> dict:
    return {
        "audited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "oaset_home": str(home),
        "total": len(results),
        "summary": {k: sum(1 for r in results if r["outcome"] == k)
                    for k in ("FEEDBACK", "PROMPT", "SILENT", "ERROR", "SKIPPED")},
        "commands": results,
    }


def _render_markdown(report: dict) -> str:
    """The audit as a reviewable document (regenerated by every run)."""
    order = {"ERROR": 0, "SILENT": 1, "PROMPT": 2, "FEEDBACK": 3, "SKIPPED": 4}
    rows = sorted(report["commands"],
                  key=lambda r: (order.get(r["outcome"], 9), r["name"]))
    summary = report["summary"]
    out = [
        "# oAset TUI 命令审计报告（自动生成）",
        "",
        f"生成时间：{report['audited_at']}　命令总数：{report['total']}",
        "",
        "方法：隔离 `OASET_HOME` + mock provider，逐条走用户真实路径 "
        '`submit_text("/name")`，记录用户实际看到什么。',
        "",
        "| 结果 | 数量 | 含义 |",
        "|---|---|---|",
        f"| FEEDBACK | {summary['FEEDBACK']} | 有可见反馈 |",
        f"| PROMPT | {summary['PROMPT']} | 弹出表单引导（缺参引导 / 危险操作确认） |",
        f"| SILENT | {summary['SILENT']} | **零反馈（不允许）** |",
        f"| ERROR | {summary['ERROR']} | **抛异常（不允许）** |",
        f"| SKIPPED | {summary['SKIPPED']} | 终止会话类，单独审 |",
        "",
        "## 命令矩阵",
        "",
        "| 命令 | 类别 | 风险 | 需参数 | 结果 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        detail = (r["detail"] or "").replace("|", "/")[:44]
        if r["outcome"] == "FEEDBACK" and r["notices"]:
            detail = (r["notices"][0] or "").replace("|", "/")[:44]
        out.append(f"| `/{r['name']}` | {r['category']} | {r['risk']} | "
                   f"{'是' if r['requires_args'] else ''} | **{r['outcome']}** | {detail} |")
    out += ["", "## 门禁含义", "",
            "本报告由 `scripts/tui_command_audit.py --markdown` 生成。"
            "把「`SILENT` 与 `ERROR` 必须为 0」设为 CI 门，"
            "即可保证新增命令不会静默失败或崩溃后无声。",
            ""]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
