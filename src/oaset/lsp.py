"""LSP client: stdio JSON-RPC with Content-Length framing.

Diagnostics loop: initialize -> textDocument/didOpen (didChange after writes)
-> publishDiagnostics collected and injected into tool results, plus the
`lsp_diagnostics` tool. Servers come from config.toml:

    [lsp]
    python = ["pylsp"]

Servers that fail to start are logged and skipped — LSP never blocks the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path

from oaset.tools.base import READ, Tool, ToolContext, ToolResult
from oaset.utils import oaset_home, one_line, short_path

DIAG_WAIT_SECONDS = 1.5
MAX_DIAGNOSTICS = 12


class LspError(Exception):
    pass


def _framed(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class LspConnection:
    """One language server over stdio with Content-Length framed JSON-RPC."""

    def __init__(self, name: str, command: list[str], cwd: Path | None = None):
        self.name = name
        self.command = command
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._buffer = b""
        self._diagnostics: dict[str, list[dict]] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._versions: dict[str, int] = {}
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.cwd) if self.cwd else None,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                break
            self._buffer += chunk
            while True:
                header_end = self._buffer.find(b"\r\n\r\n")
                if header_end < 0:
                    break
                header = self._buffer[:header_end]
                rest = self._buffer[header_end + 4:]
                match = re.search(rb"Content-Length:\s*(\d+)", header, re.IGNORECASE)
                if not match:
                    self._buffer = rest
                    continue
                length = int(match.group(1))
                if len(rest) < length:
                    break  # wait for the full body
                payload, self._buffer = rest[:length], rest[length:]
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        if request_id is not None and request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                if "error" in message:
                    future.set_exception(LspError(str(message["error"])))
                else:
                    future.set_result(message.get("result", {}))
            return
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params", {})
            self._diagnostics[params.get("uri", "")] = params.get("diagnostics", [])

    def _write(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspError(f"LSP server '{self.name}' is not running")
        self._proc.stdin.write(_framed(message))

    async def request(self, method: str, params: dict, timeout: float = 5.0) -> dict:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise LspError(f"LSP {self.name}: {method} timed out") from None

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def initialize(self) -> None:
        root = str(self.cwd or Path.home())
        await self.request(
            "initialize",
            {"processId": None, "rootUri": Path(root).as_uri(), "capabilities": {}},
        )
        self.notify("initialized", {})

    def uri_for(self, path: Path) -> str:
        return path.as_uri()

    async def did_open(self, path: Path, language_id: str = "python") -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": self.uri_for(path),
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

    async def did_change(self, path: Path, language_id: str = "python") -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        key = str(path)
        self._versions[key] = self._versions.get(key, 1) + 1
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": self.uri_for(path), "version": self._versions[key]},
                "contentChanges": [{"text": text}],
            },
        )

    def diagnostics_for(self, path: Path) -> list[dict]:
        return self._diagnostics.get(self.uri_for(path), [])

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self._proc.wait(), timeout=3)
        self._proc = None


class LspManager:
    """Owns configured servers; renders diagnostics for tool-result injection."""

    def __init__(self, servers: dict[str, list[str]], cwd: Path):
        self.cwd = cwd
        self.connections: dict[str, LspConnection] = {}
        self.config = servers or {}

    async def start_all(self) -> list[str]:
        started = []
        for language, command in self.config.items():
            if not command:
                continue
            conn = LspConnection(language, list(command), cwd=self.cwd)
            try:
                await conn.start()
                await conn.initialize()
                self.connections[language] = conn
                started.append(language)
            except (OSError, LspError):
                with contextlib.suppress(Exception):
                    await conn.close()
        return started

    def _conn_for(self, path: Path) -> LspConnection | None:
        suffix = path.suffix.lstrip(".").lower()
        aliases = {"py": "python", "js": "typescript", "ts": "typescript",
                   "tsx": "typescript", "jsx": "typescript"}
        return self.connections.get(aliases.get(suffix, suffix))

    async def refresh(self, path: Path, language_id: str = "python") -> str:
        """Open/notify, wait briefly for diagnostics, return an injectable summary."""
        conn = self._conn_for(path)
        if conn is None:
            return ""
        await conn.did_open(path)
        await conn.did_change(path, language_id)
        deadline = asyncio.get_running_loop().time() + DIAG_WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if conn.diagnostics_for(path):
                break
            await asyncio.sleep(0.05)
        diags = conn.diagnostics_for(path)
        if not diags:
            return ""
        lines = []
        for d in diags[:MAX_DIAGNOSTICS]:
            line_no = d.get("range", {}).get("start", {}).get("line", 0) + 1
            lines.append(f"  line {line_no}: {one_line(str(d.get('message', '')), 140)}")
        return f"[LSP] {short_path(path, self.cwd)}: {len(diags)} diagnostic(s)\n" + "\n".join(lines)

    async def shutdown(self) -> None:
        for conn in self.connections.values():
            with contextlib.suppress(Exception):
                await conn.close()
        self.connections.clear()


def load_lsp_config(home: Path | None = None) -> dict[str, list[str]]:
    """Read the [lsp] table from ~/.oaset/config.toml (values: argv lists)."""
    import tomllib

    path = (home or oaset_home()) / "config.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    table = raw.get("lsp", {})
    if not isinstance(table, dict):
        return {}
    return {
        str(lang): [str(part) for part in argv]
        for lang, argv in table.items()
        if isinstance(argv, list) and argv
    }


async def lsp_summary_for(path: Path, ctx: ToolContext) -> str:
    """Refresh diagnostics for `path` and return an injectable summary ('' if none)."""
    manager: LspManager | None = ctx.session_state.get("lsp_manager")
    if manager is None:
        return ""
    with contextlib.suppress(Exception):
        return await manager.refresh(path)
    return ""


class LspDiagnosticsTool(Tool):
    name = "lsp_diagnostics"
    description = (
        "Get language-server diagnostics (errors/warnings) for a file. Requires an LSP "
        "server in config.toml [lsp]; returns empty when none is available."
    )
    permission = READ
    required = ["path"]
    parameters = {"path": {"type": "string", "description": "File to check"}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = Path(args.get("path", ""))
        if not path.is_absolute():
            path = ctx.cwd / path
        manager: LspManager | None = ctx.session_state.get("lsp_manager")
        if manager is None or not manager.connections:
            return ToolResult("No LSP servers configured (config.toml [lsp]).", is_error=True)
        summary = await lsp_summary_for(path, ctx)
        return ToolResult(summary or f"No diagnostics for {path.name}.")
