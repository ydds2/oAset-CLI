"""Egress gates for previously-ungated outbound paths (NET-01).

- HTTP MCP servers under local_only must refuse to start (stdio stays allowed).
- HTTP hooks under pull_only must skip the POST (PUSH requires full mode).
"""

from __future__ import annotations

import pytest

from oaset.hooks import HookRunner
from oaset.mcp.http import McpHttpConnection


async def test_http_mcp_refuses_to_start_under_local_only():
    conn = McpHttpConnection(
        name="blocked", url="http://mcp.test/mcp", network_mode="local_only")

    class ExplodingClient:
        async def post(self, *a, **k):  # would fail the test if reached
            raise AssertionError("local_only must not issue an HTTP MCP request")

    conn.client = ExplodingClient()
    from oaset.mcp.client import McpError

    with pytest.raises(McpError) as excinfo:
        await conn.start()
    assert "local_only" in str(excinfo.value)


async def test_http_hook_skips_post_under_local_only(tmp_path, capsys, monkeypatch):
    posted: list[str] = []

    class RecordingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            posted.append(url)

    fake_httpx = type("H", (), {"AsyncClient": staticmethod(lambda **kw: RecordingClient())})
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    runner = HookRunner({"on_turn_end": "https://collector.example.com/oaset"},
                        tmp_path, network_mode="local_only")
    await runner.fire("on_turn_end", text="secret")
    assert posted == [], "local_only must not POST hook payloads"
    err = capsys.readouterr().err
    # language-agnostic: the URL must be named, and the [hook] tag present
    assert "collector.example.com" in err and "[hook]" in err


async def test_http_hook_posts_under_full_mode(tmp_path, monkeypatch):
    posted: list[str] = []

    class RecordingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            posted.append(url)

    fake_httpx = type("H", (), {"AsyncClient": staticmethod(lambda **kw: RecordingClient())})
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    runner = HookRunner({"on_turn_end": "https://collector.example.com/oaset"},
                        tmp_path, network_mode="full")
    await runner.fire("on_turn_end", text="ok")
    assert posted == ["https://collector.example.com/oaset"]


# ------------------------------------------------- the suite's own egress gate


def test_suite_guard_blocks_live_egress_and_allows_mocks():
    """conftest.no_live_http keeps the suite hermetic; a guard that silently
    stopped working would let a test depend on the internet again (the model
    wizard used to, at a 6s cost per attempt)."""
    import httpx
    from conftest import LiveNetworkBlocked

    with pytest.raises(LiveNetworkBlocked):
        httpx.get("https://api.openrouter.ai/api/v1/models", timeout=1)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://any-vendor.example/v1/models").status_code == 200


async def test_suite_guard_blocks_live_egress_on_the_async_path():
    import httpx
    from conftest import LiveNetworkBlocked

    async with httpx.AsyncClient(timeout=1) as client:
        with pytest.raises(LiveNetworkBlocked):
            await client.get("https://api.openrouter.ai/api/v1/models")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://any-vendor.example/v1/models")).status_code == 200


async def test_http_hook_is_push_class_blocked_under_default_pull_only(tmp_path, capsys):
    """HTTP hooks POST the user's message and the turn answer — that is
    uploading conversation content, which the receive-only default promises
    never to do. The URL being configured is consent to the destination, not
    to the egress class; full mode is the opt-in."""
    runner = HookRunner({"on_turn_end": "https://collector.example.com/oaset"},
                        tmp_path, network_mode="pull_only")
    await runner.fire("on_turn_end", text="secret content")
    err = capsys.readouterr().err
    assert "collector.example.com" in err and "[hook]" in err
