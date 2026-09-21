"""Baidu HTML backend — key-free, strong for Chinese / China-hosted queries.

Scrapes the desktop results page. Baidu wraps every outbound link in
``www.baidu.com/link?url=...`` redirects; the real target is usually echoed in
the container's ``mu`` attribute, which we prefer and fall back to the wrapper
link itself when absent (a baidu wrapper URL is still fetchable, just one hop
slower). Relative dates like "3天前" are NOT normalised into ``date`` — the
contract says an absent or relative date must not become a fabricated one.
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

ENDPOINT = "https://www.baidu.com/s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The OPEN TAG is part of the capture: `mu="real-url"` lives inside it, and
# slicing it off made the parser fall back to the baidu link wrapper for
# every result.
_CONTAINER_RE = re.compile(r'(<div[^>]+class="[^"]*c-container[^"]*"[^>]*>)(.*?)(?=<div[^>]+class="[^"]*c-container|\Z)',
                           re.S | re.I)
_TITLE_RE = re.compile(r"<h3[^>]*>.*?<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
_MU_RE = re.compile(r'\bmu="([^"]+)"', re.I)
_ABSTRACT_RE = re.compile(r'<span[^>]+class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
_CONTENT_RE = re.compile(r'<div[^>]+class="[^"]*c-span-last[^"]*"[^>]*>(.*?)</div>', re.S | re.I)


def parse_baidu_page(html_text: str, limit: int) -> list[SearchHit]:
    """Extract results from one Baidu SERP (pure function, fixture-testable)."""
    hits: list[SearchHit] = []
    for container in (_open_tag + body
                      for _open_tag, body in _CONTAINER_RE.findall(html_text)):
        title_match = _TITLE_RE.search(container)
        if not title_match:
            continue
        wrapper = clean_text(title_match.group(1))
        real = _MU_RE.search(container)
        url = sanitize_url(clean_text(real.group(1)) if real else wrapper)
        if not url.startswith(("http://", "https://")):
            continue
        title = clean_text(title_match.group(2))
        if not title:
            continue
        abstract = _ABSTRACT_RE.search(container) or _CONTENT_RE.search(container)
        snippet = clean_text(abstract.group(1)) if abstract else ""
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
class BaiduBackend(SearchBackend):
    name = "baidu"
    needs_key = False

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import httpx

        count = max(1, min(int(limit or 5), MAX_RESULTS))
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
            headers={"User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
            proxy=self.proxy or None,
        ) as client:
            response = await request_with_retry(
                client, ENDPOINT,
                params={"wd": query, "rn": min(count * 2, 20)})
        if response.status_code >= 400:
            raise RuntimeError(f"Baidu returned HTTP {response.status_code}")
        hits = parse_baidu_page(response.text, count)
        if not hits:
            raise RuntimeError("Baidu returned a page with no parseable results "
                               "(layout change or anti-bot wall)")
        return hits
