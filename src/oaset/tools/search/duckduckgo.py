"""DuckDuckGo HTML backend — key-free, strong for English-language queries.

Scrapes the ``html.duckduckgo.com/html`` endpoint (the same one open-webSearch
uses). Result links come wrapped in ``/l/?uddg=<urlencoded target>`` which we
decode back to the real URL; snippets sit in ``result__snippet`` anchors.
DDG's HTML endpoint provides no per-result dates, so ``date`` stays empty —
an absent date must never become a guessed one.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from oaset.tools.search import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    SearchBackend,
    SearchHit,
    _register,
    clean_text,
    request_with_retry,
    sanitize_url,
    site_of,
)

ENDPOINT = "https://html.duckduckgo.com/html/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_RESULT_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                        re.S | re.I)
_SNIPPET_RE = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                         re.S | re.I)


def unwrap_ddg(href: str) -> str:
    """Decode DDG's ``/l/?uddg=<urlencoded>`` redirect wrapper to the target."""
    if "uddg=" not in href:
        return href
    try:
        params = parse_qs(urlparse(href).query)
    except ValueError:
        return href
    target = (params.get("uddg") or [""])[0]
    return unquote(target) if target else href


def parse_ddg_page(html_text: str, limit: int) -> list[SearchHit]:
    """Extract results from one DDG HTML page (pure function, fixture-testable)."""
    hits: list[SearchHit] = []
    anchors = _RESULT_RE.findall(html_text)
    snippets = [clean_text(s) for s in _SNIPPET_RE.findall(html_text)]
    for index, (href, title_html) in enumerate(anchors):
        url = sanitize_url(unwrap_ddg(clean_text(href)))
        if not url.startswith(("http://", "https://")):
            continue
        title = clean_text(title_html)
        if not title:
            continue
        snippet = snippets[index] if index < len(snippets) else ""
        hits.append(SearchHit(
            title=title,
            url=url,
            snippet=snippet[:MAX_SNIPPET_CHARS],
            date="",
            site=site_of(url),
        ))
        if len(hits) >= max(1, min(limit, MAX_RESULTS)):
            break
    return hits


@_register
class DuckDuckGoBackend(SearchBackend):
    name = "duckduckgo"
    needs_key = False

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import httpx

        count = max(1, min(int(limit or 5), MAX_RESULTS))
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
            headers={"User-Agent": UA}, proxy=self.proxy or None,
        ) as client:
            response = await request_with_retry(
                client, ENDPOINT, params={"q": query, "kl": "wt-wt"})
        if response.status_code >= 400:
            raise RuntimeError(f"DuckDuckGo returned HTTP {response.status_code}")
        hits = parse_ddg_page(response.text, count)
        if not hits:
            raise RuntimeError("DuckDuckGo returned a page with no parseable results "
                               "(layout change or bot wall)")
        return hits
