"""Size, time and concurrency caps, and no state carried between calls.

These tests run a fake HTTP server on loopback. That is the only place
loopback is allowed: the `loopback` fixture flips the test-only switch in
web_mcp.egress and always turns it off again.
"""

import asyncio
import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brotli
import pytest

from tests.pdfgen import tiny_pdf
from web_mcp import egress
from web_mcp.egress import EgressProxy
from web_mcp.fetchers import CamoufoxFetcher, FetchError, HttpFetcher
from web_mcp.pdfworker import extract_pdf_isolated
from web_mcp.reader import PageReader, ReadError

CAP = 1_000_000  # 1 MB cap for these tests

GZIP_BOMB = gzip.compress(b"\0" * 200_000_000, compresslevel=9)          # ~200 KB -> 200 MB
BROTLI_BOMB = brotli.compress(b"\0" * 200_000_000, quality=5)            # ~ tiny -> 200 MB


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    inflight = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, status, body: bytes, ctype="text/html", extra=None, length=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/huge-no-length":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = b"<p>" + b"a" * 65536 + b"</p>"
            try:
                for _ in range(200):  # ~13 MB, more than the cap
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif p == "/huge-declared":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(50 * CAP))
            self.end_headers()
            try:
                self.wfile.write(b"x" * 1000)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif p == "/lying-length":
            # Claims to be small, then keeps sending.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"y" * 65536)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif p == "/gzip-bomb":
            self._send(200, GZIP_BOMB, extra={"Content-Encoding": "gzip"})
        elif p == "/brotli-bomb":
            self._send(200, BROTLI_BOMB, extra={"Content-Encoding": "br"})
        elif p == "/big-pdf":
            self._send(200, b"%PDF-1.4\n" + b"0" * (3 * CAP), ctype="application/pdf", length=False)
        elif p == "/slow-drip":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for _ in range(60):
                    self.wfile.write(b"a")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif p == "/set-cookie":
            self._send(
                200,
                b"<html><body><p>cookie set</p><script>try{localStorage.setItem('k','leak')}catch(e){}</script></body></html>",
                extra={"Set-Cookie": "session=secret123; Path=/; Max-Age=3600"},
            )
        elif p == "/echo":
            cookie = (self.headers.get("Cookie") or "none").encode()
            body = b"<html><head><title>echo</title></head><body><p id=c>COOKIE:" + cookie + b"</p><p id=s>STORAGE:</p><script>document.getElementById('s').textContent='STORAGE:'+(localStorage.getItem('k')||'none')</script></body></html>"
            self._send(200, body)
        elif p == "/slow-page":
            with Handler.lock:
                Handler.inflight += 1
                Handler.peak = max(Handler.peak, Handler.inflight)
            try:
                time.sleep(1.5)
                self._send(200, b"<html><head><title>slow</title></head><body><p>" + b"slow page text. " * 50 + b"</p></body></html>")
            finally:
                with Handler.lock:
                    Handler.inflight -= 1
        elif p == "/big-dom":
            self._send(200, b"<html><head><title>big</title></head><body><script>document.body.textContent='z'.repeat(5000000)</script></body></html>")
        elif p == "/reload-loop":
            self._send(200, b"<html><head><title>loop</title></head><body><p>hello</p><script>setTimeout(()=>location.reload(),50)</script></body></html>")
        else:
            self._send(404, b"not found")


@pytest.fixture
def loopback():
    egress.allow_loopback_for_tests(True)
    try:
        yield
    finally:
        egress.allow_loopback_for_tests(False)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
async def proxy():
    p = EgressProxy()
    await p.start()
    yield p
    await p.close()


def test_switch_is_off_by_default():
    assert egress._LOOPBACK_ALLOWED_FOR_TESTS is False
    assert egress.blocked_reason("127.0.0.1") is not None


# --- HTTP step -----------------------------------------------------------------


async def test_body_without_length_is_cut_at_cap(loopback, server, proxy):
    f = HttpFetcher(proxy.url, CAP)
    t0 = time.monotonic()
    r = await f.fetch(server + "/huge-no-length", 10)
    assert len(r.body) <= CAP
    assert time.monotonic() - t0 < 5


async def test_declared_oversize_is_refused_early(loopback, server, proxy):
    with pytest.raises(FetchError) as e:
        await HttpFetcher(proxy.url, CAP).fetch(server + "/huge-declared", 10)
    assert e.value.kind == "too-large"


async def test_lying_content_length_is_not_trusted(loopback, server, proxy):
    r = await HttpFetcher(proxy.url, CAP).fetch(server + "/lying-length", 10)
    assert len(r.body) <= CAP


@pytest.mark.parametrize("path", ["/gzip-bomb", "/brotli-bomb"])
async def test_decompression_bomb_is_capped(loopback, server, proxy, path):
    t0 = time.monotonic()
    r = await HttpFetcher(proxy.url, CAP).fetch(server + path, 20)
    assert len(r.body) <= CAP  # decompressed bytes, not wire bytes
    assert time.monotonic() - t0 < 10


async def test_oversized_pdf_is_refused(loopback, server, proxy):
    with pytest.raises(FetchError) as e:
        await HttpFetcher(proxy.url, CAP).fetch(server + "/big-pdf", 10)
    assert e.value.kind == "too-large"


async def test_slow_drip_hits_the_timeout(loopback, server, proxy):
    t0 = time.monotonic()
    with pytest.raises(FetchError) as e:
        await HttpFetcher(proxy.url, CAP).fetch(server + "/slow-drip", 2)
    assert e.value.kind == "timeout"
    assert time.monotonic() - t0 < 4


async def test_reader_deadline_stops_slow_drip(loopback, server, proxy):
    r = PageReader(HttpFetcher(proxy.url, CAP), None, deadline=2.5, http_timeout=30, archive_enabled=False)
    t0 = time.monotonic()
    with pytest.raises(ReadError):
        await r.read_page(server + "/slow-drip")
    assert time.monotonic() - t0 < 4


async def test_http_calls_do_not_share_cookies(loopback, server, proxy):
    f = HttpFetcher(proxy.url, CAP)
    await f.fetch(server + "/set-cookie", 5)
    r = await f.fetch(server + "/echo", 5)
    assert b"COOKIE:none" in r.body


# --- PDF worker ------------------------------------------------------------------


async def test_pdf_worker_extracts_in_child_process():
    ex = await extract_pdf_isolated(tiny_pdf("Child process text"), 20)
    assert "Child process text" in ex.text


async def test_pdf_worker_is_killed_at_timeout(monkeypatch):
    import sys

    from web_mcp import pdfworker
    from web_mcp.extract import UnsupportedContent

    # Stand-in for a PDF that makes pypdf spin: a child that never answers.
    real = asyncio.create_subprocess_exec

    async def sleeper(*args, **kw):
        return await real(sys.executable, "-c", "import time; time.sleep(60)", **kw)

    monkeypatch.setattr(pdfworker.asyncio, "create_subprocess_exec", sleeper)
    t0 = time.monotonic()
    with pytest.raises(UnsupportedContent, match="too long"):
        await extract_pdf_isolated(b"%PDF-1.4", 1.0)
    assert time.monotonic() - t0 < 3


# --- Browser concurrency (no real browser) -----------------------------------------


async def test_browser_page_limit_holds_under_load_and_cancellation():
    f = CamoufoxFetcher(None, max_pages=2)
    running = 0
    peak = 0

    class Ctx:
        async def new_page(self):
            return object()

        async def close(self):
            pass

    async def fake_browser():
        return object()

    async def fake_context(_b):
        return Ctx()

    async def fake_load(page, url, end, wait):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.3 if "fast" in url else 5)
        finally:
            running -= 1
        return "ok"

    f._ensure_browser = fake_browser
    f._new_context = fake_context
    f._load = fake_load

    tasks = [asyncio.create_task(f.fetch(f"https://x.example/{'slow' if i % 3 == 0 else 'fast'}{i}", 10, 1)) for i in range(12)]
    await asyncio.sleep(0.1)
    # Cancel some in-flight and some waiting calls; time out others.
    for t in tasks[:6:2]:
        t.cancel()
    results = await asyncio.gather(*(asyncio.wait_for(t, 3) for t in tasks), return_exceptions=True)
    assert peak <= 2
    assert f._active == 0
    assert f._sem._value == 2  # every slot was released
    assert any(r == "ok" for r in results)


# --- Real browser: isolation and caps ----------------------------------------------


def _camoufox_installed() -> bool:
    try:
        from camoufox.pkgman import installed_verstr

        return bool(installed_verstr())
    except Exception:
        return False


browser_only = pytest.mark.skipif(not _camoufox_installed(), reason="Camoufox browser not fetched")


@pytest.fixture
async def browser(proxy):
    f = CamoufoxFetcher(proxy.browser_server, headless="true", max_page_chars=100_000)
    yield f
    await f.close()


@browser_only
async def test_browser_calls_do_not_share_cookies_or_storage(loopback, server, browser):
    await browser.fetch(server + "/set-cookie", 20, 2)
    page = await browser.fetch(server + "/echo", 20, 2)
    assert "COOKIE:none" in page.html
    assert "STORAGE:none" in page.html
    assert browser.launches == 1  # same warm process, fresh context


@browser_only
async def test_browser_dom_copy_is_capped(loopback, server, browser):
    page = await browser.fetch(server + "/big-dom", 20, 2)
    assert len(page.html) <= 100_000


@browser_only
async def test_browser_navigation_cap(loopback, server, browser):
    browser.max_navigations = 3
    with pytest.raises(FetchError, match="navigations"):
        await browser.fetch(server + "/reload-loop", 15, 2)


@browser_only
async def test_browser_two_pages_at_once_under_concurrent_calls(loopback, server, browser):
    Handler.peak = 0
    await browser.fetch(server + "/echo", 20, 1)  # warm up
    results = await asyncio.gather(*(browser.fetch(server + f"/slow-page?{i}", 30, 1) for i in range(5)), return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results), results
    assert Handler.peak <= 2
    assert browser.peak_active <= 2


@browser_only
async def test_browser_cancellation_releases_slot_and_closes_context(loopback, server, browser):
    await browser.fetch(server + "/echo", 20, 1)  # warm up
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(browser.fetch(server + "/slow-page?c", 30, 1), 0.5)
    assert browser._active == 0 and browser._sem._value == 2
    contexts = browser._browser.contexts
    assert contexts == []  # the cancelled call's context was closed
