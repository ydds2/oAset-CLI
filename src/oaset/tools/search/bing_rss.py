"""Bing RSS search backend — the zero-config default (no API key required).

Bing's HTML endpoint has an undocumented ``?format=rss`` output that works
without registration. It is a fallback, not a contract: the endpoint is not
part of any published API, so the parser is written defensively and every
other backend exists precisely because this one can disappear.

The previous implementation matched ``<title>``/``<link>``/``<description>``
with three independent regexes and paired them by index; when one item lacked a
description the whole list shifted and snippets were attached to the wrong
titles. Parsing per ``<item>`` block makes that class of corruption impossible.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from oaset.tools.search import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    SearchBackend,
    SearchHit,
    _register,
    clean_text,
    site_of,
    unwrap_redirect,
)

ENDPOINT = "https://www.bing.com/search"
UA = "oaset-cli/0.10.0 (+https://github.com/ydds2/oAset-CLI)"

_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.S | re.I)
_FIELD_RE = {
    name: re.compile(rf"<{name}\b[^>]*>(.*?)</{name}>", re.S | re.I)
    for name in ("title", "link", "description", "guid", "pubDate", "date")
}
_NEWS_SOURCE_RE = re.compile(r"<News:Source\b[^>]*>(.*?)</News:Source>", re.S | re.I)

# Formats Bing/upstream feeds actually emit, most specific first.
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%b %d, %Y",
)


def parse_date(raw: str) -> str:
    """Normalise a feed date to ``YYYY-MM-DD`` (or ``YYYY-MM-DD HH:MM``).

    Returns "" when nothing parses — an absent date must never become a wrong
    one, because the model uses this field to judge freshness.
    """
    text = clean_text(raw)
    if not text:
        return ""
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def _field(block: str, name: str) -> str:
    match = _FIELD_RE[name].search(block)
    return match.group(1) if match else ""


def parse_rss(xml_text: str, limit: int) -> list[SearchHit]:
    """Parse a Bing RSS document into normalised hits (pure, unit-testable)."""
    hits: list[SearchHit] = []
    for block in _ITEM_RE.findall(xml_text or ""):
        title = clean_text(_field(block, "title"))
        link = clean_text(_field(block, "link")) or clean_text(_field(block, "guid"))
        if not link:
            continue
        url = unwrap_redirect(link)
        snippet = clean_text(_field(block, "description"))[:MAX_SNIPPET_CHARS]
        date = parse_date(_field(block, "pubDate")) or parse_date(_field(block, "date"))
        source_match = _NEWS_SOURCE_RE.search(block)
        source = clean_text(source_match.group(1)) if source_match else ""
        hits.append(SearchHit(
            title=title or site_of(url),
            url=url,
            snippet=snippet,
            date=date,
            site=source or site_of(url),
        ))
        if len(hits) >= max(1, min(limit, MAX_RESULTS)):
            break
    return hits


@_register
class BingRssBackend(SearchBackend):
    name = "bing_rss"
    needs_key = False

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        import httpx

        count = max(1, min(int(limit or 5), MAX_RESULTS))
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": UA},
        ) as client:
            response = await client.get(
                ENDPOINT,
                params={"q": query, "format": "rss", "count": count},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Bing RSS returned HTTP {response.status_code}")
        return parse_rss(response.text, count)
