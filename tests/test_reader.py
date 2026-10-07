import asyncio
import json
import time
from pathlib import Path

import pytest

from web_mcp.egress import BlockedURL, CheckedURL, check_url
from web_mcp.fetchers import BrowserPage, FetchError, RawResponse
from web_mcp.reader import Page, PageReader, ReadError, render
from web_mcp.state import DomainMemory, TTLCache

FIX = Path(__file__).parent / "fixtures"
ARTICLE = (FIX / "normal_article.html").read_text()
CF = (FIX / "cloudflare_jsc.html").read_text()
SHELL = (FIX / "enable_js.html").read_text()


async def allow_all(url: str) -> CheckedURL:
    if not url.startswith(("http://", "https://")):
        raise BlockedURL("only http and https URLs are allowed")
    host = url.split("/")[2]
    return CheckedURL(url, url.split(":")[0], host, 443, ["93.184.215.14"])


def html(body: str, status: int = 200, url: str = "https://site.example/a", headers=None) -> RawResponse:
    h = {"content-type": "text/html; charset=utf-8"}
    h.update(headers or {})
    return RawResponse(url=url, status=status, headers=h, body=body.encode())


class FakeHttp:
    """Answers by URL prefix; records calls."""

    def __init__(self, routes: dict, delay: float = 0.0):
        self.routes = routes
        self.calls: list[str] = []
        self.delay = delay

    async def fetch(self, url: str, timeout: float) -> RawResponse:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise FetchError("connection refused or not reachable", "network")


class FakeBrowser:
    name = "browser"

    def __init__(self, page: BrowserPage | Exception | None = None, delay: float = 0.0):
        self.page = page
        self.calls: list[str] = []
        self.delay = delay
        self.cancelled = False

    async def fetch(self, url, timeout, challenge_wait):
        self.calls.append(url)
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    async def close(self):
        pass

    def state(self):
        return {}


def wayback(available=True):
    snap = {"available": True, "url": "http://web.archive.org/web/20240102030405/https://site.example/a", "timestamp": "20240102030405", "status": "200"}
    body = {"url": "x", "archived_snapshots": {"closest": snap} if available else {}}
    return RawResponse("https://archive.org/wayback/available", 200, {"content-type": "application/json"}, json.dumps(body).encode())


def reader(http, browser, **kw) -> PageReader:
    kw.setdefault("url_checker", allow_all)
    return PageReader(http, browser, **kw)


URL = "https://site.example/a"


async def test_http_success_uses_step_one_only():
    http = FakeHttp({URL: html(ARTICLE)})
    browser = FakeBrowser()
    r = reader(http, browser)
    page, cached = await r.read_page(URL)
    assert page.method == "http" and not cached
    assert "river transport" in page.text
    assert browser.calls == []


async def test_403_escalates_to_browser_and_remembers_domain():
    http = FakeHttp({URL: html(CF, status=403)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="Field notes"))
    memory = DomainMemory()
    r = reader(http, browser, memory=memory, cache=TTLCache(0.0))
    page, _ = await r.read_page(URL)
    assert page.method == "browser"
    assert memory.is_blocked("site.example")
    # Next read of the same domain skips plain HTTP.
    http.calls.clear()
    page, _ = await r.read_page("https://site.example/other")
    assert http.calls == []
    assert page.method == "browser"


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_block_statuses_escalate(status):
    http = FakeHttp({URL: html("<html><body>nope</body></html>", status=status)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"))
    page, _ = await reader(http, browser).read_page(URL)
    assert page.method == "browser"


async def test_challenge_with_200_escalates():
    http = FakeHttp({URL: html(CF, status=200)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"))
    page, _ = await reader(http, browser).read_page(URL)
    assert page.method == "browser"


async def test_js_shell_escalates():
    http = FakeHttp({URL: html(SHELL)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"))
    memory = DomainMemory()
    page, _ = await reader(http, browser, memory=memory).read_page(URL)
    assert page.method == "browser"
    assert not memory.is_blocked("site.example")  # needing JS is not a block


async def test_browser_challenge_not_cleared_falls_to_archive():
    http = FakeHttp(
        {
            URL: html(CF, status=403),
            "https://archive.org/wayback/available": wayback(),
            "https://web.archive.org/web/20240102030405id_/": html(ARTICLE),
        }
    )
    browser = FakeBrowser(BrowserPage(url=URL, status=403, html=CF, title="Just a moment..."))
    page, _ = await reader(http, browser).read_page(URL)
    assert page.method == "archive"
    assert "2024-01-02" in page.note
    assert page.url.startswith("https://web.archive.org/web/20240102030405/")


async def test_404_skips_browser_and_tries_archive():
    http = FakeHttp({URL: html("<html>not found</html>", status=404), "https://archive.org/wayback/available": wayback(False)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"))
    with pytest.raises(ReadError) as e:
        await reader(http, browser).read_page(URL)
    assert browser.calls == []
    msg = str(e.value)
    assert "http: HTTP 404" in msg and "archive: no snapshot" in msg


async def test_everything_fails_gives_short_clear_message():
    http = FakeHttp({URL: html(CF, status=403), "https://archive.org/wayback/available": wayback(False)})
    browser = FakeBrowser(FetchError("page load timed out", "timeout"))
    with pytest.raises(ReadError) as e:
        await reader(http, browser).read_page(URL)
    msg = str(e.value)
    assert msg.startswith("Blocked or not reachable.")
    assert "http: blocked (cloudflare, HTTP 403)" in msg
    assert "browser: page load timed out" in msg
    assert "archive: no snapshot" in msg
    assert URL not in msg


async def test_deadline_is_enforced_and_cancels_in_flight_work():
    http = FakeHttp({URL: html(CF, status=403)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"), delay=30)
    r = reader(http, browser, deadline=3, archive_enabled=False)
    t0 = time.monotonic()
    with pytest.raises(ReadError) as e:
        await r.read_page(URL)
    assert time.monotonic() - t0 < 4
    assert "deadline" in str(e.value)
    assert browser.cancelled  # the browser step was cancelled, not left running


async def test_cache_serves_second_read():
    http = FakeHttp({URL: html(ARTICLE)})
    r = reader(http, FakeBrowser())
    await r.read_page(URL)
    page, cached = await r.read_page(URL + "#section")
    assert cached and len(http.calls) == 1
    assert "cached" in render(page, 500, cached=True)


async def test_failures_are_not_cached():
    http = FakeHttp({URL: html("x", status=404), "https://archive.org/wayback/available": wayback(False)})
    r = reader(http, None)
    for _ in range(2):
        with pytest.raises(ReadError):
            await r.read_page(URL)
    assert http.calls.count(URL) == 2


async def test_ssrf_refused_before_any_fetch():
    http = FakeHttp({})
    r = PageReader(http, FakeBrowser(), url_checker=check_url)
    with pytest.raises(ReadError) as e:
        await r.read_page("http://127.0.0.1:8888/search?q=secret")
    assert "Refused" in str(e.value)
    assert http.calls == []


async def test_redirect_refusal_does_not_escalate():
    http = FakeHttp({URL: FetchError("refused: address refused: non-public address", "blocked-url")})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html=ARTICLE, title="t"))
    with pytest.raises(ReadError) as e:
        await reader(http, browser).read_page(URL)
    assert browser.calls == []
    assert "refused" in str(e.value)


async def test_pdf_from_browser_body():
    from tests.pdfgen import tiny_pdf

    http = FakeHttp({URL: html("x", status=403)})
    browser = FakeBrowser(BrowserPage(url=URL, status=200, html="", title="", content_type="application/pdf", body=tiny_pdf("Hello PDF world")))
    page, _ = await reader(http, browser).read_page(URL)
    assert page.kind == "pdf" and "Hello PDF world" in page.text


def test_truncation_note_and_header():
    page = Page(text="line one.\n" * 2000, title="T", url=URL, method="http", kind="html")
    out = render(page, 1000)
    assert out.startswith(f"Content of {URL} (untrusted web page")
    assert "Fetched via: http" in out
    assert "[Truncated: showing" in out and "of 20,000 characters" in out
    body = out.split("---\n", 1)[1]
    assert len(body.split("\n\n[Truncated")[0]) <= 1000


def test_no_truncation_note_when_short():
    page = Page(text="short", title="", url=URL, method="archive", kind="text", note="Wayback Machine snapshot from 2024-01-02")
    out = render(page, 8000)
    assert "Truncated" not in out
    assert "archive (Wayback Machine snapshot from 2024-01-02)" in out


def test_max_chars_is_clamped():
    page = Page(text="x" * 300_000, title="", url=URL, method="http", kind="text")
    assert "showing 100,000 of" in render(page, 10**9)
    assert "showing 200 of" in render(page, -5)


async def test_cached_text_is_never_served_for_another_url():
    a, b = "https://site.example/a", "https://site.example/b"
    http = FakeHttp({a: html(ARTICLE.replace("river", "ALPHA"), url=a), b: html(ARTICLE.replace("river", "BRAVO"), url=b)})
    r = reader(http, None)
    pa, _ = await r.read_page(a)
    pb, cached = await r.read_page(b)
    assert not cached and "BRAVO" in pb.text and "ALPHA" not in pb.text
    pa2, cached = await r.read_page(a + "?x=1")
    assert not cached  # a different query string is a different URL
