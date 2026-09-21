"""Resolve a model's context / output limits instead of asking the user.

Priority:
  1. the provider's own model listing (`GET {base_url}/models`) — accurate and
     works for relays/中转站 too;
  2. the built-in vendor defaults (well-known per vendor);
  3. generic fallbacks.

Every network step is best-effort: a failure returns empty and the caller falls
back, because a model's token limits must never block onboarding.
"""

from __future__ import annotations

from typing import Any

GENERIC_CONTEXT = 128000
GENERIC_MAX_OUTPUT = 32768

# field names seen across OpenAI-compatible gateways, OpenRouter, local servers
CONTEXT_FIELDS = ("context_length", "context_window", "max_context_length",
                  "max_model_len", "max_input_tokens", "n_ctx", "context_size")
OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_output",
                 "max_tokens", "n_predict", "output_tokens")


def _as_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _pick(entry: dict[str, Any], fields: tuple[str, ...]) -> int | None:
    for field in fields:
        if field in entry:
            found = _as_positive_int(entry[field])
            if found:
                return found
    return None


def parse_limits(entry: dict[str, Any], *, context: int = 0,
                 max_output: int = 0) -> dict[str, int]:
    """Extract limits from one model entry, honouring nested provider blocks."""
    nested = entry.get("top_provider") or entry.get("provider") or {}
    result: dict[str, int] = {}
    context = _pick(entry, CONTEXT_FIELDS) or _pick(nested, CONTEXT_FIELDS) or context
    max_output = _pick(entry, OUTPUT_FIELDS) or _pick(nested, OUTPUT_FIELDS) or max_output
    if context:
        result["context"] = context
    if max_output:
        result["max_output"] = max_output
    return result


def find_model_entry(payload: Any, model_id: str) -> dict[str, Any] | None:
    """Locate a model in an OpenAI-style /models payload (tolerates variants)."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if rows is None:
        rows = payload.get("models")
    if not isinstance(rows, list):
        return None
    wanted = model_id.split("/")[-1].lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("id") or row.get("name") or row.get("model") or "").lower()
        if name == model_id.lower() or name == wanted or name.endswith(f"/{wanted}"):
            return row
    return None


def parse_models_payload(payload: Any, model_id: str) -> dict[str, int]:
    """limits for `model_id` from a /models response ({} when unknown)."""
    entry = find_model_entry(payload, model_id)
    if entry is None:
        return {}
    return parse_limits(entry)


async def discover_limits(base_url: str, api_key: str, model_id: str, *,
                          api_format: str = "openai", timeout: float = 6.0,
                          network_mode: str = "pull_only", fetch: Any = None) -> dict[str, int]:
    """Ask the provider for its model list and read the limits if offered.

    Returns {} on any failure (offline, no listing endpoint, unknown fields) —
    callers fall back to vendor/generic defaults rather than blocking.
    """
    if api_format != "openai" or not base_url:
        return {}
    from oaset.network import NetworkPolicy

    try:
        NetworkPolicy(mode=network_mode).assert_allowed("pull")
    except Exception:
        return {}
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        if fetch is not None:
            response = await fetch(url, headers)
            payload = response if isinstance(response, (dict, list)) \
                else getattr(response, "json", lambda: None)()
        else:
            import httpx

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
                payload = response.json()
    except Exception:
        return {}
    return parse_models_payload(payload, model_id)


def resolve_limits(*, discovered: dict[str, int] | None = None,
                   vendor: tuple[int, int] | None = None) -> tuple[int, int, str]:
    """(context, max_output, source) by priority: provider → vendor → generic."""
    discovered = discovered or {}
    if discovered.get("context") or discovered.get("max_output"):
        vendor_context = vendor[0] if vendor else 0
        vendor_output = vendor[1] if vendor else 0
        return (discovered.get("context") or vendor_context or GENERIC_CONTEXT,
                discovered.get("max_output") or vendor_output or GENERIC_MAX_OUTPUT,
                "provider")
    if vendor and (vendor[0] or vendor[1]):
        return (vendor[0] or GENERIC_CONTEXT, vendor[1] or GENERIC_MAX_OUTPUT, "vendor")
    return (GENERIC_CONTEXT, GENERIC_MAX_OUTPUT, "default")
