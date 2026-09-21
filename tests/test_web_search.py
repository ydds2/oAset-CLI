"""Web search: parsing correctness, backend selection, budget, cache, URL safety.

Regression source: the first implementation matched `<title>`/`<link>`/
`<description>` with three independent regexes and paired them by index. A live
Bing query returned 4 results for `count=5` and the first snippet belonged to a
different article — a model reading that cannot tell which source supports which
claim. These tests pin the block-wise parser so that class of corruption cannot
come back.
"""

from __future__ import annotations

import json
import socket

import pytest

from oaset.config import default_config
from oaset.tools import ToolRegistry
from oaset.tools.base import ToolContext
from oaset.tools.search import (
    SearchBudget,
    SearchHit,
    budget_for,
    cache_clear,
    cache_get,
    cache_key,
    cache_put,
    cache_size,
    clean_text,
    render_hits,
    resolve_backend,
    unwrap_redirect,
)
from oaset.tools.search.bing_rss import parse_date, parse_rss
from oaset.tools.url_safety import check_url
from oaset.tools.web import WebFetchTool, WebSearchTool

# A feed shaped like the real one: channel title first (which the old parser
# skipped), entities, CDATA, a redirect link, and one item with no date.
BING_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Bing: DeepSeek 联网搜索</title>
<link>http://www.bing.com:80/search?q=DeepSeek</link>
<item>
  <title>Responses API 上线服务端联网搜索</title>
  <link>https://juejin.cn/post/7668658871550181414</link>
  <description>昨天 DeepSeek V4 正式版的 Responses API 上线了服务端联网搜索&amp;实测</description>
  <pubDate>Fri, 12 Sep 2026 02:10:00 GMT</pubDate>
</item>
<item>
  <title><![CDATA[给 DeepSeek 装上"联网搜索"：生产方案 & 踩坑]]></title>
  <link>http://www.bing.com/search?q=x&amp;url=aHR0cHM6Ly9leGFtcGxlLmNvbS9hcnRpY2xl</link>
  <description><![CDATA[<b>摘要</b>：网页版有开关，API 默认断网。]]></description>
  <pubDate>2026-09-10T08:30:00Z</pubDate>
</item>
<item>
  <title>Third result</title>
  <link>https://blog.csdn.net/Dontla/article/details/145897357</link>
</item>
</channel></rss>
"""


@pytest.fixture(autouse=True)
def _clean_cache():
    cache_clear()
    yield
    cache_clear()


def ctx_for(**kwargs) -> ToolContext:
    from pathlib import Path

    cfg = default_config()
    for key, value in kwargs.items():
        setattr(cfg.search, key, value)
    return ToolContext(cwd=Path("."), mode="auto", config=cfg)


# ------------------------------------------------------------------ parsing


def test_parse_rss_pairs_fields_within_each_item():
    hits = parse_rss(BING_SAMPLE, 5)
    assert len(hits) == 3
    # the channel header is not a result
    assert all("Bing:" not in h.title for h in hits)
    # each snippet belongs to its own title — the bug this test exists for
    assert hits[0].title == "Responses API 上线服务端联网搜索"
    assert "Responses API 上线了服务端联网搜索&实测" in hits[0].snippet
    assert "生产方案" in hits[1].title
    assert "网页版有开关" in hits[1].snippet


def test_parse_rss_decodes_entities_and_cdata():
    hits = parse_rss(BING_SAMPLE, 5)
    assert "&amp;" not in hits[0].snippet and "&" in hits[0].snippet
    assert "&lt;b&gt;" not in hits[1].snippet  # tags stripped after unescape
    assert hits[1].snippet.startswith("摘要")
    assert '"' in hits[1].title  # CDATA quotes survive


def test_parse_rss_unwraps_bing_redirect_and_keeps_site():
    hits = parse_rss(BING_SAMPLE, 5)
    assert hits[1].url == "https://example.com/article"
    assert hits[1].site == "example.com"
    assert hits[2].url.startswith("https://blog.csdn.net/")


def test_parse_rss_respects_limit_and_missing_date():
    assert len(parse_rss(BING_SAMPLE, 2)) == 2
    hits = parse_rss(BING_SAMPLE, 5)
    assert hits[2].date == ""  # absent date must stay absent, never guessed
    assert hits[0].date == "2026-09-12"
    assert hits[1].date == "2026-09-10"


@pytest.mark.parametrize("raw,expected", [
    ("Fri, 12 Sep 2026 02:10:00 GMT", "2026-09-12"),
    ("2026-09-10T08:30:00Z", "2026-09-10"),
    ("2026/9/3", "2026-09-03"),
    ("Sep 1, 2026", "2026-09-01"),
    ("", ""),
    ("not a date", ""),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_clean_text_and_unwrap_redirect_are_total():
    assert clean_text(None) == ""
    assert clean_text("<p>a &amp; b</p>") == "a & b"
    assert unwrap_redirect("") == ""
    # non-Bing URLs pass through untouched
    assert unwrap_redirect("https://a.example/x") == "https://a.example/x"


# --------------------------------------------------------- backend selection


def test_explicit_backend_wins_even_when_unknown():
    backend, error = resolve_backend("does-not-exist")
    assert backend is None
    assert "Unknown search backend" in error
    assert "bing_rss" in error  # the error names what does exist


def test_auto_uses_searxng_when_url_present_else_bing():
    backend, error = resolve_backend("auto", env={})
    assert error == "" and backend is not None and backend.name == "bing_rss"

    backend, error = resolve_backend("auto", searxng_url="http://127.0.0.1:8888",
                                     env={})
    assert error == "" and backend is not None and backend.name == "searxng"

    backend, error = resolve_backend("auto", env={"SEARXNG_URL": "http://box:8888"})
    assert error == "" and backend.name == "searxng"


def test_searxng_selected_without_url_is_a_clear_error():
    backend, error = resolve_backend("searxng", env={})
    assert backend is None
    assert "searxng_url" in error


def test_unknown_auto_env_does_not_crash():
    backend, error = resolve_backend("", env={})
    assert error == "" and backend.name == "bing_rss"


# ---------------------------------------------------------------- budget/cache


def test_budget_stops_the_nth_plus_one_search():
    budget = SearchBudget(max_searches=2, max_fetches=1)
    assert budget.take_search("a") is None
    assert budget.take_search("b") is None
    assert "budget exhausted" in budget.take_search("c")
    assert budget.take_fetch("u") is None
    assert "budget exhausted" in budget.take_fetch("u2")
    budget.reset()
    assert budget.take_search("d") is None


def test_budget_is_shared_through_session_state():
    ctx = ctx_for()
    first = budget_for(ctx, max_searches=1, max_fetches=1)
    first.take_search("q")
    second = budget_for(ctx, max_searches=1, max_fetches=1)
    assert second is first  # same object, so the cap survives across calls
    assert second.searches == 1


def test_cache_roundtrip_and_ttl_zero_disables():
    hits = [SearchHit(title="t", url="https://e.example")]
    key = cache_key("  Hello   World ", 5, "bing_rss")
    cache_put(key, hits)
    assert cache_get(key, 60) == hits
    assert cache_get(key, 0) is None  # TTL 0 = disabled
    assert cache_size() == 1


def test_render_hits_is_labelled_and_marks_cache():
    text = render_hits([SearchHit(title="T", url="https://e.example", snippet="S",
                                  date="2026-01-02", site="e.example")],
                       query="q", backend="bing_rss", cached=True)
    assert "1. T" in text and "Date: 2026-01-02" in text and "URL: https://e.example"
    assert "cached=true" in text
    assert render_hits([], query="nothing") == "No results for 'nothing'."


# ---------------------------------------------------------------- URL safety


def test_metadata_addresses_blocked_even_with_private_optin():
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://100.100.100.200/"):
        refusal = check_url(url, allow_private=True)
        assert refusal is not None and "metadata" in refusal


def test_private_and_localhost_blocked_by_default_but_optin_works():
    assert check_url("http://localhost:8080/", allow_private=False) is not None
    assert check_url("http://localhost:8080/", allow_private=True) is None


def test_unresolvable_host_fails_closed():
    def resolver(host, port):
        raise OSError("no dns here")

    refusal = check_url("https://nope.invalid/", resolver=resolver)
    assert refusal is not None and "could not resolve" in refusal


def test_public_host_allowed_and_bad_scheme_rejected():
    def resolver(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    assert check_url("https://example.com/x", resolver=resolver) is None
    assert "unsupported scheme" in check_url("file:///etc/passwd", resolver=resolver)
    assert "unsupported scheme" in check_url("gopher://example.com", resolver=resolver)


# -------------------------------------------------------- tool-level behaviour


class _FakeBackend:
    name = "fake"
    needs_key = False
    calls = 0

    def available(self) -> bool:
        return True

    async def search(self, query, limit, *, timeout):
        _FakeBackend.calls += 1
        return [SearchHit(title=f"hit for {query}", url="https://e.example/1",
                          snippet="s", date="2026-09-12", site="e.example")]


class _FakeBackendFactory:
    """Hands out backends that answer from a script, so fan-out is testable."""

    def __init__(self, name: str, hits: list[SearchHit] | None = None,
                 error: Exception | None = None):
        self.name = name
        self.hits = hits or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def __call__(self) -> "_FakeBackendFactory":
        return self

    # SearchBackend protocol
    def available(self) -> bool:
        return True

    async def search(self, query, limit, *, timeout):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return list(self.hits)[:limit]


def _hit(title: str, url: str) -> SearchHit:
    return SearchHit(title=title, url=url, snippet="s", date="2026-09-12",
                     site="e.example")


# ------------------------------------------------------- multi-engine fan-out


def test_distribute_limit_gives_remainder_to_leading_engines():
    from oaset.tools.search import distribute_limit

    assert distribute_limit(8, 2) == [4, 4]
    assert distribute_limit(7, 2) == [4, 3]
    assert distribute_limit(2, 3) == [1, 1, 0]
    assert distribute_limit(5, 0) == []


@pytest.mark.asyncio
async def test_search_many_dedupes_and_keeps_surviving_engine(monkeypatch):
    from oaset.tools.search import search_many

    good = _FakeBackendFactory("good", [_hit("A", "https://x.example/1"),
                                        _hit("shared", "https://x.example/2")])
    other = _FakeBackendFactory("other", [_hit("shared dup", "https://X.example/2/"),
                                          _hit("B", "https://x.example/3")])
    broken = _FakeBackendFactory("broken", error=RuntimeError("boom"))

    merged = await search_many([good, other, broken], "q", 6, timeout=None)
    urls = [h.url for h in merged.hits]
    assert urls == ["https://x.example/1", "https://x.example/2", "https://x.example/3"]
    assert merged.engines == ["good", "other", "broken"]
    assert merged.failures and "broken" in merged.failures[0] and "boom" in merged.failures[0]
    # every engine got its own budget share
    assert [len(b.calls) for b in (good, other, broken)] == [1, 1, 1]


@pytest.mark.asyncio
async def test_failure_does_not_erase_results_in_rendered_output(monkeypatch):
    good = _FakeBackendFactory("good", [_hit("A", "https://x.example/1")])
    broken = _FakeBackendFactory("broken", error=RuntimeError("boom"))
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([good, broken], ""))
    ctx = ctx_for()
    result = await WebSearchTool().run({"query": "mixed"}, ctx)
    assert not result.is_error
    assert "1. A" in result.output
    assert "engine failures" in result.output and "broken" in result.output


def test_unwrap_redirect_strips_a1_prefix_and_tracking():
    import base64

    from oaset.tools.search import sanitize_url, unwrap_redirect

    payload = base64.urlsafe_b64encode(b"https://example.com/post?utm_source=x&id=7")
    wrapped = "https://www.bing.com/ck/a?u=a1" + payload.decode().rstrip("=")
    assert unwrap_redirect(wrapped) == "https://example.com/post?id=7"
    # tracking-only URLs stay untouched, internal Bing navigation is dropped
    assert sanitize_url("https://a.example/x?utm_campaign=q") == "https://a.example/x"
    assert sanitize_url("https://www.bing.com/search?q=x") == ""
    assert unwrap_redirect("https://plain.example/x") == "https://plain.example/x"


@pytest.mark.asyncio
async def test_web_search_is_read_class_and_uses_backend(monkeypatch):
    _FakeBackend.calls = 0
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([_FakeBackend()], ""))
    cfg = default_config()
    registry = ToolRegistry()
    ctx = ToolContext(cwd=__import__("pathlib").Path("."), mode="auto", config=cfg)
    registry.bind(ctx)
    assert registry.tools["web_search"].permission == "read"
    assert registry.tools["web_fetch"].permission == "read"

    result = await WebSearchTool().run({"query": "hello"}, ctx)
    assert not result.is_error
    assert "Date: 2026-09-12" in result.output and "hit for hello" in result.output


@pytest.mark.asyncio
async def test_web_search_second_call_hits_cache(monkeypatch):
    _FakeBackend.calls = 0
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([_FakeBackend()], ""))
    ctx = ctx_for()
    tool = WebSearchTool()
    first = await tool.run({"query": "cached"}, ctx)
    second = await tool.run({"query": "cached"}, ctx)
    assert _FakeBackend.calls == 1
    assert "cached=true" in second.output and "cached=true" not in first.output


@pytest.mark.asyncio
async def test_web_search_budget_exhaustion_is_explicit(monkeypatch):
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([_FakeBackend()], ""))
    ctx = ctx_for(per_turn_search_budget=1, cache_ttl_minutes=0)
    tool = WebSearchTool()
    assert not (await tool.run({"query": "one"}, ctx)).is_error
    blocked = await tool.run({"query": "two"}, ctx)
    assert blocked.is_error and "budget exhausted" in blocked.output


@pytest.mark.asyncio
async def test_web_search_default_budget_is_unlimited(monkeypatch):
    _FakeBackend.calls = 0
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([_FakeBackend()], ""))
    ctx = ctx_for(cache_ttl_minutes=0)
    tool = WebSearchTool()
    for i in range(8):
        result = await tool.run({"query": f"q{i}"}, ctx)
        assert not result.is_error, result.output
    assert _FakeBackend.calls == 8


@pytest.mark.asyncio
async def test_web_search_blocked_in_local_only(workspace):
    cfg = default_config()
    registry = ToolRegistry()
    ctx = ToolContext(cwd=workspace, mode="auto", network_mode="local_only", config=cfg)
    registry.bind(ctx)
    result = await registry.dispatch("web_search", json.dumps({"query": "x"}))
    assert result.is_error and "local_only" in result.output


@pytest.mark.asyncio
async def test_web_fetch_rejects_internal_url_before_any_request():
    ctx = ctx_for()
    result = await WebFetchTool().run({"url": "http://169.254.169.254/x"}, ctx)
    assert result.is_error and "metadata" in result.output


# ------------------------------------------------- redirect hops are pre-checked


@pytest.mark.asyncio
async def test_web_fetch_checks_each_redirect_hop_before_requesting_it():
    """A page that redirects at an internal address must be refused BEFORE the
    second request goes out. The old post-response guard fired only after the
    fetch (and then crashed on a wrong-arity exception), so the internal
    service saw the request first."""
    import httpx

    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8790/meta"})
        return httpx.Response(200, text="internal")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    import oaset.tools.web as webmod

    original_async_client = webmod.httpx.AsyncClient
    webmod.httpx.AsyncClient = patched_client
    try:
        ctx = ctx_for()
        result = await WebFetchTool().run({"url": "http://93.184.216.34/start"}, ctx)
    finally:
        webmod.httpx.AsyncClient = original_async_client

    assert result.is_error
    assert "127.0.0.1" in result.output
    assert all("127.0.0.1" not in hit for hit in hits), (
        f"the internal target must never be requested; got {hits}")
    assert "http://93.184.216.34/start" in hits


# --------------------------------------------------------- DNS pinning (rebind)


def test_pinned_request_keeps_identity_but_swaps_the_address():
    """The request must dial the CHECKED address while remaining, in every way
    the server and TLS can see, a request for the original host."""
    import httpx

    from oaset.tools.web import _pinned_request_for

    client = httpx.AsyncClient()
    request = _pinned_request_for(client, "https://api.example.com/v1/x?q=1", "203.0.113.7")
    assert str(request.url) == "https://203.0.113.7/v1/x?q=1"
    assert request.headers["Host"] == "api.example.com"
    assert request.extensions["sni_hostname"] == "api.example.com", (
        "SNI and certificate verification must stay on the original host")

    request6 = _pinned_request_for(client, "http://api.example.com:8080/p", "2001:db8::1")
    assert str(request6.url) == "http://[2001:db8::1]:8080/p"
    assert request6.headers["Host"] == "api.example.com:8080"
    assert "sni_hostname" not in request6.extensions, "plain http has no TLS to pin"


@pytest.mark.asyncio
async def test_web_fetch_dials_the_resolution_it_checked(monkeypatch):
    """The decisive rebinding test: the NAME does not resolve anywhere except
    in the SSRF check's own lookup. A double-resolution client cannot connect
    at all; the pinned flow dials the address the check returned — and the
    server still sees the original name in Host."""
    import http.server
    import threading

    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("Host", "")
            seen["path"] = self.path
            body = b"pinned-marker"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    real_getaddrinfo = socket.getaddrinfo

    def lying_resolver(host, *args, **kwargs):
        # ONLY the SSRF check knows this name: an unpinned second lookup dies
        if host == "rebind.test":
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lying_resolver)
    try:
        ctx = ctx_for(allow_private_urls=True)
        result = await WebFetchTool().run({"url": f"http://rebind.test:{port}/pinned"}, ctx)
    finally:
        server.shutdown()
        server.server_close()

    assert not result.is_error, result.output
    assert "pinned-marker" in result.output
    assert seen["path"] == "/pinned"
    assert seen["host"] == f"rebind.test:{port}", (
        f"the Host header must carry the original name, got {seen['host']!r}")


def test_resolve_allowed_ips_returns_the_checked_addresses():
    """The pinning seam: the check must hand back exactly the addresses it
    vetted, refuse the private ones, and pass literals through."""
    from oaset.tools.url_safety import resolve_allowed_ips

    literal = "https://93.184.216.34/x"      # a public address literal
    ips, refusal = resolve_allowed_ips(literal)
    assert refusal is None and ips == ["93.184.216.34"]
    # reserved ranges (TEST-NET-3) are blocked like any non-routable target
    ips, refusal = resolve_allowed_ips("https://203.0.113.7/x")
    assert refusal and ips == []

    ips, refusal = resolve_allowed_ips("http://127.0.0.1/x")
    assert refusal and ips == []
    ips, refusal = resolve_allowed_ips("http://127.0.0.1/x", allow_private=True)
    assert refusal is None and ips == ["127.0.0.1"]

    # unresolvable names fail closed with nothing to pin
    ips, refusal = resolve_allowed_ips("https://no-such-host.invalid/", resolver=_failing_resolver)
    assert refusal and "could not resolve" in refusal and ips == []


def _failing_resolver(host, port):
    raise socket.gaierror("offline")
