"""Offline task eval (P5 skeleton, Task 8 Step 1 of docs/plans/release-distribution).

`--offline` runs every task in eval/tasks.jsonl against the scripted
MockProvider with real tools on a temp workspace, asserting OBSERVABLE
behaviour (tool-call sequence, answer markers, error flags) — never model
wording. This proves the agent pipeline end-to-end without an API key; it is
a pipeline gate, not a model-quality benchmark (that needs a live provider
and lives behind --provider, deliberately unimplemented until P5).

Exit code 0 = every task green; nonzero prints the failing ids.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "eval" / "tasks.jsonl"


def load_tasks() -> list[dict]:
    """Parse one JSON object per task; pretty-printed objects are fine
    (raw_decode walks the stream), which keeps the file human-editable."""
    text = TASKS.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    tasks: list[dict] = []
    idx = 0
    size = len(text)
    while idx < size:
        while idx < size and text[idx] in " \t\r\n":
            idx += 1
        if idx >= size:
            break
        obj, idx = decoder.raw_decode(text, idx)
        tasks.append(obj)
    return tasks


class DeadProvider:
    """A primary provider whose every call fails non-retryably (fallback task)."""

    async def stream(self, messages, tools=None, **kwargs):
        from oaset.providers.base import ProviderError

        raise ProviderError("auth", "dead primary key", retryable=False)
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        pass


def build_turns(task: dict):
    from oaset.providers import MockProvider, MockToolCall, MockTurn

    turns = []
    for spec in task.get("turns", []):
        if "tool" in spec:
            name, args = spec["tool"]
            turns.append(MockTurn(
                content_chunks=[spec.get("say", "")],
                tool_calls=[MockToolCall(name, args)]))
        else:
            turns.append(MockTurn(content_chunks=list(spec.get("content", [""]))))
    return MockProvider(turns)


class OfflineSearchBackend:
    """Deterministic engine for the eval set: answers "no results" offline.

    The eval suite is documented as offline, but web_search resolved to the
    real Bing engine — one task dialled out on every run, which made the
    suite slow, network-dependent and wrong whenever the query happened to
    match something. A task that genuinely wants a live engine can ask for
    one by name (`"search_backend": "bing_rss"`).
    """

    name = "offline"
    needs_key = False
    proxy = ""

    def available(self) -> bool:
        return True

    async def search(self, query: str, limit: int, *, timeout) -> list:
        return []


def register_offline_search() -> None:
    from oaset.tools.search import SearchBackend, register_backend

    class _Offline(OfflineSearchBackend, SearchBackend):
        pass

    register_backend(_Offline)


async def run_task(task: dict) -> tuple[bool, str]:
    """Execute one task; return (ok, detail)."""
    from oaset.agent.loop import AgentLoop
    from oaset.agent.messages import Conversation
    from oaset.config import default_config
    from oaset.tools import ToolRegistry
    from oaset.tools.base import AutoGate, ReadOnlyGate, ToolContext

    register_offline_search()
    cfg = default_config()
    for key, value in (task.get("config") or {}).items():
        setattr(cfg, key, value)
    cfg.search.backend = task.get("search_backend", "offline")

    workdir = Path(tempfile.mkdtemp(prefix="oaset-eval-"))
    for rel, content in (task.get("setup") or {}).get("files", {}).items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if task.get("provider") == "dead-primary":
        provider = DeadProvider()
        fallbacks = [build_turns(task)]
    else:
        provider = build_turns(task)
        fallbacks = []

    gate = ReadOnlyGate() if task.get("gate") == "readonly" else AutoGate()
    registry = ToolRegistry(gate=gate)
    ctx = ToolContext(cwd=workdir, mode=task.get("gate") or "default",
                      output_limit=task.get("config", {}).get("tool_output_limit", 2000),
                      network_mode=task.get("config", {}).get("network_mode", "pull_only"),
                      # tools read their own settings section from here; without
                      # it web_search fell back to defaults (live Bing) and the
                      # task's config never reached the tool
                      config=cfg)
    registry.bind(ctx)

    loop = AgentLoop(provider, registry, Conversation(system_prompt="eval"),
                     max_iterations=cfg.max_iterations,
                     max_tool_calls=cfg.max_tool_calls,
                     injections=list(task.get("injections", [])),
                     fallbacks=fallbacks)
    events: list[dict] = []
    emit = events.append
    if task.get("cancel"):
        run_task_handle = asyncio.ensure_future(loop.run(task["prompt"], emit))
        await asyncio.sleep(0.05)
        run_task_handle.cancel()
        try:
            await run_task_handle
        except asyncio.CancelledError:
            pass
    else:
        await loop.run(task["prompt"], emit)

    expect = task.get("expect", {})
    tools_called = [e.get("name") for e in events if e.get("type") == "tool_start"]
    final_text = next((e.get("content", "") for e in reversed(events)
                       if e.get("type") == "turn_done"), "") or \
        next((e.get("message", None) and getattr(e["message"], "content", "") or ""
              for e in reversed(events) if e.get("type") == "message_done"), "")
    if task.get("cancel"):
        final_text = "".join(e.get("message").content or ""
                             for e in events if e.get("type") == "message_done"
                             and getattr(e.get("message"), "role", "") == "assistant")
    problems: list[str] = []
    tool_outputs = "\n".join(e.get("result", "") for e in events
                             if e.get("type") == "tool_end")
    if "tool_output_contains" in expect and \
            expect["tool_output_contains"] not in tool_outputs:
        problems.append(f"{expect['tool_output_contains']!r} not in tool output")
    if "tools" in expect and tools_called != expect["tools"]:
        problems.append(f"tools {tools_called} != {expect['tools']}")
    if "contains" in expect and expect["contains"] not in (final_text or ""):
        problems.append(f"{expect['contains']!r} not in final text {final_text[:120]!r}")
    if "not_contains" in expect and expect["not_contains"] in (final_text or ""):
        problems.append(f"{expect['not_contains']!r} must not appear")
    if expect.get("is_error"):
        error_events = [e for e in events if e.get("type") == "tool_end"
                        and e.get("is_error")]
        if not error_events:
            problems.append("expected a tool error event, none seen")
    if expect.get("notice_fallback"):
        if not any(e.get("type") == "notice" for e in events):
            problems.append("expected a fallback/switch notice")
    if "persisted_messages" in expect:
        saved: list = []
        loop.on_message = saved.append
        # on_message is consulted during run(); the run already happened, so
        # re-check via the conversation instead.
        count = len(loop.conversation.messages)
        if count < expect["persisted_messages"]:
            problems.append(f"conversation has {count} messages, "
                            f"expected >= {expect['persisted_messages']}")
    return (not problems), "; ".join(problems) or "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline task eval")
    parser.add_argument("--offline", action="store_true", default=True)
    parser.add_argument("--only", default="", help="run one task by id")
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]

    failures: list[tuple[str, str]] = []

    async def all_tasks() -> None:
        for task in tasks:
            ok, detail = await run_task(task)
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] {task['id']} ({task.get('category', '')}) {detail}")
            if not ok:
                failures.append((task["id"], detail))

    asyncio.run(all_tasks())
    print(f"\n{len(tasks) - len(failures)}/{len(tasks)} tasks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
