"""P3 regressions (docs/PLAN.zh-CN.md §5): key-free multi-engine search with
visible degradation, proxy/retry plumbing, fuzzy edit_file, fetch robots +
streamed size cap, and the /login first-class cloud path.

Engine parsers are tested against fixture HTML (offline); the degrade chain
and robots logic run against fakes — no network in this suite.
"""

from __future__ import annotations

import httpx

from oaset.config import default_config
from oaset.providers import MockProvider
from oaset.tools.base import ToolContext
from oaset.tools.fs import EditFileTool
from oaset.tools.search import (
    AUTO_FALLBACK,
    backend_by_name,
    default_engines,
)
from oaset.tools.web import _parse_robots, _robots_blocks
from oaset.tui.app import OasetApp


def make_app(workspace, script=None):
    provider = MockProvider(script or [])
    return OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                    model_id="mock/mock-echo")


# ------------------------------------------------------ P3-1 engine parsers


def test_new_engines_are_registered_and_offered():
    names = set(default_engines())
    assert {"bing_rss", "baidu", "duckduckgo", "brave"} <= names
    for name in ("baidu", "duckduckgo", "brave"):
        assert backend_by_name(name) is not None


def test_duckduckgo_parser_decodes_redirects_and_pairs_snippets():
    from oaset.tools.search.duckduckgo import parse_ddg_page

    html_text = """
    <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Futm_source%3Dx&rut=abc">Result A</a></h2>
    <a class="result__snippet">first snippet</a>
    <h2><a class="result__a" href="https://direct.example.com/b">Result B</a></h2>
    <a class="result__snippet">second snippet</a>
    """
    hits = parse_ddg_page(html_text, 10)
    assert [h.title for h in hits] == ["Result A", "Result B"]
    assert hits[0].url == "https://example.com/a"  # redirect decoded, utm stripped
    assert hits[0].snippet == "first snippet"      # paired by index within the page
    assert hits[0].site == "example.com"
    assert hits[0].date == ""                      # DDG HTML: no dates, none invented


def test_baidu_parser_prefers_mu_attribute_over_wrapper():
    from oaset.tools.search.baidu import parse_baidu_page

    html_text = """
    <div class="result c-container new-pmd" mu="https://real.example.com/post/1">
      <h3 class="c-title"><a href="http://www.baidu.com/link?url=XYZ">中文标题一</a></h3>
      <span class="c-abstract">这是摘要 <b>加粗</b> 内容。</span>
    </div>
    <div class="result c-container">
      <h3><a href="http://www.baidu.com/link?url=ABC">标题二</a></h3>
    </div>
    """
    hits = parse_baidu_page(html_text, 10)
    assert hits[0].url == "https://real.example.com/post/1"  # mu beats the wrapper
    assert hits[0].title == "中文标题一"
    assert "加粗" in hits[0].snippet
    assert hits[1].url.startswith("http://www.baidu.com/link?url=")  # no mu: wrapper kept


def test_brave_parser_skips_internal_links():
    from oaset.tools.search.brave import parse_brave_page

    html_text = """
    <div class="snippet" data-type="web">
      <a href="https://search.brave.com/help">Help</a>
      <a href="https://docs.example.com/guide">Guide title</a>
      <div class="snippet-description">How to do the thing.</div>
    </div>
    """
    hits = parse_brave_page(html_text, 10)
    assert len(hits) == 1
    assert hits[0].url == "https://docs.example.com/guide"
    assert hits[0].title == "Guide title"
    assert hits[0].snippet == "How to do the thing."


# --------------------------------------------- P3-1/P3-2 degrade + plumbing


async def test_auto_degrades_to_next_engine_with_visible_note(tmp_path, isolated_home):
    # isolated_home: without it the REAL machine's glm credential makes the
    # auto chain pick the zhipu engine (correct product behaviour) and this
    # test would hit the live network instead of its fake engines
    from oaset.tools.search import SearchHit

    calls: list[str] = []

    class Dead:
        name = AUTO_FALLBACK[0]

        async def search(self, query, limit, *, timeout):
            calls.append(self.name)
            raise RuntimeError("engine down")

    class Alive:
        name = "baidu"

        async def search(self, query, limit, *, timeout):
            calls.append(self.name)
            return [SearchHit(title="t", url="https://x.example.com/a", snippet="s")]

    from oaset.tools.web import WebSearchTool

    ctx = ToolContext(cwd=tmp_path, mode="default", output_limit=2000,
                      config=default_config())
    tool = WebSearchTool()

    # Drive the degrade chain through the real tool path by faking the
    # resolver order: primary = bing_rss (dead), fallback = baidu (alive).
    import oaset.tools.search as search_mod

    search_mod._load_backends()  # register real backends FIRST: lazy import
    # would otherwise re-run @_register and overwrite the patches below
    originals = dict(search_mod._BACKENDS)
    try:
        search_mod._BACKENDS["bing_rss"] = lambda: Dead()
        search_mod._BACKENDS["baidu"] = lambda: Alive()
        result = await tool.run({"query": "test"}, ctx)
    finally:
        search_mod._BACKENDS.clear()
        search_mod._BACKENDS.update(originals)

    assert not result.is_error
    assert "degraded-to=baidu" in result.output
    assert calls == ["bing_rss", "baidu"]


async def test_backends_receive_proxy_from_config(tmp_path, monkeypatch):
    from oaset.tools.web import WebSearchTool

    seen: list[str] = []

    class Probe:
        name = "bing_rss"
        proxy = ""

        async def search(self, query, limit, *, timeout):
            seen.append(self.proxy)
            return []

    cfg = default_config()
    cfg.search.proxy = "http://127.0.0.1:9090"
    ctx = ToolContext(cwd=tmp_path, mode="default", output_limit=500, config=cfg)
    monkeypatch.setattr("oaset.tools.web.resolve_backends",
                        lambda *a, **k: ([Probe()], ""))
    from oaset.tools.search import cache_clear

    cache_clear()  # a sibling test may have cached this query's key
    result = await WebSearchTool().run({"query": "proxy plumb test"}, ctx)
    assert not result.is_error
    assert seen == ["http://127.0.0.1:9090"], "the config proxy reaches the engine"


def test_request_with_retry_backs_off_then_succeeds(monkeypatch):
    import asyncio

    from oaset.tools.search import request_with_retry

    attempts = {"n": 0}
    sleeps: list[float] = []

    class Flaky:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("boom")
            return "response"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def main():
        return await request_with_retry(Flaky(), "https://x", params={})

    result = asyncio.run(main())
    assert result == "response" and attempts["n"] == 2 and sleeps == [0.5]


# ------------------------------------------------ P3-4 fuzzy edit_file


def _ctx(tmp_path):
    return ToolContext(cwd=tmp_path, mode="default", output_limit=500)


async def test_fuzzy_edit_applies_trailing_whitespace_drift(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("def f():\n    return 1   \n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "code.py", "old_text": "def f():\n    return 1", "new_text": "def f():\n    return 2"},
        _ctx(tmp_path))
    assert not result.is_error and "fuzzy match" in result.output
    assert "return 2" in target.read_text(encoding="utf-8")


async def test_fuzzy_edit_applies_indentation_drift(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("if x:\n        do_a()\n        do_b()\n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "code.py",
         "old_text": "if x:\n    do_a()\n    do_b()",
         "new_text": "if x:\n    do_c()"},
        _ctx(tmp_path))
    assert not result.is_error
    body = target.read_text(encoding="utf-8")
    assert "do_c()" in body and "do_a()" not in body


async def test_fuzzy_edit_lists_ambiguous_candidates(tmp_path):
    target = tmp_path / "twice.py"
    target.write_text("x = call()\nx = call()\n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "twice.py",
         "old_text": "x = call()  ",
         "new_text": "x = other()"},
        _ctx(tmp_path))
    assert result.is_error and "2 locations" in result.output and "line 1" in result.output
    assert target.read_text(encoding="utf-8") == "x = call()\nx = call()\n"


async def test_fuzzy_edit_reports_closest_lines_when_nothing_matches(tmp_path):
    target = tmp_path / "near.py"
    target.write_text("alpha_one()\n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "near.py", "old_text": "alpha_two()", "new_text": "x"},
        _ctx(tmp_path))
    assert result.is_error and "alpha_one" in result.output


async def test_fuzzy_edit_preserves_crlf_line_endings(tmp_path):
    target = tmp_path / "win.py"
    target.write_bytes(b"value = 1  \r\nprint(value)\r\n")
    result = await EditFileTool().run(
        {"path": "win.py", "old_text": "value = 1", "new_text": "value = 2"},
        _ctx(tmp_path))
    assert not result.is_error
    assert b"value = 2  \r\n" in target.read_bytes(), "CRLF must survive the fuzzy path"


# ----------------------------------------------- P3-6 robots + streamed cap


def test_robots_parser_scopes_star_group_and_ignores_other_agents():
    robots = """
    # comment
    User-agent: Googlebot
    Disallow: /private-google

    User-agent: *
    Disallow: /admin
    Disallow:

    User-agent: oaset-cli
    Disallow: /tmp
    """
    rules = _parse_robots(robots)
    assert "/admin" in rules and "/tmp" in rules
    assert "/private-google" not in rules


def test_robots_blocks_by_path_prefix():
    # RFC 9309 prefix matching: "/admin" also covers "/administration".
    assert _robots_blocks(["/admin"], "/admin/settings?x=1")
    assert _robots_blocks(["/admin"], "/administration")
    assert not _robots_blocks([], "/admin")


async def test_web_fetch_honours_robots_txt(tmp_path, monkeypatch):
    from oaset.tools.web import _ROBOTS_CACHE, WebFetchTool

    _ROBOTS_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="secret")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: real_client(
                            **{**kwargs, "transport": transport}))
    ctx = _ctx(tmp_path)
    ctx.config = default_config()
    ctx.config.search.allow_private_urls = True  # MockTransport needs no DNS
    result = await WebFetchTool().run({"url": "http://localhost:9/private/x"}, ctx)
    assert result.is_error and "robots.txt" in result.output
    ok = await WebFetchTool().run({"url": "http://localhost:9/public/x"}, ctx)
    assert not ok.is_error


async def test_web_fetch_caps_streamed_body(tmp_path, monkeypatch):
    from oaset.tools import web as web_mod
    from oaset.tools.web import _ROBOTS_CACHE, WebFetchTool

    _ROBOTS_CACHE.clear()
    big = b"x" * (web_mod.MAX_BYTES + 500_000)
    downloaded = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        downloaded["n"] += 1
        return httpx.Response(200, content=big, headers={
            "content-type": "text/plain"})

    transport = httpx.MockTransport(handler)

    class CapClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("proxy", None)
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", CapClient)
    ctx = _ctx(tmp_path)
    ctx.config = default_config()
    ctx.config.search.allow_private_urls = True
    result = await WebFetchTool().run({"url": "http://localhost:9/huge"}, ctx)
    assert not result.is_error
    assert "truncated at" in result.output  # streaming stopped at the cap
    assert downloaded["n"] == 2  # robots.txt probe + the page itself


# --------------------------------------------------- P3-5 /login first-class


async def test_login_stores_key_and_points_at_next_step(tmp_path, monkeypatch):

    app = make_app(tmp_path)
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr("oaset.credentials.save_credential",
                        lambda provider, key: stored.append((provider, key)))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        async def fake_input(self, title, placeholder="", password=False):
            return "sk-test-123"

        monkeypatch.setattr(type(app), "_input", fake_input)
        await app.cmd_login("mock")
        await pilot.pause(0.2)
        assert stored == [("mock", "sk-test-123")]
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "已可用" in rendered or "can send now" in rendered, \
            "logging into the current provider must activate this session immediately"


async def test_login_rejects_unknown_provider(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app.cmd_login("nope")
        await pilot.pause(0.1)
        assert app.status_bar._error, "an unknown provider pins a visible error"
