"""Final end-to-end check of the review fixes on the real app.

Drives the actual TUI and reports each fixed behaviour as PASS/FAIL, so the
green test suite is corroborated by observable app behaviour.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def make_app(script=None, cfg=None):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    cfg = cfg or default_config()
    cfg.ui_language = "zh"
    return OasetApp(cfg=cfg, cwd=ROOT, provider=MockProvider(script or []),
                    model_id="mock/mock-echo")


def status_text(app) -> str:
    bar = app.status_bar
    return " ".join(str(getattr(bar, a, "") or "")
                    for a in ("left_text", "right1_text", "right2_text"))


async def main() -> None:
    # 1. paste reaches the provider as content, not as a marker
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp
    from oaset.tui.paste import resolve_draft
    from oaset.utils import oaset_home

    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    # Construct the app with the provider directly so the host is built in
    # __init__ (the fast-boot path defers it, and `provider.requests` would stay
    # empty because the real chain would be used instead).
    app = OasetApp(cfg=default_config(), cwd=ROOT, provider=provider,
                   model_id="mock/mock-echo")
    app.cfg.ui_language = "zh"
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        big = "".join(f"LOG LINE {i}: boom\n" for i in range(400))
        app.input_area.insert_collapsed_paste(big)
        await pilot.pause(0.2)
        draft = app.input_area.text
        resolved, expanded = resolve_draft(draft, oaset_home() / "pastes")
        check("paste: draft collapsed", "[pasted:" in draft, f"{len(draft)} chars")
        check("paste: expands to the body", len(expanded) == 1 and "LOG LINE 0" in resolved,
              f"-> {len(resolved)} chars")
        app.submit_text(draft)
        for _ in range(60):
            await pilot.pause(0.1)
            if not app._worker_running():
                break
    sent = "\n".join(str(m.get("content")) for r in provider.requests for m in r
                     if isinstance(m.get("content"), str))
    check("paste: provider got the content", "LOG LINE 399: boom" in sent)
    check("paste: provider never saw the marker", "[pasted:" not in sent)

    # 2. tool-card spinner animates and shows elapsed time
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        run = app.activity_start("t1", "run_shell", "pytest -q")
        card = run.card
        frames = []
        for _ in range(25):
            app._spin_cards()
            await pilot.pause(0.12)
            frames.append(card.title_text)
        card._started -= 9.0
        app._spin_cards()
        await pilot.pause(0.2)
        check("spinner: animates", len(set(frames)) > 1, f"{len(set(frames))} frames")
        check("spinner: shows elapsed", bool(re.search(r"\d+s", card.title_text or "")),
              repr(card.title_text))
        # 2b. an interrupted run is closed out, not left spinning
        app.flush_tool_activity()
        await pilot.pause(0.2)
        check("interrupt: card finished", "tool-running" not in card.classes,
              f"classes={sorted(card.classes)}")

    # 3. the mounted DOM is actually bounded
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        chat = app.chat
        for i in range(900):
            chat.add_notice(f"notice line {i}", "info")
            if i % 50 == 0:
                await pilot.pause(0.03)
        for _ in range(30):
            await pilot.pause(0.05)
        live = len(chat._live_blocks())
        stale = [w for w in chat.children if getattr(w, "_oaset_archived", False)]
        check("archive: DOM bounded", live <= chat.max_mounted_blocks + 2,
              f"live={live} cap={chat.max_mounted_blocks}")
        check("archive: no duplicates",
              len(chat._archived) == len({id(w) for w in chat._archived}),
              f"{len(chat._archived)} refs")
        check("archive: nothing archived but mounted", not stale, f"{len(stale)} stale")

    # 4. status bar carries useful content on a narrow terminal
    app = make_app()
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause(0.3)
        app.set_mode("plan")
        await pilot.pause(0.3)
        narrow = status_text(app)
        check("status: narrow shows mode", "plan" in narrow.lower(), repr(narrow[:70]))
        check("status: narrow does not repeat the mode badge",
              narrow.count("plan 模式（只读）") <= 1,
              f"badge appears {narrow.count('plan 模式（只读）')}x")
    app = make_app()
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.set_mode("auto")
        await pilot.pause(0.3)
        check("status: auto badge", "自动放行" in status_text(app),
              repr(status_text(app)[:70]))
        app.set_mode("default")

    # 5. /density with no argument reports instead of silently changing
    app = make_app()
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        before = app.cfg.ui.density
        app.submit_text("/density")
        await pilot.pause(0.5)
        check("density: no silent change", app.cfg.ui.density == before,
              f"{before} -> {app.cfg.ui.density}")

    # 6. goal is idempotent and survives a skills refresh
    app = make_app()
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.cmd_goal("objective one")
        app.cmd_goal("objective two")
        app._refresh_skills_prompt()
        await pilot.pause(0.2)
        prompt = app.conversation.system_prompt
        check("goal: single block", prompt.count("# Current goal") == 1,
              f"{prompt.count('# Current goal')} block(s)")
        check("goal: survives skills refresh", "objective two" in prompt)

    # 7. restored sessions do not invent timings
    app = make_app()
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        card = app.chat.add_tool_card("read_file", "src/x.py", restored=True)
        card.finish("body", is_error=False)
        await pilot.pause(0.2)
        check("restore: no fake elapsed", "0.0s" not in (card.title_text or ""),
              repr(card.title_text))

    # 8. /memory states what it withheld and offers the full text
    from oaset.i18n import t as _t
    from oaset.tui.widgets.chat import NoticeCard
    from oaset.utils import oaset_home

    mem_dir = oaset_home() / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(
        "".join(f"fact {i}: remembered\n" for i in range(400)), encoding="utf-8")
    app = make_app()
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        await app.cmd_memory("")
        await pilot.pause(0.3)
        shown = "\n".join(w.raw_text for w in app.chat.query(NoticeCard))
    check("memory: shows a preview", "fact 0:" in shown)
    check("memory: states what it withheld", _t("memory_view_hint") in shown)
    check("memory: names the full-text path", "/memory all" in shown)
    (mem_dir / "MEMORY.md").unlink(missing_ok=True)

    # 9. /search hits are actionable instead of a dead-end list
    import inspect as _inspect

    from oaset.tui.controllers import session_admin as _sa

    search_src = _inspect.getsource(_sa.SessionAdminMixin.cmd_search)
    check("search: hits open the session",
          "_pick(" in search_src and "_load_session(" in search_src)

    # 10. the app stylesheet has no dead id selectors
    import re as _re

    from oaset.tui.app import OasetApp as _App

    css_code = _re.sub(r"/\*.*?\*/", "", _App.CSS, flags=_re.S)
    css_ids = set(_re.findall(r"#([a-z0-9-]+)\s*[,{:\s]", css_code))
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        live_ids = {getattr(n, "id", None) for n in app.screen.walk_children()}
    dead = sorted(css_ids - live_ids)
    check("css: no dead id selectors", not dead, f"dead={dead}")

    width = max(len(n) for n, _, _ in CHECKS) + 2
    failures = 0
    print("=== live behaviour check ===")
    for name, ok, detail in CHECKS:
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {detail}")
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} passed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
