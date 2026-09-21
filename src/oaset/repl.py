"""Line-oriented REPL: the accessibility surface.

`oaset --plain` runs the SAME SessionHost runtime as the TUI, but every line
of output is plain, append-only text — no ANSI cursor movement, no repainting,
no spinners. Screen readers read TUI frames as jumping fragments; they read
this mode the way they read a normal terminal program.

Also the right surface for dumb terminals, slow SSH links and piping into
other tools. Permissions are asked as readable one-line prompts (y/a/n),
sessions persist to JSONL exactly like the TUI's.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any, Callable

from oaset.config import AppConfig
from oaset.i18n import t


class PlainGate:
    """PermissionGate that asks on stdin — readable, one line at a time."""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn
        self._session_allowed: set[str] = set()

    async def request(self, tool_name: str, level: str, summary: str,
                      preview: str | None = None) -> str:
        if tool_name in self._session_allowed:
            return "allow"
        print(t("repl_permission", tool=tool_name, level=level, summary=summary),
              flush=True)
        if preview:
            for line in preview.splitlines()[:8]:
                print(f"  {line}", flush=True)
        try:
            answer = self._input(t("repl_permission_answer") + " ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "deny"  # nobody to ask: fail closed
        if answer in ("a", "always"):
            self._session_allowed.add(tool_name)
            return "always"
        if answer in ("y", "yes"):
            return "allow"
        return "deny"


class PlainRepl:
    def __init__(
        self,
        cfg: AppConfig,
        cwd: Path,
        model_id: str | None = None,
        yolo: bool = False,
        provider: Any | None = None,
        input_fn: Callable[[str], str] | None = None,
        write: Callable[[str], None] | None = None,
        thinking_level: str = "",
    ) -> None:
        self.cfg = cfg
        self.cwd = Path(cwd)
        self.model_id = model_id
        self.yolo = yolo
        self.provider = provider
        self._startup_thinking = str(thinking_level or "").strip().lower()
        self._input = input_fn or input
        self._write = write or (lambda s: (sys.stdout.write(s), sys.stdout.flush()))
        self._failures = 0

    def _auto(self) -> bool:
        """--yolo OR a persisted permission_mode=auto (/yolo in the TUI):
        the same config must not gate differently per surface."""
        return bool(self.yolo) or getattr(self.cfg, "permission_mode", "") == "auto"

    async def run(self) -> int:
        from oaset.host import SessionHost
        from oaset.tools import AutoGate

        host = SessionHost(
            self.cfg, self.cwd, model_id=self.model_id, provider=self.provider,
            gate=AutoGate() if self._auto() else PlainGate(self._input),
            mode="auto" if self._auto() else "default", persist=True,
        )
        if self._startup_thinking:
            from oaset.thinking import normalize_level

            _level = normalize_level(self._startup_thinking)
            if _level:
                host.registry.ctx.session_state["thinking_level"] = _level
                host.registry.ctx.session_state["thinking_level_set"] = True
        self._out(t("repl_banner", model=host.model_cfg.id, cwd=str(self.cwd)))
        self._out(t("repl_hint"))
        try:
            while True:
                try:
                    line = await asyncio.to_thread(self._input, "❯ ")
                except EOFError:
                    break
                except KeyboardInterrupt:
                    break
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if line.strip().lower() in ("/exit", "/quit", "/q", "exit", "q"):
                    break
                if line.startswith("/"):
                    self._out(t("repl_unknown_cmd", cmd=line))
                    continue
                await self._chat(host, line)
        finally:
            with contextlib.suppress(Exception):
                await host.aclose()
        self._out(t("repl_bye"))
        # CI/scripts branch on the exit code: a session where every turn
        # failed must not look like success
        return 1 if self._failures else 0

    async def _chat(self, host, line: str) -> None:
        stream = host.chat(line)
        try:
            async for event in stream:
                self._render(event)
        except KeyboardInterrupt:
            self._out("\n" + t("repl_interrupted"))
            with contextlib.suppress(Exception):
                await stream.aclose()
        except Exception as exc:  # provider/loop failure: report, keep the REPL alive
            self._failures += 1
            message = getattr(exc, "message", str(exc))
            self._out("\n" + t("repl_error", error=message))

    def _render(self, event: Any) -> None:
        kind = getattr(event, "type", None) or (
            event.get("type") if isinstance(event, dict) else None)
        data = getattr(event, "data", None) or (event if isinstance(event, dict) else {})
        if kind == "assistant_delta":
            self._out(str(data.get("text", "")))
        elif kind == "tool_call_started":
            self._out("\n" + t("repl_tool", name=data.get("name", "?"),
                               summary=data.get("summary", "")))
        elif kind == "tool_completed":
            mark = "✗" if data.get("is_error") else "✓"
            self._out(t("repl_tool_done", mark=mark, name=data.get("name", "?")))
        elif kind == "tool_denied":
            self._out(t("repl_tool_denied"))
        elif kind == "notice":
            self._out("\n" + t("repl_notice", text=data.get("text", "")))
        elif kind == "turn_completed":
            usage = data.get("usage") or {}
            total = usage.get("total_tokens")
            self._out("\n" + (t("repl_turn_done", tokens=total) if total else "") + "\n")
        elif kind == "error":
            error = data.get("error")
            message = getattr(error, "message", str(error)) if error is not None else "?"
            self._out("\n" + t("repl_error", error=message))
        # reasoning_delta / lifecycle events stay silent: append-only output
        # for screen readers must not gain chatter the TUI hides by default

    def _out(self, text: str) -> None:
        self._write(text + "\n" if not text.endswith("\n") else text)
