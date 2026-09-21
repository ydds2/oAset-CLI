"""Lifecycle hooks.

Config (config.toml):
    [hooks]
    on_user_message = "echo new task >> ~/oaset-events.log"
    on_tool_start   = "notify-send $OASET_TOOL_NAME"
    on_tool_end     = "echo $OASET_TOOL_NAME finished >> hook.log"
    on_turn_end     = "say done"
    on_error        = "alert.sh"

HTTP actions: prefix the value with "http://"/"https://" and
the payload is POSTed as JSON instead of run as a shell command:
    on_turn_end = "https://collector.example.com/oaset"

Payload arrives via OASET_* environment variables (shell actions) or as a
JSON body {"event": ..., ...payload} (HTTP actions). Hook failures are
printed to stderr and never fatal.

Portability + injection safety: the documented examples use POSIX syntax
(`$OASET_TOOL_NAME`), which is not valid in PowerShell or cmd. Placeholders
are therefore rewritten to the detected shell's native environment-variable
reference (bash keeps `$OASET_X`, PowerShell gets `$env:OASET_X`, cmd gets
`%OASET_X%`) — the VALUES are never interpolated into the command string.
Hook payloads carry user messages and tool arguments; substituting them
verbatim would let payload content execute as shell syntax. Values travel
through the subprocess environment only (`env=`), so a message like
`x; rm -rf /` can never become a second command. A placeholder whose value
is absent from the environment is left untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Any

from oaset.utils import detect_shell, shell_command_line, truncate_text

EVENTS = (
    "on_user_message",
    "on_turn_start",
    "on_tool_start",
    "on_tool_end",
    "on_turn_end",
    "on_error",
)
HOOK_TIMEOUT = 5.0
PAYLOAD_LIMIT = 500

_PLACEHOLDER_RE = re.compile(r"\$\{?(OASET_[A-Z0-9_]+)\}?")


def render_hook_command(action: str, env: dict[str, str]) -> str:
    """Rewrite ``$OASET_*`` placeholders to the detected shell's native
    environment-variable reference. Values are NOT interpolated into the
    command string (they would execute as shell syntax); they reach the hook
    process through its environment instead."""
    shell = detect_shell()
    base = os.path.basename(shell).lower().removesuffix(".exe")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            return match.group(0)
        if base in ("powershell", "pwsh"):
            return f"$env:{name}"
        if base == "cmd":
            return f"%{name}%"
        return f"${name}"  # POSIX shells: the env reference already works

    return _PLACEHOLDER_RE.sub(replace, action)


def _is_http(action: str) -> bool:
    return action.startswith("http://") or action.startswith("https://")


class HookRunner:
    def __init__(self, hooks: dict[str, str], cwd: Path, network_mode: str = "pull_only"):
        self.actions = {k: v for k, v in (hooks or {}).items() if k in EVENTS and v}
        self.cwd = cwd
        self.network_mode = network_mode

    @property
    def active(self) -> bool:
        return bool(self.actions)

    @property
    def commands(self) -> dict[str, str]:
        """Back-compat view for callers that expect shell-only hooks."""
        return {k: v for k, v in self.actions.items() if not _is_http(v)}

    async def fire(self, event: str, **payload: Any) -> None:
        action = self.actions.get(event)
        if not action:
            return
        fields = {"event": event, **payload}
        if _is_http(action):
            await self._post(action, fields)
            return
        env = {**os.environ, "OASET_EVENT": event}
        for key, value in payload.items():
            env[f"OASET_{key.upper()}"] = truncate_text(str(value), PAYLOAD_LIMIT)
        command = render_hook_command(action, env)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_command_line(command),
                cwd=str(self.cwd),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await asyncio.wait_for(proc.wait(), timeout=HOOK_TIMEOUT)
        except TimeoutError:
            # The timeout must end the HOOK, not just stop waiting for it: the
            # child used to keep running (and a slow hook fired on every tool
            # call accumulated orphans), so kill the tree before moving on.
            from oaset.utils import kill_tree_async

            with contextlib.suppress(Exception):
                await kill_tree_async(proc)
        except asyncio.CancelledError:
            # A user interrupt must not be swallowed: kill the child, then let
            # the cancellation reach the agent loop.
            from oaset.utils import kill_tree_async

            with contextlib.suppress(Exception):
                await kill_tree_async(proc)
            raise
        except Exception:
            pass  # hooks must never break the agent loop

    async def _post(self, url: str, fields: dict[str, Any]) -> None:
        """HTTP hooks are PUSH-class, full stop: the payload carries the user's
        message and the assistant's answer, and the product promises that
        pull_only ("receive-only") never uploads conversation content anywhere.
        The URL being user-configured is consent to the DESTINATION, not to
        bypassing the mode — full mode is the opt-in that sends. Refusals are
        logged to stderr, never fatal.
        """
        import sys

        from oaset.i18n import t
        from oaset.network import NetworkPolicy

        policy = NetworkPolicy(self.network_mode)
        if not policy.allows("push"):
            print(t("hook_blocked_by_policy", url=url, mode=self.network_mode),
                  file=sys.stderr)
            return
        import httpx

        body = {k: truncate_text(str(v), PAYLOAD_LIMIT) for k, v in fields.items()}
        try:
            # One overall wall (not per-phase): httpx's timeout= applies to
            # connect/read/write separately, so a server trickling one byte
            # every few seconds used to hold the turn open indefinitely.
            async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
                await asyncio.wait_for(client.post(url, json=body), timeout=HOOK_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # never fatal
