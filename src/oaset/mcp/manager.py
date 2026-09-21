"""MCP manager: loads mcp.json, starts every declared server, aggregates tools.

Trust model (mirrors plugins.py): servers declared in the USER config
(~/.oaset/mcp.json) are managed by the user directly and start by default.
Servers declared in a PROJECT config (<cwd>/.oaset/mcp.json) execute
third-party commands from whatever repo you happen to be in — they are
skipped until explicitly trusted:

    oaset mcp trust <name>

The trust record pins a sha256 digest of the server's config (command, args,
env, url, headers); editing the config invalidates trust until re-trusted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaset.i18n import t
from oaset.mcp.client import McpConnection, McpError, McpToolDef
from oaset.mcp.tools import McpToolAdapter


@dataclass
class ServerSpec:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    kind: str = "stdio"  # "stdio" | "http"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    startup_timeout_ms: int = 0
    enabled_tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    origin: str = "user"  # "user" | "project"
    inherit_env: bool = False
    trusted: bool = True  # project-origin servers start untrusted


def _load_trust_records(home: Path) -> dict[str, str]:
    path = home / "trusted_mcp.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_trust_records(home: Path, records: dict[str, str]) -> None:
    from oaset.utils import atomic_write_text, file_lock

    path = home / "trusted_mcp.json"
    # same lock rule as plugin trust: a lost update here silently re-prompts
    # for a server the user already trusted, or worse trusts the wrong digest
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=2))


def _trust_key(cwd: Path, name: str) -> str:
    return f"{os.path.normcase(str(cwd))}::{name}"


def server_digest(spec: ServerSpec) -> str:
    """Fingerprint of everything that makes a server declaration executable."""
    payload = json.dumps(
        {
            "command": spec.command,
            "args": spec.args,
            "env": spec.env,
            "url": spec.url,
            "headers": spec.headers,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_project_server_trusted(home: Path, cwd: Path, spec: ServerSpec) -> bool:
    record = _load_trust_records(home).get(_trust_key(cwd, spec.name))
    return record is not None and record == server_digest(spec)


def trust_project_server(home: Path, cwd: Path, name: str) -> str | None:
    """Pin a project server's config digest. Returns the digest, or None if
    no project server with that name exists."""
    spec = next(
        (
            s
            for s in load_server_specs(home, cwd=cwd, _check_trust=False)
            if s.name == name and s.origin == "project"
        ),
        None,
    )
    if spec is None:
        return None
    records = _load_trust_records(home)
    records[_trust_key(cwd, name)] = server_digest(spec)
    _save_trust_records(home, records)
    return records[_trust_key(cwd, name)]


def revoke_project_server(home: Path, cwd: Path, name: str) -> bool:
    records = _load_trust_records(home)
    key = _trust_key(cwd, name)
    if key not in records:
        return False
    del records[key]
    _save_trust_records(home, records)
    return True


def load_server_specs(
    home: Path, cwd: Path | None = None, *, _check_trust: bool = True
) -> list[ServerSpec]:
    """Read <home>/mcp.json and <cwd>/.oaset/mcp.json — {"mcpServers": {name: {...}}}.
    Project entries (cwd) win on name clash. cwd defaults to the process cwd.
    Project-origin specs are marked untrusted unless a matching trust record
    exists (pass _check_trust=False for the raw view, e.g. `oaset mcp trust`)."""
    specs: dict[str, ServerSpec] = {}
    search = [
        (Path(home), "user"),
        (Path(cwd) / ".oaset" if cwd is not None else Path.cwd() / ".oaset", "project"),
    ]
    for base, origin in search:
        path = base / "mcp.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = raw.get("mcpServers", raw.get("servers", {}))
        if not isinstance(servers, dict):
            continue
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            spec = _parse_spec(name, entry, origin)
            if spec is not None:
                if (
                    _check_trust
                    and origin == "project"
                    and cwd is not None
                    and not is_project_server_trusted(home, Path(cwd), spec)
                ):
                    spec.trusted = False
                specs[name] = spec
    return list(specs.values())


def _parse_spec(name: str, entry: dict[str, Any], origin: str = "user") -> ServerSpec | None:
    """One mcp.json entry: {"command":...} for stdio or {"url":...} for HTTP."""
    import os as _os

    if entry.get("url"):
        headers = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
        token_env = str(entry.get("bearerTokenEnvVar") or "")
        if token_env and _os.environ.get(token_env):
            headers["Authorization"] = f"Bearer {_os.environ[token_env]}"
        return ServerSpec(
            name=name,
            kind="http",
            url=str(entry["url"]),
            headers=headers,
            startup_timeout_ms=int(entry.get("startupTimeoutMs") or 0),
            enabled_tools=[str(t) for t in (entry.get("enabledTools") or [])],
            disabled_tools=[str(t) for t in (entry.get("disabledTools") or [])],
            origin=origin,
        )
    if entry.get("command"):
        args = entry.get("args", [])
        return ServerSpec(
            name=name,
            command=str(entry["command"]),
            args=[str(a) for a in args] if isinstance(args, list) else [],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            startup_timeout_ms=int(entry.get("startupTimeoutMs") or 0),
            enabled_tools=[str(t) for t in (entry.get("enabledTools") or [])],
            disabled_tools=[str(t) for t in (entry.get("disabledTools") or [])],
            origin=origin,
            inherit_env=bool(entry.get("inherit_env")),
        )
    return None


class McpManager:
    """Owns all MCP connections; produces Tool adapters for the registry."""

    def __init__(self, home: Path, cwd: Path | None = None, network_mode: str = "pull_only"):
        self.home = home
        self.cwd = cwd
        self.network_mode = network_mode
        self.connections: dict[str, McpConnection] = {}
        self.remote_tools: dict[str, list[McpToolDef]] = {}
        self.errors: dict[str, str] = {}
        self.untrusted: list[str] = []

    async def start_all(self, connect_timeout: float = 12.0) -> list[tuple[str, str | None]]:
        """Start every trusted server concurrently; returns [(name, error_or_None)].

        Servers used to start serially, so one wedged server added its full
        timeout to every other one's startup. `asyncio.gather` keeps the worst
        case at a single timeout for the whole batch (S1 in docs/PLAN.zh-CN.md).
        Failures stay isolated per server. Project-origin servers without a
        matching trust record are NOT spawned — they are reported via
        `self.untrusted` / status_line until `oaset mcp trust <name>`."""
        from oaset.mcp.http import filter_remote_tools

        async def _start(spec: Any) -> tuple[str, str | None]:
            conn: Any  # McpConnection | McpHttpConnection (duck-typed same surface)
            if spec.kind == "http":
                from oaset.mcp.http import McpHttpConnection

                conn = McpHttpConnection(
                    name=spec.name,
                    url=spec.url,
                    headers=spec.headers,
                    timeout=connect_timeout,
                    network_mode=self.network_mode,
                )
            else:
                conn = McpConnection(
                    name=spec.name,
                    command=spec.command,
                    args=spec.args,
                    env=spec.env,
                    timeout=connect_timeout,
                    inherit_env=spec.inherit_env,
                )
            try:
                await conn.start()
                tools = await conn.list_tools()
                tools = filter_remote_tools(tools, spec.enabled_tools, spec.disabled_tools)
                self.connections[spec.name] = conn
                self.remote_tools[spec.name] = tools
                return (spec.name, None)
            except (McpError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                self.errors[spec.name] = str(exc)
                return (spec.name, str(exc))

        specs = load_server_specs(self.home, self.cwd)
        trusted_specs = [s for s in specs if s.trusted]
        self.untrusted = [s.name for s in specs if not s.trusted]
        results = list(await asyncio.gather(*(_start(s) for s in trusted_specs)))
        for name in self.untrusted:
            results.append((name, None))  # skipped, not an error; surfaced via untrusted
        # Registration order follows the config file, not completion order, so
        # tool schemas reach the model deterministically.
        self.connections = {n: c for n, c in ((name, self.connections.get(name))
                                              for name, _ in results) if c is not None}
        self.remote_tools = {n: t for n, t in ((name, self.remote_tools.get(name))
                                               for name, _ in results) if t is not None}
        return results

    def adapters(self) -> list[McpToolAdapter]:
        adapters: list[McpToolAdapter] = []
        for server, tools in self.remote_tools.items():
            conn = self.connections[server]
            for tool in tools:
                adapters.append(McpToolAdapter(server=server, conn=conn, tool=tool))
        return adapters

    async def shutdown(self) -> None:
        for conn in self.connections.values():
            try:
                await conn.close()
            except Exception:
                pass
        self.connections.clear()
        self.remote_tools.clear()

    def status_line(self) -> str:
        if not self.connections and not self.errors and not self.untrusted:
            return "MCP: no servers configured (~/.oaset/mcp.json)"
        parts = [f"{s}:{len(t)} tools" for s, t in self.remote_tools.items()]
        for name, err in self.errors.items():
            parts.append(f"{name}: failed ({err[:60]})")
        if self.untrusted:
            parts.append(
                t("mcp_untrusted", names=", ".join(self.untrusted))
                + " (oaset mcp trust <name>)"
            )
        return "MCP: " + ", ".join(parts)
