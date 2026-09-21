"""Zero-config discovery of local model servers on loopback.

The integration contract here is "discover what already exists": if Ollama or
LM Studio is running on this machine, oAset says so — no download, no account,
no config file. Nothing is ever written by discovery; it only annotates
`/models list` and `oaset doctor`. Both probes are loopback-only, so they are
allowed under every network mode including local_only, and the fetch is
injectable so tests stay hermetic.
"""

from __future__ import annotations

LOCAL_SERVERS: tuple[tuple[str, str], ...] = (
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("lmstudio", "http://127.0.0.1:1234/v1"),
)


def _model_ids(payload: object) -> list[str]:
    """OpenAI-shape {data: [{id}]} — both Ollama and LM Studio serve it."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [str(item.get("id")) for item in data
            if isinstance(item, dict) and item.get("id")]


async def discover_local_servers(*, fetch=None, timeout: float = 0.5) -> list[dict]:
    """Probe the well-known loopback endpoints; return only servers that answer.

    Absence is normal, not an error — most machines run none of these. Returns
    [{"vendor", "base_url", "models": [id, ...]}] in the fixed probe order.
    """
    if fetch is None:
        async def fetch(url: str) -> object:
            import httpx

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

    found: list[dict] = []
    for vendor_id, base_url in LOCAL_SERVERS:
        try:
            payload = await fetch(base_url + "/models")
        except Exception:
            continue  # nothing listening there: quiet, expected
        models = _model_ids(payload)
        if models:
            found.append({"vendor": vendor_id, "base_url": base_url,
                          "models": models})
    return found


def discover_local_servers_sync(timeout: float = 0.5) -> list[dict]:
    """Sync twin for sync callers (oaset doctor)."""
    import httpx

    found: list[dict] = []
    for vendor_id, base_url in LOCAL_SERVERS:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(base_url + "/models")
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            continue
        models = _model_ids(payload)
        if models:
            found.append({"vendor": vendor_id, "base_url": base_url,
                          "models": models})
    return found
