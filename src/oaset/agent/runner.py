"""Headless prompt execution shared by the gateway, cron daemon, and tests.

Always goes through :class:`oaset.host.SessionHost` so TUI / SDK / desktop
skins and headless jobs share the same kernel, persistence and MCP lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from oaset.config import AppConfig
from oaset.host import SessionHost
from oaset.utils import extract_image_attachments


async def execute_prompt(
    cfg: AppConfig,
    cwd: Path,
    text: str,
    model_id: str | None = None,
    provider: Any | None = None,
    yolo: bool = False,
    with_mcp: bool = True,
    with_tools: bool = True,
    on_event=None,
    persist: bool = True,
) -> str:
    """Run one prompt through SessionHost; return the final answer text."""
    from oaset.providers.base import ProviderError

    host = SessionHost(
        cfg, cwd, model_id=model_id, provider=provider, yolo=yolo,
        tools=with_tools, mcp=with_mcp, persist=persist,
        # the CLI owns the one-shot usage write (ordered with its own message
        # append); a door write here would double every resume usage line
        persist_usage=False,
    )
    images = extract_image_attachments(
        text, cwd, enabled=host.model_cfg.has("image_in")
    )
    try:
        async for event in host.chat(text, images=images or None):
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    pass
            if event.type == "turn_completed":
                return str(event.data.get("content") or "")
        return ""
    except ProviderError:
        raise
    finally:
        await host.aclose()
