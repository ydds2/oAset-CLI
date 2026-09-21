"""One-command adoption from the public MCP Registry (`oaset mcp add <name>`).

The registry (registry.modelcontextprotocol.io, ~10k servers) answers with
server metadata: a hosted URL ("remotes") and/or an installable package
("packages", e.g. npm). This module fetches that metadata and turns it into
an mcp.json entry — with the third-party boundary stated up front: adding a
registry server ADOPTS THIRD-PARTY CODE, and stdio entries download and run
someone else's package on first connect. The fetch is injectable so tests
stay hermetic.
"""

from __future__ import annotations

import json
from typing import Any

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"


def _entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        servers = payload.get("servers")
        if isinstance(servers, list):
            return [s for s in servers if isinstance(s, dict)]
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    return []


def _meta(entry: dict[str, Any]) -> dict[str, Any]:
    inner = entry.get("server")
    return inner if isinstance(inner, dict) else entry


def _first(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_registry_payload(payload: Any, name: str) -> list[dict[str, Any]]:
    """Registry payload -> [{name, description, url, package, transport}].

    Only entries whose server name contains the query (case-insensitive);
    exact matches first. URL transports win over packages (no install).
    """
    seen: dict[str, dict[str, Any]] = {}
    for entry in _entries(payload):
        meta = _meta(entry)
        server_name = _first(meta, "name") or _first(entry, "name")
        if not server_name:
            continue
        short = server_name.rsplit("/", 1)[-1]
        if name.lower() not in server_name.lower() and name.lower() != short.lower():
            continue
        url = _first(meta, "url")
        remotes = meta.get("remotes")
        if not url and isinstance(remotes, list) and remotes:
            first_remote = remotes[0]
            url = _first(first_remote, "url")
        package = ""
        packages = meta.get("packages")
        if isinstance(packages, list) and packages:
            first_package = packages[0]
            if isinstance(first_package, dict):
                package = _first(first_package, "identifier", "name", "package")
        transport = "http" if url else ("stdio" if package else "")
        if not transport:
            continue
        candidate = {
            "name": short,
            "full_name": server_name,
            "description": _first(meta, "description"),
            "url": url,
            "package": package,
            "transport": transport,
        }
        key = json.dumps(candidate, sort_keys=True)
        seen.setdefault(key, candidate)
    exact = [c for c in seen.values() if c["name"].lower() == name.lower()]
    others = [c for c in seen.values() if c["name"].lower() != name.lower()]
    return exact + others


async def search_registry(name: str, *, fetch: Any = None,
                          timeout: float = 10.0) -> list[dict[str, Any]]:
    """Query the official registry; empty list on any failure (fail-closed:
    an unreachable registry must not produce a half-guessed entry)."""
    if fetch is None:
        import httpx

        async def fetch(url: str) -> Any:
            async with httpx.AsyncClient(timeout=timeout,
                                         follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

    try:
        payload = await fetch(f"{REGISTRY_URL}?search={name}")
    except Exception:
        return []
    return parse_registry_payload(payload, name)


def to_server_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    """Registry candidate -> mcp.json entry (user-level: trusted by ownership)."""
    if candidate["transport"] == "http":
        return {"kind": "http", "url": candidate["url"]}
    return {"kind": "stdio", "command": "npx", "args": ["-y", candidate["package"]]}
