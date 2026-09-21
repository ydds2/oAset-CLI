"""SearXNG backend — self-hosted meta-search, no API key, egress stays yours.

SearXNG's JSON API aggregates its configured engines and returns a stable
schema, which makes it the best "I want search I control" option for a
local-first product. It is only used when ``SEARXNG_URL`` is set (or the
``[search] searxng_url`` config key), so the default install stays zero-config.
"""

from __future__ import annotations

from oaset.tools.search import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    SearchBackend,
    SearchHit,
    _register,
    clean_text,
    site_of,
)

UA = "oaset-cli/0.10.0 (+https://github.com/ydds2/oAset-CLI)"


@_register
class SearxngBackend(SearchBackend):
    name = "searxng"
    needs_key = False

    def __init__(self, base_url: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")

    def available(self) -> bool:
        return bool(self.base_url)

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import httpx

        if not self.base_url:
            raise RuntimeError(
                "SearXNG is not configured. Set [search] searxng_url or the "
                "SEARXNG_URL environment variable to your instance URL."
            )
        count = max(1, min(int(limit or 5), MAX_RESULTS))
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": UA, "Accept": "application/json"},
        ) as client:
            response = await client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "pageno": 1},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"SearXNG returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "SearXNG did not return JSON — enable the json format in its "
                "settings.yml (search.formats: [html, json])."
            ) from exc
        hits: list[SearchHit] = []
        for row in (payload.get("results") or [])[:count]:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            hits.append(SearchHit(
                title=clean_text(str(row.get("title") or "")) or site_of(url),
                url=url,
                snippet=clean_text(str(row.get("content") or ""))[:MAX_SNIPPET_CHARS],
                date=clean_text(str(row.get("publishedDate") or ""))[:32],
                site=site_of(url),
            ))
        return hits
