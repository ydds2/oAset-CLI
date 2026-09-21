"""`oaset mcp add <name>` — one-command registry adoption, third-party warning
stated up front, hermetic via injected fetches."""

from __future__ import annotations

import json
from pathlib import Path

from oaset.mcp.registry import parse_registry_payload, search_registry

REGISTRY_PAYLOAD = {
    "servers": [
        {"server": {
            "name": "io.github.someone/server-time",
            "description": "current time tools",
            "packages": [{"registryType": "npm", "identifier": "mcp-server-time"}],
        }},
        {"server": {
            "name": "io.github.other/notes",
            "description": "notes search",
            "remotes": [{"url": "https://notes.example.com/mcp"}],
        }},
    ]
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_registry_parse_prefers_exact_and_url_transports():
    candidates = parse_registry_payload(REGISTRY_PAYLOAD, "notes")
    assert candidates[0]["name"] == "notes"
    assert candidates[0]["transport"] == "http"
    assert candidates[0]["url"] == "https://notes.example.com/mcp"

    stdio = parse_registry_payload(REGISTRY_PAYLOAD, "server-time")
    assert stdio[0]["transport"] == "stdio"
    assert stdio[0]["package"] == "mcp-server-time"


def test_registry_parse_no_match_is_empty():
    assert parse_registry_payload(REGISTRY_PAYLOAD, "nothing-matches") == []


async def test_search_registry_is_fail_closed_offline():
    async def boom(url):
        raise ConnectionError("registry unreachable")

    assert await search_registry("time", fetch=boom) == []


def test_mcp_add_writes_user_mcp_json_and_warns_about_third_party(
        isolated_home, monkeypatch, capsys):
    from oaset.cli import cmd_mcp

    async def fake_search(name, **kwargs):
        return parse_registry_payload(REGISTRY_PAYLOAD, name)

    monkeypatch.setattr("oaset.mcp.registry.search_registry", fake_search)
    monkeypatch.setattr("oaset.mcp.manager.load_server_specs", lambda *a, **k: [])
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: isolated_home / "ws"))

    rc = cmd_mcp(_ns(action="add", name="notes"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "notes.example.com" in out

    written = json.loads((isolated_home / "mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["notes"]["url"] == "https://notes.example.com/mcp"


def test_mcp_add_stdio_entry_warns_before_download(isolated_home, monkeypatch, capsys):
    from oaset.cli import cmd_mcp

    async def fake_search(name, **kwargs):
        return parse_registry_payload(REGISTRY_PAYLOAD, name)

    monkeypatch.setattr("oaset.mcp.registry.search_registry", fake_search)
    monkeypatch.setattr("oaset.mcp.manager.load_server_specs", lambda *a, **k: [])
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: isolated_home / "ws"))

    rc = cmd_mcp(_ns(action="add", name="server-time"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out or "警告" in out, "the third-party download must be stated"
    assert "mcp-server-time" in out

    written = json.loads((isolated_home / "mcp.json").read_text(encoding="utf-8"))
    entry = written["mcpServers"]["server-time"]
    assert entry["command"] == "npx" and entry["args"] == ["-y", "mcp-server-time"]


def _ns(**kwargs):
    import argparse

    defaults = {"action": "list", "name": None, "url": None}
    return argparse.Namespace(**{**defaults, **kwargs})


def test_cmd_shim_detection():
    """Windows CreateProcess cannot launch .cmd shims (npx/npm) directly; the
    spawn wrapper must recognise the common package runners."""
    from oaset.mcp.client import _needs_cmd_shim

    assert _needs_cmd_shim("npx") and _needs_cmd_shim("npx.cmd")
    assert _needs_cmd_shim("pnpm") and not _needs_cmd_shim("node")
    assert _needs_cmd_shim("C:/npm/npx") and not _needs_cmd_shim("python.exe")


def test_mcp_add_rejects_non_http_url(isolated_home, monkeypatch, capsys):
    """--url writes a server oAset will later dial; only http(s) qualifies."""
    from oaset.cli import cmd_mcp
    from oaset.i18n import t

    rc = cmd_mcp(_ns(action="add", name="x", url="file:///etc/passwd"))
    assert rc == 2
    assert t("mcp_add_url_bad", url="file:///etc/passwd") in capsys.readouterr().err
    assert not (isolated_home / "mcp.json").exists()
