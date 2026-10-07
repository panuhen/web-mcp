import httpx
import pytest

from web_mcp.search import Result, SearchError, Searcher, render, shape, trim

SEARX = "http://searxng.test:8080"


def searx_payload(n=8, unresponsive=None, dup=True):
    results = [
        {
            "title": f"Result {i}",
            "url": f"https://site{i}.example/page",
            "content": "word " * 100,
            "engine": "bing",
            "engines": ["bing", "google cse"] if i % 2 else ["bing"],
            "publishedDate": "2026-09-01T10:00:00" if i == 0 else None,
        }
        for i in range(n)
    ]
    if dup and n:
        results.insert(1, dict(results[0], url="https://www.site0.example/page/", title="dup"))
    return {"query": "q", "results": results, "unresponsive_engines": unresponsive or []}


def make(handler, **kw) -> Searcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Searcher(SEARX, client=client, **kw)


def route(searx=None, brave=None, exa=None, calls=None):
    def handler(request: httpx.Request):
        host = request.url.host
        if calls is not None:
            calls.append(host)
        if host == "searxng.test":
            if isinstance(searx, Exception):
                raise searx
            return httpx.Response(200, json=searx) if not isinstance(searx, httpx.Response) else searx
        if host == "api.search.brave.com":
            assert request.headers["X-Subscription-Token"] == "brave-key"
            return brave if isinstance(brave, httpx.Response) else httpx.Response(200, json=brave)
        if host == "api.exa.ai":
            assert request.headers["x-api-key"] == "exa-key"
            return exa if isinstance(exa, httpx.Response) else httpx.Response(200, json=exa)
        raise AssertionError(host)

    return handler


BRAVE = {"web": {"results": [{"title": "B1", "url": "https://b1.example/", "description": "<strong>brave</strong> snippet", "page_age": "2026-01-02T00:00:00"}]}}
EXA = {"results": [{"title": "E1", "url": "https://e1.example/", "text": "exa text", "publishedDate": "2025-05-05T00:00:00.000Z"}]}


async def test_searxng_results_are_shaped_and_deduplicated():
    s = make(route(searx=searx_payload()))
    out = await s.search("q", 6)
    assert out.provider == "searxng"
    assert [r.url for r in out.results] == [f"https://site{i}.example/page" for i in range(6)]
    assert out.results[0].published == "2026-09-01"
    assert all(len(r.snippet) <= 241 for r in out.results)
    assert out.results[1].engines == ["bing", "google cse"]


async def test_no_keys_means_no_fallback_calls():
    calls = []
    s = make(route(searx=searx_payload(0), calls=calls))
    out = await s.search("q")
    assert out.provider == "searxng" and out.results == []
    assert calls == ["searxng.test"]


async def test_empty_searxng_uses_brave_when_key_set():
    s = make(route(searx=searx_payload(0), brave=BRAVE), brave_api_key="brave-key")
    out = await s.search("q")
    assert out.provider == "brave"
    assert out.results[0].snippet == "brave snippet"
    assert "fallback" in out.note


async def test_brave_rate_limited_then_exa():
    s = make(
        route(searx=searx_payload(0), brave=httpx.Response(429), exa=EXA),
        brave_api_key="brave-key",
        exa_api_key="exa-key",
    )
    out = await s.search("q")
    assert out.provider == "exa"
    assert out.results[0].published == "2025-05-05"


async def test_most_engines_unresponsive_triggers_fallback():
    payload = searx_payload(2, unresponsive=[["brave", "too many requests"], ["duckduckgo", "CAPTCHA"], ["startpage", "timeout"]], dup=False)
    for r in payload["results"]:
        r["engines"] = ["bing"]
    s = make(route(searx=payload, exa=EXA), exa_api_key="exa-key")
    out = await s.search("q", 6)
    assert out.provider == "exa"


async def test_thin_searxng_kept_when_fallback_fails():
    payload = searx_payload(2, unresponsive=[["a", "x"], ["b", "y"], ["c", "z"]], dup=False)
    for r in payload["results"]:
        r["engines"] = ["bing"]
    s = make(route(searx=payload, exa=httpx.Response(500)), exa_api_key="exa-key")
    out = await s.search("q", 6)
    assert out.provider == "searxng" and len(out.results) == 2


async def test_searxng_down_without_fallback_is_a_clear_error():
    s = make(route(searx=httpx.ConnectError("refused")))
    with pytest.raises(SearchError) as e:
        await s.search("q")
    msg = str(e.value)
    assert "SearXNG is not reachable" in msg and "No fallback provider" in msg


async def test_searxng_down_with_exa():
    s = make(route(searx=httpx.ConnectError("refused"), exa=EXA), exa_api_key="exa-key")
    out = await s.search("q")
    assert out.provider == "exa"
    assert "not reachable" in out.note


async def test_json_format_disabled_message():
    s = make(route(searx=httpx.Response(403)))
    with pytest.raises(SearchError, match="json"):
        await s.search("q")


async def test_empty_query():
    with pytest.raises(SearchError):
        await make(route()).search("  ")


def test_render_names_provider():
    from web_mcp.search import SearchOutcome

    out = render("q", SearchOutcome("searxng", [Result("T", "https://x.example/", "snip", ["bing"], "2026-01-01")]))
    assert "provider: searxng" in out and "untrusted" in out
    assert "engines: bing · published: 2026-01-01" in out


def test_shape_drops_non_http_and_caps():
    rs = [Result("a", "javascript:1"), Result("b", "https://b.example/")] + [Result(str(i), f"https://{i}.example/") for i in range(30)]
    out = shape(rs, 5)
    assert len(out) == 5 and out[0].url == "https://b.example/"


def test_trim():
    assert trim("a " * 500).endswith("…")
    assert len(trim("a " * 500)) <= 241
    assert trim("  short  ") == "short"
