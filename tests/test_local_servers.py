"""Zero-config local model server discovery (Ollama / LM Studio on loopback)."""

from __future__ import annotations

from oaset.local_servers import discover_local_servers, discover_local_servers_sync


async def test_discover_reports_only_responding_servers():
    async def fetch(url: str):
        if url.startswith("http://127.0.0.1:11434"):
            return {"data": [{"id": "qwen3:8b"}, {"id": "llama3"}]}
        raise ConnectionError(f"nothing listening at {url}")

    found = await discover_local_servers(fetch=fetch)
    assert [s["vendor"] for s in found] == ["ollama"]
    assert found[0]["models"] == ["qwen3:8b", "llama3"]
    assert found[0]["base_url"] == "http://127.0.0.1:11434/v1"


async def test_discover_is_quiet_when_nothing_listens():
    async def boom(url: str):
        raise ConnectionError("empty machine")

    assert await discover_local_servers(fetch=boom) == []


async def test_discover_ignores_malformed_payloads():
    async def weird(url: str):
        return {"data": "not-a-list"}

    assert await discover_local_servers(fetch=weird) == []


def test_sync_twin_injectable():
    def fetch(url: str):
        if "1234" in url:
            return {"data": [{"id": "fantasy-model"}]}
        raise ConnectionError()

    import httpx

    real_get = httpx.Client.get

    def patched(self, url, **kwargs):
        if url.endswith("/models"):
            try:
                payload = fetch(url)
            except Exception as exc:
                raise httpx.ConnectError(str(exc)) from exc
            _json_like = payload

            class FakeResp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return _json_like

            return FakeResp()
        return real_get(self, url, **kwargs)

    httpx.Client.get = patched
    try:
        found = discover_local_servers_sync()
    finally:
        httpx.Client.get = real_get
    assert [s["vendor"] for s in found] == ["lmstudio"]


def test_models_list_annotates_a_detected_server(isolated_home, monkeypatch, capsys):
    # sync on purpose: cmd_models bridges to a fresh loop via arun, which is
    # forbidden inside a running one (pytest-asyncio would trip exactly that)
    """`oaset models list` is where a zero-config user meets discovery: the
    server that is ALREADY running must be named, with the one command that
    adopts it."""
    from oaset import cli as climod

    async def fake_discover(*, fetch=None, timeout=0.5):
        return [{"vendor": "ollama", "base_url": "http://127.0.0.1:11434/v1",
                 "models": ["qwen3:8b"]}]

    monkeypatch.setattr("oaset.local_servers.discover_local_servers", fake_discover)
    assert climod.cmd_models(_ns_like()) == 0
    out = capsys.readouterr().out
    assert "ollama" in out and "11434" in out and "/models add" in out


def _ns_like():
    import argparse

    return argparse.Namespace(json=False)
