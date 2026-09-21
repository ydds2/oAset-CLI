"""Discover models on loopback OpenAI-compatible servers (Ollama, LM Studio).

Static config still has placeholder ids (`ollama/llama`). This module asks
``GET {base_url}/models`` and merges real tags into ``cfg.models`` in memory.
Failures are silent: a stopped local server must not block startup.
"""

from __future__ import annotations

from typing import Any

from oaset.config import AppConfig, ModelConfig, resolve_api_key
from oaset.network import is_loopback_url

DISCOVER_TIMEOUT = 2.0


def parse_model_ids(payload: Any) -> list[str]:
    """OpenAI-style ``{data: [{id: ...}]}`` (also ``models`` / bare name)."""
    rows: Any
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("models")
        if not isinstance(rows, list):
            return []
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            name = row
        elif isinstance(row, dict):
            name = str(row.get("id") or row.get("name") or row.get("model") or "")
        else:
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


async def list_remote_models(
    base_url: str,
    api_key: str = "",
    *,
    timeout: float = DISCOVER_TIMEOUT,
    network_mode: str = "pull_only",
    fetch: Any = None,
) -> list[str]:
    """Best-effort ``GET /models``. Empty on any failure."""
    if not base_url:
        return []
    from oaset.network import NetworkPolicy

    try:
        NetworkPolicy(mode=network_mode).assert_allowed("model_call", endpoint=base_url)
    except Exception:
        return []
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
                if response.status_code >= 400:
                    return []
                payload = response.json()
    except Exception:
        return []
    return parse_model_ids(payload)


def merge_discovered_models(cfg: AppConfig, provider: str, names: list[str]) -> list[str]:
    """Add ``provider/<name>`` entries that are not already configured. Returns new ids."""
    pcfg = cfg.providers.get(provider)
    if pcfg is None:
        return []
    added: list[str] = []
    for name in names:
        slug = name.strip().strip("/")
        if not slug:
            continue
        mid = f"{provider}/{slug}"
        if mid in cfg.models:
            continue
        cfg.models[mid] = ModelConfig(
            id=mid,
            provider=provider,
            model=slug,
            max_context_size=128000,
            capabilities=["tool_use"],
            display_name=f"{pcfg.name} {slug}",
        )
        added.append(mid)
    return added


async def refresh_local_models(
    cfg: AppConfig,
    *,
    network_mode: str | None = None,
    fetch: Any = None,
    timeout: float = DISCOVER_TIMEOUT,
) -> list[str]:
    """Probe every loopback provider; merge discovered tags. Returns new model ids."""
    mode = network_mode or cfg.network_mode
    added: list[str] = []
    for name, pcfg in cfg.providers.items():
        if not is_loopback_url(pcfg.base_url):
            continue
        names = await list_remote_models(
            pcfg.base_url,
            resolve_api_key(pcfg.api_key),
            timeout=timeout,
            network_mode=mode,
            fetch=fetch,
        )
        added.extend(merge_discovered_models(cfg, name, names))
    return added
