"""Fetchers for the read_page ladder.

- `HttpFetcher`: curl_cffi with a Chrome TLS/HTTP2 fingerprint. Redirects are
  followed by hand so each hop is checked by the SSRF guard first.
- `StealthFetcher`: the interface for the browser step. `CamoufoxFetcher` is the
  first implementation; a Scrapling/Patchright one can be dropped in later.

All traffic goes through the loopback `EgressProxy`, which refuses non-public
addresses at connect time.
"""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .egress import BlockedURL, check_url
from .extract import is_supported, media_type

log = logging.getLogger("web_mcp.fetch")

# Camoufox suggests geoip=True whenever a proxy is set. Our proxy is the local
# egress guard, so the exit IP is the machine's own; set TZ instead.
warnings.filterwarnings("ignore", message=".*geoip.*")

REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# Query parameters that anti-bot challenges append to the URL after they pass.
CHALLENGE_PARAMS = {"solution", "js_challenge", "jsc_token", "jsc_orig_r", "__cf_chl_tk", "__cf_chl_rt_tk", "__cf_chl_f_tk", "__cf_chl_jschl_tk__", "__cf_chl_captcha_tk__"}


def strip_challenge_params(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in CHALLENGE_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


class FetchError(Exception):
    """A fetch failed. `kind` tells the ladder what to do next."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # error | blocked-url | unsupported | too-large | network | timeout


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    method: str = "http"

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass
class BrowserPage:
    url: str
    status: int | None
    html: str
    title: str
    content_type: str = "text/html"
    body: bytes | None = None   # set for non-HTML documents the browser returned
    challenge_cleared: bool = False
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Step 1: plain HTTP


DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}


class HttpFetcher:
    name = "http"

    def __init__(self, proxy_url: str | None, max_bytes: int, max_redirects: int = 8, impersonate: str = "chrome"):
        self.proxy_url = proxy_url
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.impersonate = impersonate

    async def fetch(self, url: str, timeout: float) -> RawResponse:
        # curl's own timeout does not bound a slow-drip body read, so the
        # whole call (all hops, all body chunks) runs under one hard timeout.
        try:
            async with asyncio.timeout(timeout):
                return await self._fetch(url, timeout)
        except TimeoutError:
            raise FetchError("timed out", "timeout") from None

    async def _fetch(self, url: str, timeout: float) -> RawResponse:
        from curl_cffi import CurlError, CurlOpt
        from curl_cffi.requests import AsyncSession

        current = url
        deadline = time.monotonic() + timeout
        # A fresh session per call: no cookies carried between unrelated reads.
        # Stream mode sets no total timeout in curl, so add one: each transfer
        # stops by itself even if we stop waiting for it.
        async with AsyncSession(
            impersonate=self.impersonate,
            proxy=self.proxy_url,
            headers=DEFAULT_HEADERS,
            curl_options={CurlOpt.TIMEOUT_MS: int(timeout * 1000)},
        ) as s:
            for _hop in range(self.max_redirects + 1):
                try:
                    await check_url(current)
                except BlockedURL as e:
                    raise FetchError(f"refused: {e}", "blocked-url") from None
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    raise FetchError("timed out", "timeout")
                try:
                    r = await s.request(
                        "GET",
                        current,
                        stream=True,
                        allow_redirects=False,
                        timeout=remaining,
                    )
                except CurlError as e:
                    raise _curl_error(e) from None
                try:
                    status = r.status_code
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    if status in REDIRECT_STATUSES and headers.get("location"):
                        current = urljoin(current, headers["location"])
                        continue
                    return RawResponse(url=current, status=status, headers=headers, body=await self._read_body(r, headers))
                except CurlError as e:
                    raise _curl_error(e) from None
                finally:
                    await _abort_stream(r)
            raise FetchError("too many redirects", "error")

    async def _read_body(self, r, headers: dict[str, str]) -> bytes:
        ct = headers.get("content-type", "")
        if r.status_code < 400 and not is_supported(ct):
            raise FetchError(f"unsupported content type {media_type(ct)}", "unsupported")
        try:
            declared = int(headers.get("content-length", "0") or 0)
        except ValueError:
            declared = 0
        if declared > self.max_bytes:  # early refusal only; never trusted as a limit
            raise FetchError("response too large", "too-large")
        chunks: list[bytes] = []
        size = 0
        truncated = False
        # Chunks are decompressed bytes (curl decodes gzip/br/zstd), so this
        # caps the decoded size and stops decompression bombs.
        async for chunk in r.aiter_content():
            chunks.append(chunk)
            size += len(chunk)
            if size > self.max_bytes:
                truncated = True
                break
        body = b"".join(chunks)
        if truncated:
            if "pdf" in media_type(ct) or body[:5] == b"%PDF-":
                raise FetchError("response too large", "too-large")
            body = body[: self.max_bytes]
        return body


async def _abort_stream(r) -> None:
    """Stop a curl_cffi stream now instead of waiting for the body to finish."""
    quit_now = getattr(r, "quit_now", None)
    if quit_now is not None:
        quit_now.set()  # the write callback aborts the transfer at the next chunk
    try:
        await asyncio.wait_for(asyncio.shield(r.aclose()), 1.0)
    except BaseException:
        pass  # a stalled transfer ends at curl's TIMEOUT_MS on its own


def _curl_error(e: Exception) -> FetchError:
    msg = str(e).lower()
    if "proxy" in msg or "socks" in msg:
        # The egress guard refused the target (or it was unreachable).
        return FetchError("connection refused or not reachable", "network")
    if "timed out" in msg or "timeout" in msg:
        return FetchError("timed out", "timeout")
    return FetchError(f"network error ({type(e).__name__})", "network")


# ---------------------------------------------------------------------------
# Step 2: stealth browser


@runtime_checkable
class StealthFetcher(Protocol):
    """A browser that renders a page, waits out challenges and returns the HTML."""

    name: str

    async def fetch(self, url: str, timeout: float, challenge_wait: float) -> BrowserPage: ...

    async def close(self) -> None: ...

    def state(self) -> dict: ...


# JS run in the page: return at most N characters of the serialised DOM, so a
# page that builds a huge DOM cannot make us copy all of it out of the browser.
_CAPPED_HTML_JS = "(n) => { const h = document.documentElement ? document.documentElement.outerHTML : ''; return h.length > n ? h.slice(0, n) : h; }"

MAX_PAGE_CHARS = 3_000_000   # serialised DOM we copy out of the browser
MAX_NAVIGATIONS = 12         # main-frame navigations per call (challenge reloads included)
SETTLE_MAX = 8.0             # seconds to wait for JS-rendered text after load


class CamoufoxFetcher:
    """Camoufox (anti-detect Firefox) via Playwright.

    The browser *process* starts on first use, stays warm and closes after
    `idle_close` seconds without use. Every call gets its own fresh browser
    context (own cookies, storage, cache, fingerprint), closed when the call
    ends, so nothing carries from one read to the next. At most `max_pages`
    calls run at once. Never shows a window: real headless mode, or Camoufox's
    virtual display (Xvfb) inside the container.
    """

    name = "browser"

    def __init__(
        self,
        proxy_server: str | None,
        headless: str = "true",
        idle_close: float = 300.0,
        max_pages: int = 2,
        max_redirects: int = 8,
        max_page_chars: int = MAX_PAGE_CHARS,
        max_navigations: int = MAX_NAVIGATIONS,
    ):
        self.proxy_server = proxy_server
        self.headless: bool | str = "virtual" if headless == "virtual" else True
        self.idle_close = idle_close
        self.max_pages = max_pages
        self.max_redirects = max_redirects
        self.max_page_chars = max_page_chars
        self.max_navigations = max_navigations
        self._sem = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._active = 0
        self.peak_active = 0
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None
        self.launches = 0

    def state(self) -> dict:
        return {
            "warm": self._browser is not None,
            "active_pages": self._active,
            "launches": self.launches,
            "idle_for_s": round(time.monotonic() - self._last_used, 1) if self._last_used else None,
        }

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None:
                return self._browser
            from camoufox.async_api import AsyncNewBrowser
            from playwright.async_api import async_playwright

            prefs = {
                # Send localhost through the proxy too, so the egress guard sees it.
                "network.proxy.allow_hijacking_localhost": True,
                "network.proxy.no_proxies_on": "",
                "network.proxy.socks_remote_dns": True,
                "network.http.http3.enable": False,
                "network.http.redirection-limit": self.max_redirects,
                "network.dns.disablePrefetch": True,
                "network.prefetch-next": False,
                "network.predictor.enabled": False,
                "browser.download.enabled": False,
                "dom.popup_maximum": 0,
                "media.autoplay.default": 5,       # no audio/video autoplay downloads
                "media.preload.default": 0,
            }
            try:
                self._pw = await async_playwright().start()
                kwargs = dict(
                    headless=self.headless,
                    block_webrtc=True,
                    firefox_user_prefs=prefs,
                    i_know_what_im_doing=True,
                )
                if self.proxy_server:
                    kwargs["proxy"] = {"server": self.proxy_server}
                self._browser = await AsyncNewBrowser(self._pw, **kwargs)
            except BaseException:
                await self._shutdown()
                raise
            self.launches += 1
            self._last_used = time.monotonic()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_watch())
            log.info("browser started")
            return self._browser

    async def _new_context(self, browser):
        from camoufox.async_api import AsyncNewContext

        return await AsyncNewContext(browser, accept_downloads=False, service_workers="block")

    async def _idle_watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(30.0, max(1.0, self.idle_close / 4)))
                if self._browser is None:
                    return
                if self._active == 0 and time.monotonic() - self._last_used >= self.idle_close:
                    async with self._lock:
                        if self._active == 0 and time.monotonic() - self._last_used >= self.idle_close:
                            await self._shutdown()
                            log.info("browser closed after idle")
                            return
        except asyncio.CancelledError:
            pass

    async def _shutdown(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await asyncio.wait_for(getattr(obj, meth)(), 10)
                except BaseException:
                    pass
        self._browser = self._pw = None

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            await self._shutdown()

    async def fetch(self, url: str, timeout: float, challenge_wait: float) -> BrowserPage:
        end = time.monotonic() + timeout
        # The semaphore is released by `async with` on every exit path,
        # including timeouts and cancellation.
        async with self._sem:
            self._active += 1
            self.peak_active = max(self.peak_active, self._active)
            context = None
            try:
                try:
                    browser = await asyncio.wait_for(self._ensure_browser(), max(1.0, end - time.monotonic()))
                    context = await asyncio.wait_for(self._new_context(browser), max(1.0, end - time.monotonic()))
                except asyncio.TimeoutError:
                    raise FetchError("browser start timed out", "timeout") from None
                page = await context.new_page()
                return await self._load(page, url, end, challenge_wait)
            except FetchError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A dead browser gets restarted on the next call.
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    async with self._lock:
                        await self._shutdown()
                raise FetchError(f"browser error ({type(e).__name__})", "network") from None
            finally:
                # Closing the context stops everything the page was doing and
                # drops its cookies, storage and cache. Shielded so a
                # cancellation (deadline) cannot skip it.
                if context is not None:
                    try:
                        await asyncio.shield(asyncio.wait_for(context.close(), 5))
                    except BaseException:
                        pass
                self._active -= 1
                self._last_used = time.monotonic()

    async def _html(self, page) -> str:
        from playwright.async_api import Error as PWError

        try:
            return await page.evaluate(_CAPPED_HTML_JS, self.max_page_chars) or ""
        except PWError:
            return ""

    async def _load(self, page, url, end, challenge_wait) -> BrowserPage:
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout

        from .detect import detect_challenge, visible_text_len

        def left_ms(cap: float | None = None) -> float:
            left = max(0.2, end - time.monotonic())
            return 1000 * (min(left, cap) if cap else left)

        navigations = 0
        last_nav = time.monotonic()

        def on_nav(frame):
            nonlocal navigations, last_nav
            if frame == page.main_frame:
                navigations += 1
                last_nav = time.monotonic()
                if navigations == self.max_navigations + 1:
                    # Stop a reload/redirect loop right away, not at the next check.
                    asyncio.ensure_future(page.close())

        page.on("framenavigated", on_nav)

        status: int | None = None
        content_type = "text/html"
        body: bytes | None = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=left_ms())
            if resp is not None:
                status = resp.status
                headers = await resp.all_headers()
                content_type = headers.get("content-type", "text/html")
                mt = media_type(content_type)
                if mt and not mt.startswith(("text/html", "application/xhtml")):
                    try:
                        declared = int(headers.get("content-length", "0") or 0)
                    except ValueError:
                        declared = 0
                    if declared > MAX_PAGE_CHARS * 4:
                        raise FetchError("response too large", "too-large")
                    try:
                        body = await resp.body()
                    except PWError:
                        body = None
                    if body is not None and len(body) > MAX_PAGE_CHARS * 4:
                        raise FetchError("response too large", "too-large")
        except PWTimeout:
            raise FetchError("page load timed out", "timeout") from None
        except PWError as e:
            msg = str(e)
            if "Download is starting" in msg:
                raise FetchError("the URL is a file download", "unsupported") from None
            if "NS_ERROR_PROXY" in msg or "NS_ERROR_UNKNOWN_HOST" in msg or "NS_ERROR_CONNECTION_REFUSED" in msg:
                raise FetchError("connection refused or not reachable", "network") from None
            if "NS_ERROR_REDIRECT_LOOP" in msg:
                raise FetchError("too many redirects", "error") from None
            raise FetchError(f"navigation failed ({msg.splitlines()[0][:80]})", "network") from None

        cleared = False
        html = ""
        if body is None:
            # Wait for a challenge to clear (the page usually reloads itself).
            challenge_end = min(end - 0.5, time.monotonic() + challenge_wait)
            first = True
            while True:
                if navigations > self.max_navigations:
                    raise FetchError("too many page navigations", "error")
                html = await self._html(page)
                reason = detect_challenge(html, status if first else None)
                if not reason:
                    cleared = not first
                    break
                first = False
                if time.monotonic() >= challenge_end:
                    break
                try:
                    await page.wait_for_load_state("load", timeout=left_ms(1.5))
                except (PWTimeout, PWError):
                    pass
                await asyncio.sleep(1.0)
            # Let the page settle: JS challenges (Reddit, some WAFs) solve
            # themselves and navigate to the real page a second or two later,
            # and app shells fill in after load. Read once navigation has been
            # quiet for a moment and there is real text, or when time is up.
            settle_end = min(end - 0.5, time.monotonic() + SETTLE_MAX)
            while True:
                try:
                    await page.wait_for_load_state("load", timeout=left_ms(2.0))
                except (PWTimeout, PWError):
                    pass
                html = await self._html(page)
                quiet = time.monotonic() - last_nav >= 1.0
                if quiet and visible_text_len(html) >= 500:
                    break
                if time.monotonic() >= settle_end or navigations > self.max_navigations:
                    break
                await asyncio.sleep(0.7)
            if navigations > self.max_navigations:
                raise FetchError("too many page navigations", "error")

        if navigations > self.max_navigations:
            raise FetchError("too many page navigations", "error")
        final_url = strip_challenge_params(page.url)
        if urlsplit(final_url).scheme not in ("http", "https"):
            raise FetchError("page ended on a non-web URL", "blocked-url")
        try:
            title = await page.title()
        except PWError:
            title = ""
        return BrowserPage(
            url=final_url,
            status=status,
            html=html,
            title=title[:300],
            content_type=content_type,
            body=body,
            challenge_cleared=cleared,
        )

