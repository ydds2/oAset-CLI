"""Provider-native capabilities + zhipu search engine + Workspace audit (G1)."""

from __future__ import annotations

import asyncio
import json

from oaset.capabilities import (
    NativeWebSearchTool,
    install_native_capabilities,
    native_web_search_supported,
    split_native_tools,
)
from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tools.search import default_engines, resolve_backends
from oaset.tools.search.zhipu import parse_zhipu_response
from oaset.workspace import Workspace

# ------------------------------------------------------------- capabilities

def test_native_web_search_supported_matrix():
    assert native_web_search_supported("moonshot")
    assert native_web_search_supported("moonshot/kimi-k2")  # provider/model form
    assert not native_web_search_supported("deepseek")
    assert not native_web_search_supported("glm")  # glm uses the zhipu ENGINE path
    assert not native_web_search_supported("")


def test_install_native_web_search_swaps_local_out():
    from oaset.tools import ToolRegistry

    registry = ToolRegistry(gate=None)  # default toolset incl. local web_search
    stepped = install_native_capabilities(registry, "moonshot")
    assert stepped == ["web_search"]
    assert "$web_search" in registry.tools, "native tool declared to the model"
    assert "web_search" not in registry.tools, "local schema leaves the request (tokens saved)"
    # idempotent
    assert install_native_capabilities(registry, "moonshot") == []

    # unsupported provider: nothing changes
    plain = ToolRegistry(gate=None)
    assert install_native_capabilities(plain, "deepseek") == []
    assert "web_search" in plain.tools and "$web_search" not in plain.tools

    # config opt-out
    off = ToolRegistry(gate=None)
    assert install_native_capabilities(off, "moonshot", enabled=False) == []


def test_native_tool_schema_and_empty_result_contract():
    import asyncio

    from oaset.tools.base import ToolContext

    tool = NativeWebSearchTool()
    schema = tool.schema()
    assert schema["type"] == "builtin_function"
    assert schema["function"]["name"] == "$web_search"
    result = asyncio.run(tool.run({}, ToolContext(cwd=__import__("pathlib").Path("."))))
    assert result.output == "" and not result.is_error, (
        "empty reply is the platform contract: the provider executes the search")


def test_providers_filter_dollar_tools_by_support():
    native = {"type": "builtin_function", "function": {"name": "$web_search"}}
    normal = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    kept, stripped = split_native_tools([normal, native])
    assert [t["function"]["name"] for t in kept] == ["read_file"]
    assert stripped == [native]


async def test_kimi_turn_runs_native_search_end_to_end(workspace):
    """A moonshot-model turn: model calls $web_search; the loop answers with
    the empty platform reply and continues — no local search engine runs."""
    from oaset.agent.runner import execute_prompt
    from oaset.config import save_config

    cfg = default_config()
    cfg.search.backend = "auto"
    save_config(cfg)

    script = [
        MockTurn(content_chunks=["查一下\n"],
                 tool_calls=[MockToolCall("$web_search", {"query": "oaset cli"})]),
        MockTurn(content_chunks=["搜索完成，结论如下。"]),
    ]
    # execute_prompt builds the runtime from cfg: point the default model at
    # a moonshot provider entry so install_native_capabilities fires, but
    # inject the mock provider so no network happens
    cfg.default_provider = "moonshot"
    cfg.default_model = "moonshot/kimi-k2"
    reply = await execute_prompt(cfg, workspace, "search please",
                                 provider=MockProvider(script), yolo=True,
                                 with_mcp=False, persist=False)
    assert "结论" in reply


# ------------------------------------------------------------- zhipu engine

ZHIPU_FIXTURE = {
    "id": "x", "choices": [{
        "message": {
            "role": "assistant",
            "tool_calls": [{
                "id": "t1",
                "type": "web_search",
                "function": {
                    "name": "$web_search",
                    "arguments": json.dumps({
                        "search_result": [
                            {"title": "oAset CLI", "link": "https://example.com/oaset",
                             "content": "本地优先的终端编程 agent", "index": 0},
                            {"title": "", "link": "https://skip.me", "content": "no title"},
                            {"title": "Second", "link": "https://example.com/2",
                             "content": "第二条"},
                        ]
                    }, ensure_ascii=False),
                },
            }],
        },
    }],
}


def test_zhipu_parse_extracts_rows_and_skips_incomplete():
    hits = parse_zhipu_response(ZHIPU_FIXTURE, limit=5)
    assert [h.title for h in hits] == ["oAset CLI", "Second"]
    assert hits[0].url == "https://example.com/oaset"
    assert "本地优先" in hits[0].snippet
    assert parse_zhipu_response({}, 5) == []


def test_auto_chain_prefers_zhipu_when_glm_key_exists(monkeypatch, isolated_home):
    from oaset.credentials import save_credential

    save_credential("glm", "sk-test-key")
    backends, error = resolve_backends("auto")
    assert not error and [b.name for b in backends] == ["zhipu"]

    # no key -> official-zero-config fallback, unchanged behaviour
    from oaset import credentials as cred

    monkeypatch.setattr(cred, "load_credential", lambda p, home=None: "")
    import oaset.tools.search.zhipu as zhipu_mod

    monkeypatch.setattr(zhipu_mod, "_glm_key", lambda: "")
    backends, error = resolve_backends("auto")
    assert not error and [b.name for b in backends] == ["bing_rss"]


def test_default_engines_leads_with_zhipu_when_key_present(isolated_home):
    from oaset.credentials import save_credential

    save_credential("glm", "sk-test-key")
    engines = default_engines()
    assert engines[0] == "zhipu" and "bing_rss" in engines


# ------------------------------------------------------------- Workspace (G1)

async def test_workspace_multi_host_and_audit_trail(workspace, isolated_home):
    ws = Workspace(default_config(), workspace, home=isolated_home)
    a = ws.host("left")
    b = ws.host("right")
    assert a is ws.host("left"), "named hosts are stable"
    assert a.session.meta.session_id != b.session.meta.session_id

    script = [MockTurn(content_chunks=["done A\n"],
                       tool_calls=[MockToolCall("read_file", {"path": "x.txt"})])]
    for host, prompt in ((a, "task A"), (b, "task B")):
        host.provider = MockProvider(script)
        host.kernel.provider = host.provider

    async def _drain(host, prompt):
        async for _event in host.chat(prompt):
            pass

    await asyncio.gather(_drain(a, "task A"), _drain(b, "task B"))
    await asyncio.sleep(0.1)  # let fire-and-forget audit writes land
    await ws.aclose()

    trail = ws.read_audit()
    tools = {row["tool"] for row in trail}
    assert "read_file" in tools, "every tool call lands in the audit JSONL"
    hosts = {row["host"] for row in trail}
    assert {"left", "right"} <= hosts, "rows carry the host label"
    sessions = {row["session"] for row in trail}
    assert len(sessions) >= 2, "each host's own session id is recorded"
    for row in trail:
        assert {"ts", "tool", "args", "error", "ms"} <= set(row)
