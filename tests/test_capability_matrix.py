"""Native-capability engine: ALL mainstream providers, ALL mechanisms.

Coverage matrix, serialization per mechanism, runtime flag bridge, local
tool step-back, fallback safety, and the doctor report.
"""

from __future__ import annotations

import pytest

from oaset.capabilities import (
    CAPABILITIES,
    capabilities_for,
    capability_report,
    install_native_capabilities,
    native_params,
    native_server_tool,
    native_web_search_supported,
)
from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.providers.anthropic_native import AnthropicNativeProvider, _to_anthropic_tools
from oaset.providers.gemini_native import GeminiNativeProvider, _to_gemini_tools
from oaset.providers.openai_compat import OpenAICompatProvider
from oaset.runtime import build_runtime
from oaset.tools import ToolRegistry

# ----------------------------------------------------------- coverage matrix

def test_every_mainstream_provider_has_web_search_capability():
    covered = {cap.provider for cap in capabilities_for("*", "web_search")} or {
        cap.provider for cap in CAPABILITIES if cap.skill == "web_search"}
    # moonshot/openai/qwen/anthropic/gemini — the mainstream set; deepseek
    # has none (verified: its API does not offer web search) and must NOT
    # appear here
    assert {"moonshot", "openai", "qwen", "anthropic", "gemini"} <= covered
    assert "deepseek" not in covered
    mechanisms = {cap.mechanism for cap in CAPABILITIES}
    assert mechanisms == {"client_tool", "server_tool", "param"}


def test_mechanism_accessors():
    from oaset.capabilities import native_server_tools, native_skills_for

    # client_tool (moonshot): schema via the contract tool
    assert any(c.mechanism == "client_tool" for c in capabilities_for("moonshot"))
    assert native_server_tool("moonshot") is None

    # param (openai / qwen): merged request parameters
    assert native_params("openai") == {"web_search_options": {"search_context_size": "low"}}
    assert native_params("qwen") == {"enable_search": True}
    assert native_params("openai/gpt-4o") == native_params("openai")  # model form
    assert native_params("deepseek") == {}

    # server_tool (anthropic / gemini): provider-format declaration
    anthropic_tool = native_server_tool("anthropic")
    assert anthropic_tool and anthropic_tool["type"] == "web_search_20250305"
    gemini_tool = native_server_tool("gemini")
    assert gemini_tool and "google_search" in gemini_tool
    assert native_server_tool("deepseek") is None

    anthropic_all = native_server_tools("anthropic")
    types = {t.get("type") for t in anthropic_all}
    assert {"web_search_20250305", "web_fetch_20250910",
            "code_execution_20250825"} <= types
    # NO vendor computer-use protocol is declared: the local computer-use
    # ships as ordinary function tools for every model instead
    assert not any(str(t).startswith(("computer_", "bash_", "text_editor_"))
                   for t in types)
    gemini_all = native_server_tools("gemini")
    assert any("google_search" in t for t in gemini_all)
    assert any("url_context" in t for t in gemini_all)
    assert any("code_execution" in t for t in gemini_all)
    assert "thinking" in native_skills_for("anthropic")
    assert "thinking" in native_skills_for("gemini")
    assert "code_execution" not in native_skills_for("openai")
    assert not native_skills_for("deepseek")


def test_install_steps_back_local_for_every_capable_provider():
    expected = {
        "moonshot": ["web_search"],
        "openai": ["web_search"],
        "qwen": ["web_search"],
        "anthropic": ["web_search", "web_fetch"],
        "gemini": ["web_search", "web_fetch"],
    }
    for provider, retired in expected.items():
        registry = ToolRegistry(gate=None)
        stepped = install_native_capabilities(registry, provider)
        assert stepped == retired, provider
        for name in retired:
            assert name not in registry.tools, (provider, name)
        assert "$web_search" not in registry.tools or provider == "moonshot", (
            "only the client_tool mechanism needs the contract tool")
        # run_shell is never retired: server code_execution is a different sandbox
        assert "run_shell" in registry.tools, provider

    plain = ToolRegistry(gate=None)
    assert install_native_capabilities(plain, "deepseek") == []
    assert "web_search" in plain.tools
    assert install_native_capabilities(plain, "moonshot", enabled=False) == []


# ------------------------------------------------------- provider serialization

class _CaptureClient:
    """Records create() kwargs without any network."""

    class _Completions:
        def __init__(self, sink):
            self._sink = sink

        async def create(self, **kwargs):
            self._sink.update(kwargs)

            class _Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

            return _Stream()

    class _Chat:
        def __init__(self, sink):
            self.completions = _CaptureClient._Completions(sink)

    class _AsyncClient:
        def __init__(self, sink):
            self.chat = _CaptureClient._Chat(sink)

        async def aclose(self):
            pass

    def __init__(self):
        self.captured: dict = {}
        self.chat = _CaptureClient._Chat(self.captured)
        self.base_url = "https://api.example.com/v1"


@pytest.mark.parametrize("provider,params", [
    ("openai", {"web_search_options": {"search_context_size": "low"}}),
    ("qwen", {"enable_search": True}),
])
async def test_openai_compat_param_mechanism(provider, params):
    client = _CaptureClient()
    provider_obj = OpenAICompatProvider(
        f"{provider}/m", api_key="k", base_url="https://api.example.com/v1",
        client=client,
    )
    provider_obj.native_web_search = True
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider_obj.stream([{"role": "user", "content": "hi"}], tools):
        pass
    sent = client.captured
    for key, value in params.items():
        assert sent.get(key) == value, (provider, key)
    names = [t["function"]["name"] for t in sent["tools"]]
    assert "$web_search" not in names, "param providers never see $-tools"


async def test_openai_compat_off_sends_no_native_bits():
    client = _CaptureClient()
    provider_obj = OpenAICompatProvider(
        "openai/gpt-4o", api_key="k", base_url="https://api.example.com/v1",
        client=client,
    )
    provider_obj.native_web_search = False
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider_obj.stream([{"role": "user", "content": "hi"}], tools):
        pass
    assert "web_search_options" not in client.captured
    assert "enable_search" not in client.captured


def test_anthropic_serialization_passes_server_tools_through():
    normal = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    converted = _to_anthropic_tools([normal])
    assert converted[0]["name"] == "read_file"
    assert "input_schema" in converted[0]

    server_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    passthrough = _to_anthropic_tools([normal, server_tool])
    assert server_tool in passthrough, "server tools keep their native shape"


def test_gemini_serialization_keeps_grounding_shape():
    decls = _to_gemini_tools([{"type": "function",
                               "function": {"name": "read_file", "parameters": {}}}])
    assert decls == [{"functionDeclarations": [{"name": "read_file",
                                                "parameters": {"type": "object", "properties": {}},
                                                "description": ""}]}]
    grounding = {"google_search": {}}
    mixed = _to_gemini_tools([
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
        grounding,
        {"url_context": {}},
        {"code_execution": {}},
    ])
    assert any("google_search" in entry for entry in mixed)
    assert any("url_context" in entry for entry in mixed)
    assert any("code_execution" in entry for entry in mixed)
    assert mixed[0]["functionDeclarations"][0]["name"] == "read_file"


async def test_anthropic_declares_server_skills_thinking_and_beta_headers():
    import json

    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        captured["beta"] = request.headers.get("anthropic-beta", "")
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider("anthropic/claude-sonnet", api_key="sk", client=client)
    provider.native_skills = frozenset(
        {"web_search", "web_fetch", "code_execution", "thinking"})
    provider.native_web_search = True
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider.stream([{"role": "user", "content": "hi"}], tools):
        pass
    types = {t.get("type") for t in captured["body"]["tools"]}
    assert {"web_search_20250305", "web_fetch_20250910",
            "code_execution_20250825"} <= types
    assert captured["body"]["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert captured["body"]["temperature"] == 1
    assert "web-fetch-2025-09-10" in captured["beta"]
    assert "code-execution-2025-08-25" in captured["beta"]
    names = [t.get("name") for t in captured["body"]["tools"] if t.get("name") == "read_file"]
    assert names, "local tools still go out next to server tools"


async def test_gemini_declares_grounding_code_execution_and_thoughts():
    import json

    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiNativeProvider("gemini/gemini-flash", api_key="k", client=client)
    provider.native_skills = frozenset(
        {"web_search", "url_context", "code_execution", "thinking"})
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider.stream([{"role": "user", "content": "hi"}], tools):
        pass
    tools_sent = captured["body"]["tools"]
    assert any("google_search" in t for t in tools_sent)
    assert any("url_context" in t for t in tools_sent)
    assert any("code_execution" in t for t in tools_sent)
    decls = [t for t in tools_sent if "functionDeclarations" in t]
    assert decls and any(d["name"] == "read_file" for d in decls[0]["functionDeclarations"])
    thinking = captured["body"]["generationConfig"]["thinkingConfig"]
    assert thinking["includeThoughts"] is True


# ------------------------------------------------------ runtime flag bridge

async def test_runtime_flags_active_and_fallback_providers():
    cfg = default_config()
    cfg.default_provider = "moonshot"
    cfg.default_model = "moonshot/kimi-k2"
    cfg.providers["deepseek"] = default_config().providers["deepseek"]
    cfg.fallback_models = ["deepseek/deepseek-chat"]
    cfg.search.native = "auto"

    rt = build_runtime(cfg, __import__("pathlib").Path("."),
                       provider=MockProvider([]), tools=True)
    assert rt.provider is not None
    # fallback chain carries per-instance flags
    for candidate in (rt.provider, *rt.fallbacks):
        model_ref = getattr(candidate, "model_id", "") or "moonshot/kimi-k2"
        assert candidate.native_web_search == native_web_search_supported(model_ref)

    # opt-out kills every flag
    cfg.search.native = "off"
    rt2 = build_runtime(cfg, __import__("pathlib").Path("."),
                        provider=MockProvider([]), tools=True)
    assert not getattr(rt2.provider, "native_web_search", False)
    assert getattr(rt2.provider, "native_skills", frozenset()) == frozenset()
    assert "web_search" in rt2.registry.tools


def test_doctor_report_distinguishes_verified_from_spec_wired():
    report = capability_report("moonshot")
    assert report and "已真机验证" in report[0]
    spec = capability_report("gemini")
    assert spec and "按规范接线" in spec[0]
    assert capability_report("deepseek") == []


async def test_kimi_end_to_end_contract_tool_round_trip(workspace):
    from oaset.agent.runner import execute_prompt

    cfg = default_config()
    cfg.default_provider = "moonshot"
    cfg.default_model = "moonshot/kimi-k2"
    script = [
        MockTurn(content_chunks=["查\n"],
                 tool_calls=[MockToolCall("$web_search", {"query": "x"})]),
        MockTurn(content_chunks=["结论完成。"]),
    ]
    reply = await execute_prompt(cfg, workspace, "search",
                                 provider=MockProvider(script), yolo=True,
                                 with_mcp=False, persist=False)
    assert "结论" in reply
