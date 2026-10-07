import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from web_mcp.fetchers import BrowserPage, FetchError
from web_mcp.pacing import RateLimited, TokenBucket, parse_rate
from web_mcp.search import (
    EngineHealth,
    Result,
    SearchError,
    SearchOutcome,
    Searcher,
    normalize_query,
    render,
    shape,
    trim,
)
from web_mcp.serp import parse_brave, parse_startpage

FIX = Path(__file__).parent / "fixtures"
SEARX = "http://searxng.test:8080"
CONFIG = {
    "engines": [
        {"name": n, "enabled": en, "categories": cats}
        for n, en, cats in [
            ("bing", True, ["general", "web"]),
            ("google cse", True, ["general", "web"]),
            ("brave", True, ["general", "web"]),
            ("mojeek", True, ["general", "web"]),
            ("startpage", True, ["general", "web"]),
            ("currency", True, ["general"]),
            ("wikidata", True, ["general"]),
            ("yahoo", False, ["general", "web"]),
            ("bing news", True, ["news"]),
        ]
    ]
}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def results(n, engines=("bing",), start=0):
    return [
        {
            "title": f"Result {i}",
            "url": f"https://site{i}.example/page",
            "content": "word " * 100,
            "engine": engines[0],
            "engines": list(engines),
            "publishedDate": "2026-09-01T10:00:00" if i == 0 else None,
        }
        for i in range(start, start + n)
    ]


def payload(n=8, unresponsive=None, engines=("bing",), start=0):
    return {"query": "q", "results": results(n, engines, start), "unresponsive_engines": unresponsive or []}


class Upstream:
    """Fake SearXNG + APIs. `searx` is a list of answers, one per /search call."""

    def __init__(self, searx=None, brave=None, exa=None):
        self.searx = list(searx or [])
        self.brave = brave
        self.exa = exa
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request):
        host, path = request.url.host, request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((f"{host}{path}", params))
        if host == "searxng.test" and path == "/config":
            return httpx.Response(200, json=CONFIG)
        if host == "searxng.test":
            answer = self.searx.pop(0) if self.searx else payload(0)
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
        if host == "api.search.brave.com":
            return self.brave if isinstance(self.brave, httpx.Response) else httpx.Response(200, json=self.brave)
        if host == "api.exa.ai":
            return self.exa if isinstance(self.exa, httpx.Response) else httpx.Response(200, json=self.exa)
        raise AssertionError(host)

    def searches(self):
        return [p for u, p in self.calls if u.endswith("/search")]


def make(up: Upstream, **kw) -> Searcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(up))
    kw.setdefault("pacer", TokenBucket(100, 1.0))
    return Searcher(SEARX, client=client, **kw)


class FakeBrowser:
    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.urls: list[str] = []

    async def fetch(self, url, timeout, wait):
        self.urls.append(url)
        for key, page in self.pages.items():
            if key in url:
                if isinstance(page, Exception):
                    raise page
                return BrowserPage(url=url, status=200, html=page, title="")
        raise FetchError("connection refused or not reachable", "network")


BRAVE = {"web": {"results": [{"title": "B1", "url": "https://b1.example/", "description": "<strong>brave</strong> snippet", "page_age": "2026-01-02T00:00:00"}]}}
EXA = {"results": [{"title": "E1", "url": "https://e1.example/", "text": "exa text", "publishedDate": "2025-05-05T00:00:00.000Z"}]}
STARTPAGE_HTML = (FIX / "serp_startpage.html").read_text()
BRAVE_HTML = (FIX / "serp_brave.html").read_text()


# --- shaping ---------------------------------------------------------------


async def test_searxng_results_are_shaped_and_deduplicated():
    p = payload(8)
    p["results"].insert(1, dict(p["results"][0], url="https://www.site0.example/page/", title="dup"))
    p["results"][1 + 1]["engines"] = ["bing", "google cse"]
    out = await make(Upstream([p])).search("q", 6)
    assert out.provider == "searxng"
    assert [r.url for r in out.results] == [f"https://site{i}.example/page" for i in range(6)]
    assert out.results[0].published == "2026-09-01"
    assert all(len(r.snippet) <= 241 for r in out.results)
    assert out.results[1].engines == ["bing", "google cse"]


def test_render_names_layer():
    out = render("q", SearchOutcome("searxng-retry", [Result("T", "https://x.example/", "snip", ["bing"], "2026-01-01")]))
    assert "provider: searxng-retry" in out and "untrusted" in out
    assert "engines: bing · published: 2026-01-01" in out


def test_shape_drops_non_http_and_caps():
    rs = [Result("a", "javascript:1"), Result("b", "https://b.example/")] + [Result(str(i), f"https://{i}.example/") for i in range(30)]
    out = shape(rs, 5)
    assert len(out) == 5 and out[0].url == "https://b.example/"


def test_trim():
    assert trim("a " * 500).endswith("…")
    assert len(trim("a " * 500)) <= 241
    assert trim("  short  ") == "short"


# --- cache ------------------------------------------------------------------


async def test_cache_hits_on_normalised_query():
    up = Upstream([payload(8)])
    s = make(up)
    first = await s.search("Rust  Borrow   checker", 6)
    second = await s.search("  rust borrow checker ", 6)
    assert first.provider == "searxng" and second.provider == "cache"
    assert "cached answer from searxng" in second.note
    assert [r.url for r in second.results] == [r.url for r in first.results]
    assert len(up.searches()) == 1


async def test_cache_key_includes_max_results():
    up = Upstream([payload(8), payload(8)])
    s = make(up)
    await s.search("q", 6)
    out = await s.search("q", 3)
    assert out.provider == "searxng" and len(up.searches()) == 2


def test_normalize_query():
    assert normalize_query("  Hello\tWORLD \n x ") == "hello world x"


async def test_results_cached_one_hour_empty_five_minutes():
    clock = Clock()
    up = Upstream([payload(0), payload(0), payload(8)])
    s = make(up, clock=clock)
    assert (await s.search("nothing here")).results == []
    clock.t += 299
    assert (await s.search("nothing here")).provider == "cache"
    clock.t += 2
    assert (await s.search("nothing here")).provider == "searxng"  # empty entry expired
    assert len(up.searches()) == 2
    await s.search("something", 6)
    clock.t += 3599
    assert (await s.search("something", 6)).provider == "cache"
    clock.t += 2
    up.searx.append(payload(8))
    assert (await s.search("something", 6)).provider == "searxng"


async def test_queries_are_not_logged(caplog):
    from web_mcp.config import Settings
    from web_mcp.server import setup_logging

    setup_logging(Settings())  # the server's real logging setup (LOG_DETAILS off)
    caplog.set_level("INFO")
    s = make(Upstream([payload(8)]))
    await s.search("secret project name", 6)
    await s.search("secret project name", 6)
    assert "secret" not in caplog.text


# --- engine health and retry -------------------------------------------------


async def test_retry_with_healthy_engines_when_most_engines_fail():
    unresponsive = [["google cse", "too many requests"], ["brave", "too many requests"], ["duckduckgo", "timeout"]]
    up = Upstream([payload(2, unresponsive), payload(6, engines=("mojeek", "startpage"), start=10)])
    s = make(up)
    out = await s.search("q", 6)
    assert out.provider == "searxng-retry"
    retry = up.searches()[1]
    assert set(retry["engines"].split(",")) == {"mojeek", "startpage"}  # not the failed ones, not bing again, not currency
    assert "google cse" in out.note and "brave" in out.note
    # The next query skips resting engines from the start.
    up.searx.append(payload(8, engines=("bing",)))
    await s.search("another query", 6)
    first = up.searches()[2]
    assert set(first["engines"].split(",")) == {"bing", "mojeek", "startpage"}


async def test_no_retry_when_nothing_failed():
    up = Upstream([payload(0)])
    s = make(up)
    out = await s.search("q")
    assert out.provider == "searxng" and out.results == []
    assert len(up.searches()) == 1


async def test_news_query_adds_news_engine():
    up = Upstream([payload(8)])
    s = make(up)
    await s.search("latest news about widgets", 6)
    assert "bing news" in up.searches()[0]["engines"].split(",")


def test_engine_health_expiry():
    clock = Clock()
    h = EngineHealth(clock)
    h.update([], [("brave", "too many requests"), ("duckduckgo", "timeout")])
    assert set(h.unhealthy()) == {"brave", "duckduckgo"}
    clock.t += 301
    assert set(h.unhealthy()) == {"brave"}
    h.update(["brave"], [])
    assert h.unhealthy() == {}


# --- fallback ordering ---------------------------------------------------------


async def test_browser_layer_runs_after_searxng_and_before_api():
    up = Upstream([payload(0)], brave=BRAVE)
    browser = FakeBrowser({"startpage.com": STARTPAGE_HTML})
    s = make(up, browser=browser, brave_api_key="k")
    out = await s.search("widgets", 6)
    assert out.provider == "browser:startpage"
    assert [r.url for r in out.results] == ["https://alpha.example/guide", "https://beta.example/post/42"]
    assert not any("brave.com" in u for u, _ in up.calls)


async def test_browser_engines_tried_in_order():
    up = Upstream([payload(0)])
    browser = FakeBrowser({"startpage.com": FetchError("page load timed out", "timeout"), "search.brave.com": BRAVE_HTML})
    out = await make(up, browser=browser).search("widgets", 6)
    assert out.provider == "browser:brave"
    assert browser.urls[0].startswith("https://www.startpage.com/") and browser.urls[1].startswith("https://search.brave.com/")


async def test_browser_challenge_page_is_not_parsed():
    cf = (FIX / "cloudflare_jsc.html").read_text()
    up = Upstream([payload(0)])
    out = await make(up, browser=FakeBrowser({"startpage.com": cf, "search.brave.com": cf})).search("widgets")
    assert out.provider == "searxng" and out.results == []
    assert "blocked (cloudflare)" in out.note


async def test_api_is_last_and_only_with_key():
    up = Upstream([payload(0)], brave=BRAVE)
    out = await make(up, browser=FakeBrowser({}), brave_api_key="k").search("widgets")
    assert out.provider == "brave-api"
    up2 = Upstream([payload(0)], brave=BRAVE)
    await make(up2, browser=FakeBrowser({})).search("widgets")
    assert not any("brave.com" in u for u, _ in up2.calls)


async def test_browser_search_rate_limit():
    clock = Clock()
    up = Upstream([payload(0)] * 5)
    browser = FakeBrowser({})
    s = make(up, browser=browser, browser_engines=["startpage"], browser_per_minute=2, clock=clock)
    for i in range(3):
        out = await s.search(f"query {i}")
    assert len(browser.urls) == 2
    assert "browser search rate limit" in out.note


async def test_searxng_down_falls_to_browser():
    up = Upstream([httpx.ConnectError("refused")])
    out = await make(up, browser=FakeBrowser({"startpage.com": STARTPAGE_HTML})).search("widgets")
    assert out.provider == "browser:startpage"
    assert "not reachable" in out.note


async def test_searxng_down_and_no_other_layer_is_a_clear_error():
    up = Upstream([httpx.ConnectError("refused")])
    with pytest.raises(SearchError) as e:
        await make(up).search("q")
    assert "SearXNG is not reachable" in str(e.value)


async def test_api_order_brave_then_exa():
    up = Upstream([payload(0)], brave=httpx.Response(429), exa=EXA)
    out = await make(up, brave_api_key="k", exa_api_key="k").search("q")
    assert out.provider == "exa-api" and out.results[0].published == "2025-05-05"


async def test_json_format_disabled_message():
    with pytest.raises(SearchError, match="json"):
        await make(Upstream([httpx.Response(403)])).search("q")


async def test_empty_query():
    with pytest.raises(SearchError):
        await make(Upstream()).search("  ")


# --- pacing ---------------------------------------------------------------------


async def test_pacer_queues_bursts_instead_of_failing():
    bucket = TokenBucket(3, 0.3)  # 3 per 0.3 s
    t0 = time.monotonic()
    waits = await asyncio.gather(*(bucket.acquire(5) for _ in range(6)))
    elapsed = time.monotonic() - t0
    assert waits[:3] == [0.0, 0.0, 0.0]
    assert 0.25 <= elapsed < 1.0
    assert bucket.waited == 3


async def test_pacer_refuses_when_queue_too_long():
    bucket = TokenBucket(1, 10)
    await bucket.acquire(0)
    with pytest.raises(RateLimited):
        await bucket.acquire(1)


async def test_search_reports_local_pacing():
    s = make(Upstream([payload(8)] * 3), pacer=TokenBucket(1, 60), max_queue_wait=0.1)
    await s.search("one")
    with pytest.raises(SearchError, match="pacing"):
        await s.search("two")


def test_parse_rate():
    assert parse_rate("3/10") == (3, 10.0)
    assert parse_rate("bad") == (3, 10.0)


# --- result page parsers ---------------------------------------------------------


def test_parse_startpage():
    rs = parse_startpage(STARTPAGE_HTML)
    assert [r.url for r in rs] == ["https://alpha.example/guide", "https://beta.example/post/42"]
    assert rs[0].title == "Alpha guide to widgets"
    assert rs[0].snippet.startswith("12 Mar 2025 ... A short guide")
    assert "css" not in rs[0].title


def test_parse_brave():
    rs = parse_brave(BRAVE_HTML)
    assert [r.url for r in rs] == ["https://gamma.example/widgets", "https://delta.example/q/1"]
    assert rs[0].title == "Gamma widgets explained"
    assert rs[0].snippet == "June 4, 2024 - Widgets explained with diagrams and examples."
    assert rs[1].title == "Delta Q&A: are widgets hard?"


def test_parsers_survive_garbage():
    assert parse_startpage("") == [] and parse_brave("<html><body>nothing</body></html>") == []
