"""MCP server administration for the TUI (`/mcp` management surface).

Pure functions over the two `mcp.json` locations (user home + project), so the
TUI flow stays thin and the rules are unit-testable without a display:

- server entries are validated before writing (name, transport, command/url);
- project scope wins on name clash at load time, so the editor exposes the
  scope explicitly instead of silently editing the wrong file;
- writes are atomic (tmp + replace) and preserve unrelated keys.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCOPES = ("project", "user")


class McpAdminError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ServerRow:
    name: str
    scope: str
    transport: str          # "stdio" | "http"
    target: str             # command line or URL
    enabled: bool
    tool_filter: str        # "enabled: a,b" / "disabled: c" / ""


def mcp_path(home: Path, cwd: Path, scope: str) -> Path:
    if scope == "project":
        return Path(cwd) / ".oaset" / "mcp.json"
    if scope == "user":
        return Path(home) / "mcp.json"
    raise McpAdminError("mcp.bad_scope", f"unknown scope {scope!r} (use project|user)")


def read_servers(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpAdminError("mcp.bad_json", f"{path}: {exc}") from exc
    servers = raw.get("mcpServers")
    if servers is None:
        servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        raise McpAdminError("mcp.bad_shape", f"{path}: mcpServers must be an object")
    return servers


def write_servers(path: Path, servers: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"mcpServers": servers}
    try:
        from oaset.utils import atomic_write_text, file_lock

        with file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as exc:
        # a locked/read-only mcp.json is routine on Windows (editor open):
        # surfaced as a notice, never a bare OSError into the @work worker
        raise McpAdminError("mcp.write_failed", f"{path}: {exc}") from exc


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise McpAdminError("mcp.name_required", "server name is required")
    if any(ch.isspace() for ch in name):
        raise McpAdminError("mcp.name_invalid", f"server name must not contain spaces: {name!r}")
    return name


def server_entry(*, url: str = "", command: str = "", args: list[str] | None = None,
                 env: dict[str, str] | None = None,
                 enabled_tools: list[str] | None = None,
                 disabled_tools: list[str] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Build one mcp.json entry; exactly one transport must be provided."""
    url, command = url.strip(), command.strip()
    if bool(url) == bool(command):
        raise McpAdminError(
            "mcp.transport_required",
            "provide exactly one of url (HTTP) or command (stdio)")
    if url and not url.startswith(("http://", "https://")):
        raise McpAdminError("mcp.bad_url", f"url must start with http(s):// — got {url!r}")
    entry: dict[str, Any] = {"url": url} if url else {
        "command": command, "args": [str(a) for a in (args or [])]}
    if env:
        entry["env"] = {str(k): str(v) for k, v in env.items()}
    if headers:
        entry["headers"] = {str(k): str(v) for k, v in headers.items()}
    if enabled_tools:
        entry["enabledTools"] = [str(t) for t in enabled_tools]
    if disabled_tools:
        entry["disabledTools"] = [str(t) for t in disabled_tools]
    return entry


def add_server(path: Path, name: str, entry: dict[str, Any], *,
               overwrite: bool = False) -> None:
    name = _validate_name(name)
    servers = read_servers(path)
    if name in servers and not overwrite:
        raise McpAdminError("mcp.duplicate", f"server already exists: {name}")
    servers[name] = entry
    write_servers(path, servers)


def set_enabled(path: Path, name: str, enabled: bool) -> None:
    servers = read_servers(path)
    if name not in servers:
        raise McpAdminError("mcp.unknown_server", f"no such server: {name}")
    entry = dict(servers[name])
    if enabled:
        entry.pop("enabled", None)   # absence means enabled (loader semantics)
    else:
        entry["enabled"] = False
    servers[name] = entry
    write_servers(path, servers)


def remove_server(path: Path, name: str) -> None:
    servers = read_servers(path)
    if name not in servers:
        raise McpAdminError("mcp.unknown_server", f"no such server: {name}")
    with contextlib.suppress(KeyError):
        del servers[name]
    write_servers(path, servers)


def list_servers(home: Path, cwd: Path) -> list[ServerRow]:
    """Merged view: project entries override user entries of the same name."""
    rows: dict[str, ServerRow] = {}
    for scope in ("user", "project"):  # project last: it wins on clash
        path = mcp_path(home, cwd, scope)
        try:
            servers = read_servers(path)
        except McpAdminError:
            continue
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("url"):
                transport, target = "http", str(entry["url"])
            else:
                command = str(entry.get("command", ""))
                args = " ".join(str(a) for a in (entry.get("args") or []))
                transport, target = "stdio", f"{command} {args}".strip()
            enabled_tools = [str(t) for t in (entry.get("enabledTools") or [])]
            disabled_tools = [str(t) for t in (entry.get("disabledTools") or [])]
            if enabled_tools:
                filt = "enabled: " + ",".join(enabled_tools)
            elif disabled_tools:
                filt = "disabled: " + ",".join(disabled_tools)
            else:
                filt = ""
            rows[name] = ServerRow(name=name, scope=scope, transport=transport,
                                   target=target,
                                   enabled=entry.get("enabled") is not False,
                                   tool_filter=filt)
    return sorted(rows.values(), key=lambda r: r.name)
