"""Search backends, result model, cache and per-turn budget.

Design notes (see git history: CONTRACTS §3, deleted 2026-09-13):

- One ordered backend table, one normalising result type. Every backend returns
  ``SearchHit`` objects with the SAME fields, so nothing downstream has to know
  which engine answered (other clients ship conflicting selection orders and
  providers that leak their own field names; we deliberately do not).
- ``date`` is a first-class field: without it the model cannot judge freshness,
  which was the single biggest quality gap in the old Bing-only implementation.
- The cache and the budget live here, not in the tool, so every caller
  (TUI, one-shot CLI, sub-agents, gateway) gets them for free.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- result model

MAX_SNIPPET_CHARS = 600
MAX_RESULTS = 20


@dataclass
class SearchHit:
    """One normalised search result. Every backend produces exactly this."""

    title: str
    url: str
    snippet: str = ""
    date: str = ""  # ISO-ish or the engine's own string; "" when unknown
    site: str = ""

    def render(self, index: int) -> str:
        """Model-facing text block for one hit (labelled fields)."""
        lines = [f"{index}. {self.title or '(no title)'}"]
        if self.site:
            lines.append(f"   Site: {self.site}")
        if self.date:
            lines.append(f"   Date: {self.date}")
        lines.append(f"   URL: {self.url}")
        if self.snippet:
            lines.append(f"   Summary: {self.snippet}")
        return "\n".join(lines)


def render_hits(hits: list[SearchHit], *, query: str = "", cached: bool = False,
                backend: str = "", extra: str = "", failures: list[str] | None = None) -> str:
    """Render a result list for the model, with a short provenance header.

    Partial failures are reported *next to* the results rather than replacing
    them: the model should know an engine was skipped, not lose the answers
    that did arrive.
    """
    if not hits:
        message = f"No results for '{query}'." if query else "No results."
        if failures:
            message += "\nEngine failures: " + "; ".join(failures)
        return message
    head = []
    if backend:
        head.append(f"backend={backend}")
    if cached:
        head.append("cached=true")
    if extra:
        head.append(extra)
    blocks = [hit.render(i) for i, hit in enumerate(hits, 1)]
    prefix = f"[search results: {' '.join(head)}]\n" if head else ""
    suffix = ""
    if failures:
        suffix = "\n\n[engine failures: " + "; ".join(failures) + "]"
    return prefix + "\n\n".join(blocks) + suffix



# ------------------------------------------------------------- URL normalising

_ENTITY_RE = re.compile(r"&(#x?[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(raw: str) -> str:
    """HTML-unescape, strip tags, collapse whitespace. Order matters: unescape
    after tag-stripping so `&lt;script&gt;` stays visible as text instead of
    becoming a tag that then gets removed."""
    if not raw:
        return ""
    text = raw.strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return " ".join(text.split())


def unwrap_redirect(url: str) -> str:
    """Resolve Bing's redirect wrapper to the real target.

    Bing hands back `/ck/a?...&u=a1<base64url>` and `/search?...&url=<b64>`
    wrappers. The ``a1`` prefix is a version marker that must be stripped
    before decoding (it is not part of the payload) — a decoder that keeps it
    yields garbage and then silently falls back to the Bing URL, which is how a
    model ends up "reading" a search page instead of the article.

    Reference: open-webSearch `src/engines/bing/parser.ts:19-37`.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.netloc.lower().endswith("bing.com"):
        return url
    params = parse_qs(parsed.query)
    raw = (params.get("u") or params.get("url") or [""])[0].strip()
    if raw:
        import base64

        payload = raw[2:] if raw.startswith("a1") else raw
        padded = payload + "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace").strip()
        except Exception:
            decoded = ""
        if decoded.startswith(("http://", "https://")):
            return sanitize_url(decoded)
    return url


_TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "ref", "source", "spm", "from")


def sanitize_url(url: str) -> str:
    """Strip tracking params and reject Bing's internal navigation URLs.

    Returning an internal `/search` or `/ck/a` URL as a "result" wastes a fetch
    and pollutes the transcript, so it is dropped instead.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host.endswith("bing.com") and (
        path.startswith("/search") or path.startswith("/ck/a")
        or path.startswith("/newtabredir")
    ):
        return ""
    if not parsed.query:
        return url
    kept = [
        (key, value) for key, value in parse_qs(parsed.query, keep_blank_values=True).items()
        if key.lower() not in _TRACKING_PARAMS
    ]
    if len(kept) == len(parse_qs(parsed.query, keep_blank_values=True)):
        return url
    from urllib.parse import urlencode, urlunparse

    query = urlencode([(k, v) for k, values in kept for v in values])
    return urlunparse(parsed._replace(query=query))



def site_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


# --------------------------------------------------------------------- caching

_CACHE: dict[str, tuple[float, list[SearchHit]]] = {}


def cache_key(query: str, limit: int, backend: str) -> str:
    return f"{backend}|{limit}|{' '.join(query.lower().split())}"


def cache_get(key: str, ttl_minutes: int) -> list[SearchHit] | None:
    if ttl_minutes <= 0:
        return None
    entry = _CACHE.get(key)
    if entry is None:
        return None
    stored_at, hits = entry
    if time.time() - stored_at > ttl_minutes * 60:
        _CACHE.pop(key, None)
        return None
    return hits


def cache_put(key: str, hits: list[SearchHit]) -> None:
    _CACHE[key] = (time.time(), hits)


def cache_clear() -> None:
    _CACHE.clear()


def cache_size() -> int:
    return len(_CACHE)


# ---------------------------------------------------------------------- budget


@dataclass
class SearchBudget:
    """Per-turn call budget: the cheapest possible defence against a search loop.

    A single factual question routinely costs search -> fetch -> search again;
    without a cap the agent can fan out indefinitely (no search
    budget at all — only its generic iteration cap).
    """

    max_searches: int = 0
    max_fetches: int = 0
    searches: int = 0
    fetches: int = 0
    history: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.searches = 0
        self.fetches = 0
        self.history.clear()

    def take_search(self, query: str) -> str | None:
        """Consume one search slot; return an error string when exhausted."""
        if self.max_searches > 0 and self.searches >= self.max_searches:
            return (
                f"Search budget exhausted for this turn ({self.max_searches} searches). "
                "Answer with what you have, or ask the user to raise "
                "[search].per_turn_search_budget."
            )
        self.searches += 1
        self.history.append(f"search:{query}")
        return None

    def take_fetch(self, url: str) -> str | None:
        if self.max_fetches > 0 and self.fetches >= self.max_fetches:
            return (
                f"Fetch budget exhausted for this turn ({self.max_fetches} fetches). "
                "Answer with what you have, or ask the user to raise "
                "[search].per_turn_fetch_budget."
            )
        self.fetches += 1
        self.history.append(f"fetch:{url}")
        return None


def budget_for(ctx: Any, *, max_searches: int, max_fetches: int) -> SearchBudget:
    """Per-conversation budget living in the tool context's session state."""
    state = getattr(ctx, "session_state", None)
    if state is None:
        return SearchBudget(max_searches=max_searches, max_fetches=max_fetches)
    budget = state.get("search_budget")
    if not isinstance(budget, SearchBudget):
        budget = SearchBudget(max_searches=max_searches, max_fetches=max_fetches)
        state["search_budget"] = budget
    else:
        budget.max_searches = max_searches
        budget.max_fetches = max_fetches
    return budget


# -------------------------------------------------------------------- backends


class SearchBackend:
    """One search engine. `search()` must return normalised hits or raise.

    ``proxy`` is set per query by the tool layer from ``[search] proxy`` so
    enterprise proxies work without changing backend signatures; engines read
    it when building their HTTP client."""

    name = "backend"
    needs_key = False
    proxy: str = ""

    def available(self) -> bool:  # pragma: no cover - trivial
        return True

    async def search(self, query: str, limit: int, *, timeout: float) -> list[SearchHit]:
        raise NotImplementedError


# Order used when the configured/auto engine returns nothing: degrade
# visibly through the list instead of answering "no results" off one
# engine's bad day (S7). searxng participates as the auto primary when its
# URL is configured; the fallback sequence covers the key-free engines.
AUTO_FALLBACK: tuple[str, ...] = ("bing_rss", "baidu", "duckduckgo", "brave")

# HTML-scraping engines get one automatic retry with linear backoff: search
# endpoints throttle and hiccup, and a single 403/timeout should not read as
# "the web has no answer".
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.5


async def request_with_retry(client, url: str, *, params: dict | None = None):
    """GET with one retry after linear backoff (shared by the HTML engines).

    Connection resets and 429/5xx bodies raise like any other failure so the
    per-engine failure note stays truthful; the retry only covers transport
    noise between attempts."""
    import asyncio

    import httpx

    last: Exception | None = None
    for attempt in range(max(1, RETRY_ATTEMPTS)):
        try:
            return await client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
            if attempt + 1 < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last is not None
    raise last


def known_backends() -> list[str]:
    return sorted(_BACKENDS)


def register_backend(cls: type[SearchBackend]) -> type[SearchBackend]:
    """Register a backend class under its ``name``, replacing a same-name one.

    Public counterpart of the ``@_register`` decorator: the eval harness and
    test suites add deterministic engines through this instead of reaching
    into the private table — a live engine inside a test is a flake with a
    network dependency attached.
    """
    return _register(cls)


def backend_by_name(name: str) -> SearchBackend | None:
    factory = _BACKENDS.get((name or "").lower().strip())
    return factory() if factory else None


def _register(cls: type[SearchBackend]) -> type[SearchBackend]:
    _BACKENDS[cls.name] = cls
    return cls


def _load_backends() -> None:
    """Import backend modules for their registration side effect."""
    from oaset.tools.search import baidu, bing_rss, brave, duckduckgo, searxng, zhipu  # noqa: F401


_BACKENDS: dict[str, type[SearchBackend]] = {}

# ONE ordered selection table — deliberately not three (see module docstring).
# Explicit config wins even when it is wrong, so a typo surfaces the backend's
# own error instead of silently rerouting the user's queries to another engine.
DEFAULT_BACKEND = "bing_rss"


def resolve_backend(
    name: str = "", *, searxng_url: str = "", env: dict[str, str] | None = None,
) -> tuple[SearchBackend | None, str]:
    """Single-backend convenience wrapper around :func:`resolve_backends`."""
    backends, error = resolve_backends(name, searxng_url=searxng_url, env=env)
    if not backends:
        return None, error
    return backends[0], ""


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def resolve_backends(
    name: str = "", *, searxng_url: str = "", env: dict[str, str] | None = None,
) -> tuple[list[SearchBackend], str]:
    """Pick one or more backends.

    ``name`` may be a single backend, ``"auto"``, or a comma-separated list
    (e.g. ``"searxng,bing_rss"``) — multi-engine fan-out is the single biggest
    quality lever: independent indexes disagree, and agreement between them is
    the cheapest available relevance signal.
    """
    _load_backends()
    environ = env if env is not None else _environ()
    url = searxng_url or environ.get("SEARXNG_URL", "")
    requested = (name or "").strip()
    if requested.lower() in ("", "auto"):
        if url:
            backend = _BACKENDS["searxng"](url)  # type: ignore[call-arg]
            if backend.available():
                return [backend], ""
        # official domestic API first (reuses the glm credential), then the
        # zero-config fallback — compliance before convenience in "auto".
        # available() defaults to True: the registry is duck-typed, and a
        # backend that only implements search() IS usable.
        for auto_name in ("zhipu", DEFAULT_BACKEND):
            factory = _BACKENDS.get(auto_name)
            if factory is None:
                continue
            backend = factory()
            if getattr(backend, "available", lambda: True)():
                return [backend], ""
        return [], "No search backend is available in this build."

    backends: list[SearchBackend] = []
    unknown: list[str] = []
    for raw in requested.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        factory = _BACKENDS.get(token)
        if factory is None:
            unknown.append(token)
            continue
        if token == "searxng":
            instance = factory(url)  # type: ignore[call-arg]
            if not instance.available():
                return [], ("Search backend 'searxng' is selected but no instance URL is set. "
                            "Set [search] searxng_url or SEARXNG_URL.")
            backends.append(instance)
        else:
            backends.append(factory())
    if unknown:
        return [], (f"Unknown search backend(s): {', '.join(unknown)}. "
                    f"Known backends: {', '.join(known_backends())}.")
    return backends, ""


def default_engines() -> list[str]:
    """Engines worth offering to the model, ordered by usefulness here."""
    _load_backends()
    return [name for name in ("zhipu", "bing_rss", "searxng", "baidu", "duckduckgo", "brave")
            if name in _BACKENDS and _BACKENDS[name]().available()]


def distribute_limit(total: int, count: int) -> list[int]:
    """Split a result budget across engines (first engines get the remainder).

    Reference: open-webSearch `src/core/search/searchEngines.ts:52-59`.
    """
    if count <= 0:
        return []
    base, remainder = divmod(max(0, int(total)), count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


@dataclass
class AggregatedSearch:
    """Merged result set plus what went wrong on the way."""

    hits: list[SearchHit] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


async def search_many(
    backends: list[SearchBackend], query: str, limit: int, *, timeout: Any,
) -> AggregatedSearch:
    """Query every backend concurrently, merge, de-duplicate by URL.

    A backend that fails contributes a failure note instead of killing the
    whole call — with one engine a transient 5xx used to mean "no results".
    """
    import asyncio

    if not backends:
        return AggregatedSearch(failures=["no backend available"])
    limits = distribute_limit(limit, len(backends))

    async def one(backend: SearchBackend, share: int) -> AggregatedSearch:
        try:
            hits = await backend.search(query, max(1, share), timeout=timeout)
        except Exception as exc:  # one engine must not sink the query
            return AggregatedSearch(engines=[backend.name],
                                    failures=[f"{backend.name}: {type(exc).__name__}: {exc}"])
        return AggregatedSearch(hits=list(hits), engines=[backend.name])

    parts = await asyncio.gather(*(one(b, s) for b, s in zip(backends, limits)))
    merged = AggregatedSearch()
    seen: set[str] = set()
    for part in parts:
        merged.engines.extend(part.engines)
        merged.failures.extend(part.failures)
        for hit in part.hits:
            key = hit.url.rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.hits.append(hit)
    merged.hits = merged.hits[:limit]
    return merged

