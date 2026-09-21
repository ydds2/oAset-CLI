"""Brave Search HTML backend — key-free best-effort.

Scrapes the public results page. Brave's markup changes often and it serves
bot walls aggressively, so this backend is explicitly best-effort: any layout
drift surfaces as a per-engine failure note next to the other engines'
results, never as a dead query. Best used alongside an independent engine so
agreement can be judged.
"""

from __future__ import annotations

import re

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

ENDPOINT = "https://search.brave.com/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The negative lookahead (?!-) keeps compound classes like
# "snippet-description" from being treated as a NEW block boundary.
_BLOCK_RE = re.compile(r'<div[^>]+class="[^"]*(?:snippet|result)(?!-)[^"]*"[^>]*>(.*?)(?=<div[^>]+class="[^"]*(?:snippet|result)(?!-)|\Z)',
                       re.S | re.I)
_LINK_RE = re.compile(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DESC_RE = re.compile(r'<div[^>]+class="[^"]*(?:snippet-description|description|content)[^"]*"[^>]*>(.*?)</div>',
                      re.S | re.I)

_INTERNAL = ("brave.com/search", "brave.com/settings", "search.brave.com/help")


def parse_brave_page(html_text: str, limit: int) -> list[SearchHit]:
    """Extract results from one Brave SERP (pure function, fixture-testable).

    Skips brave-internal links; the first external link in a block is the
    result URL and its text is the title."""
    hits: list[SearchHit] = []
    for block in _BLOCK_RE.findall(html_text):
        url = ""
        title = ""
        for href, text in _LINK_RE.findall(block):
            candidate = sanitize_url(clean_text(href))
            if not candidate.startswith(("http://", "https://")):
                continue
            if any(host in candidate for host in _INTERNAL):
                continue
            url = candidate
            title = clean_text(text)
            break
        if not url or not title:
            continue
        desc = _DESC_RE.search(block)
        snippet = clean_text(desc.group(1)) if desc else ""
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
class BraveBackend(SearchBackend):
    name = "brave"
    needs_key = False

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import httpx

        count = max(1, min(int(limit or 5), MAX_RESULTS))
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
            headers={"User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
            proxy=self.proxy or None,
        ) as client:
            response = await request_with_retry(client, ENDPOINT, params={"q": query})
        if response.status_code >= 400:
            raise RuntimeError(f"Brave returned HTTP {response.status_code}")
        hits = parse_brave_page(response.text, count)
        if not hits:
            raise RuntimeError("Brave returned a page with no parseable results "
                               "(layout change or bot wall)")
        return hits
