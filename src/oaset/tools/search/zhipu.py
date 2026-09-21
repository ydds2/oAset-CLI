"""Zhipu web-search-pro backend — the provider-native search API, as an engine.

Reuses the user's EXISTING glm credential (the same key that powers the glm
model provider), so "compliant domestic search" is zero new signups for
anyone already running GLM. Works regardless of which model is active —
DeepSeek or Kimi sessions can search through it too.

Endpoint: POST https://open.bigmodel.cn/api/paas/v4/tools
Body: {"model": "web-search-pro", "messages": [...], "stream": false}
Results arrive as a tool_call whose function.arguments JSON carries
``search_result: [{title, content, link, ...}]``.
"""

from __future__ import annotations

import json
from typing import Any

from oaset.tools.search import (
    MAX_SNIPPET_CHARS,
    SearchBackend,
    SearchHit,
    _register,
    clean_text,
    site_of,
)

ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/tools"


def parse_zhipu_response(payload: Any, limit: int) -> list[SearchHit]:
    """Pure extraction (fixture-testable): tolerate both documented shapes."""
    hits: list[SearchHit] = []
    rows: list[dict] = []

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("search_result", "search_results") and isinstance(value, list):
                    rows.extend(v for v in value if isinstance(v, dict))
                else:
                    _collect(value)
        elif isinstance(node, list):
            for item in node:
                _collect(item)
        elif isinstance(node, str) and "search_result" in node:
            # tool_call arguments arrive as a JSON STRING, not an object
            try:
                _collect(json.loads(node))
            except (ValueError, TypeError):
                pass

    _collect(payload)
    for row in rows:
        url = str(row.get("link") or row.get("url") or row.get("icon") or "").strip()
        title = clean_text(str(row.get("title") or ""))
        snippet = clean_text(str(row.get("content") or ""))[:MAX_SNIPPET_CHARS]
        if not url or not title:
            continue
        hits.append(SearchHit(title=title, url=url, snippet=snippet, site=site_of(url)))
        if len(hits) >= limit:
            break
    return hits


def _glm_key() -> str:
    from oaset.credentials import load_credential

    key = load_credential("glm")
    return key or ""


@_register
class ZhipuBackend(SearchBackend):
    name = "zhipu"
    needs_key = True

    def available(self) -> bool:
        # available() runs during engine listing; a missing key just means
        # "not in the chain" — never an error
        try:
            return bool(_glm_key())
        except Exception:
            return False

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import os

        import httpx

        key = _glm_key() or os.environ.get("GLM_API_KEY", "")
        if not key:
            raise RuntimeError("zhipu search needs the glm credential (oaset login glm)")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": "web-search-pro",
                      "messages": [{"role": "user", "content": query}],
                      "stream": False},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"zhipu web-search-pro HTTP {response.status_code}")
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise RuntimeError("zhipu web-search-pro returned non-JSON") from exc
        # arguments may arrive as a JSON string inside the tool_call
        try:
            raw = json.dumps(payload, ensure_ascii=False)
            payload = json.loads(raw)
        except Exception:
            pass
        return parse_zhipu_response(payload, limit)
