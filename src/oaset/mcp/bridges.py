"""Approval-gated bridges for MCP elicitation/create and roots/list.

Same injection pattern as the sampling bridge: the client calls the handler;
the handler goes through the unified PermissionGate first, then (for
elicitation) collects one field per requestedSchema property through a TUI
inline input. User cancel maps to the MCP "cancel" action; gate denial maps
to None → the client answers -32002 denied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def make_elicitation_bridge(gate: Any, field_input: Any, log: Any = None):
    """Build an elicitation_handler.

    gate: unified PermissionGate (UIGate asks, AutoGate allows, ReadOnlyGate
    denies). field_input: async (title, placeholder) -> str | None — the TUI
    mounts an InlineInput; None means the user cancelled the field.
    """

    async def handler(params: dict[str, Any]) -> dict[str, Any] | None:
        message = str(params.get("message", ""))
        schema = params.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        decision = await gate.request(
            "mcp_elicitation", "exec",
            f"MCP server requests input: {message}"[:200],
            preview=json.dumps(schema, ensure_ascii=False)[:300],
        )
        if not decision or str(decision).lower() in ("deny", "denied"):
            if log:
                log("elicitation denied by gate")
            return None

        content: dict[str, Any] = {}
        for name, field_spec in properties.items():
            field_spec = field_spec if isinstance(field_spec, dict) else {}
            title = str(field_spec.get("title") or name)
            if name not in required and not str(field_spec.get("type", "string")):
                continue
            value = await field_input(title, f"{name} ({field_spec.get('type', 'string')})")
            if value is None:
                if name in required:
                    if log:
                        log(f"elicitation field {name!r} cancelled by user")
                    return {"action": "cancel"}
                continue  # optional field left out
            if str(field_spec.get("type", "string")) == "boolean":
                content[name] = str(value).strip().lower() in ("1", "true", "y", "yes", "是")
            elif str(field_spec.get("type", "string")) in ("integer", "number"):
                try:
                    content[name] = int(value) if field_spec.get("type") == "integer" else float(value)
                except ValueError:
                    content[name] = value  # let the server validate
            else:
                content[name] = value
        return {"action": "accept", "content": content}

    return handler


def make_roots_handler(gate: Any, cwd: Any, log: Any = None,
                       extra_roots: list[Path] | None = None):
    """Build a roots_handler exposing the workspace root plus optional extra
    roots (multi-root support).

    Revealing workspace paths to a server is information disclosure, so the
    read is gated (ReadOnlyGate denies in headless — conservative default).
    """
    resolved_roots = [Path(cwd).resolve()]
    for extra in (extra_roots or []):
        resolved_roots.append(Path(extra).resolve())

    async def handler(params: dict[str, Any]) -> dict[str, Any] | None:
        decision = await gate.request(
            "mcp_roots", "read",
            "MCP server requests workspace roots: exposing "
            + ", ".join(str(r) for r in resolved_roots),
            preview="\n".join(str(r) for r in resolved_roots),
        )
        if not decision or str(decision).lower() in ("deny", "denied"):
            if log:
                log("roots request denied by gate")
            return None
        return {"roots": [{"uri": root.as_uri(), "name": root.name}
                          for root in resolved_roots]}

    return handler
