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
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

from .egress import BlockedURL, check_url
from .extract import is_supported, media_type

log = logging.getLogger("web_mcp.fetch")

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


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
        from curl_cffi import CurlError
        from curl_cffi.requests import AsyncSession

        current = url
        deadline = time.monotonic() + timeout
        # A fresh session per call: no cookies carried between unrelated reads.
        async with AsyncSession(impersonate=self.impersonate, proxy=self.proxy_url, headers=DEFAULT_HEADERS) as s:
            for _hop in range(self.max_redirects + 1):
                try:
                    await check_url(current)
                except BlockedURL as e:
                    raise FetchError(f"refused: {e}", "blocked-url") from None
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    raise FetchError("timed out", "timeout")
                try:
                    async with s.stream("GET", current, allow_redirects=False, timeout=remaining) as r:
                        status = r.status_code
                        headers = {k.lower(): v for k, v in r.headers.items()}
                        if status in REDIRECT_STATUSES and headers.get("location"):
                            current = urljoin(current, headers["location"])
                            continue
                        ct = headers.get("content-type", "")
                        if status < 400 and not is_supported(ct):
                            raise FetchError(f"unsupported content type {media_type(ct)}", "unsupported")
                        try:
                            declared = int(headers.get("content-length", "0") or 0)
                        except ValueError:
                            declared = 0
                        if declared > self.max_bytes:
                            raise FetchError("response too large", "too-large")
                        chunks: list[bytes] = []
                        size = 0
                        truncated = False
                        async for chunk in r.aiter_content():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > self.max_bytes:
                                truncated = True
                                break
                        body = b"".join(chunks)
                        if truncated:
                            if "pdf" in media_type(ct):
                                raise FetchError("response too large", "too-large")
                            body = body[: self.max_bytes]
                        return RawResponse(url=current, status=status, headers=headers, body=body)
                except FetchError:
                    raise
                except CurlError as e:
                    msg = str(e).lower()
                    if "proxy" in msg or "socks" in msg:
                        # The egress guard refused the target (or it was unreachable).
                        raise FetchError("connection refused or not reachable", "network") from None
                    if "timed out" in msg or "timeout" in msg:
                        raise FetchError("timed out", "timeout") from None
                    raise FetchError(f"network error ({type(e).__name__})", "network") from None
            raise FetchError("too many redirects", "error")


# ---------------------------------------------------------------------------
# Step 2: stealth browser


@runtime_checkable
class StealthFetcher(Protocol):
    """A browser that renders a page, waits out challenges and returns the HTML."""

    name: str

    async def fetch(self, url: str, timeout: float, challenge_wait: float) -> BrowserPage: ...

    async def close(self) -> None: ...

    def state(self) -> dict: ...


class CamoufoxFetcher:
    """Camoufox (anti-detect Firefox) via Playwright.

    Started on first use, kept warm, closed after `idle_close` seconds without
    use. At most `max_pages` pages at once. Never shows a window: either real
    headless mode or Camoufox's virtual display (Xvfb) inside the container.
    """

    name = "browser"

    def __init__(self, proxy_server: str | None, headless: str = "true", idle_close: float = 300.0, max_pages: int = 2):
        self.proxy_server = proxy_server
        self.headless: bool | str = "virtual" if headless == "virtual" else True
        self.idle_close = idle_close
        self._sem = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self._active = 0
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

    async def _ensure(self):
        async with self._lock:
            if self._context is not None:
                return self._context
            from camoufox.async_api import AsyncNewBrowser, AsyncNewContext
            from playwright.async_api import async_playwright

            prefs = {
                # Send localhost through the proxy too, so the egress guard sees it.
                "network.proxy.allow_hijacking_localhost": True,
                "network.proxy.no_proxies_on": "",
                "network.proxy.socks_remote_dns": True,
                "network.http.http3.enable": False,
                "network.dns.disablePrefetch": True,
                "network.prefetch-next": False,
                "network.predictor.enabled": False,
                "browser.download.enabled": False,
                "pdfjs.disabled": False,
            }
            self._pw = await async_playwright().start()
            try:
                kwargs = dict(
                    headless=self.headless,
                    block_webrtc=True,
                    firefox_user_prefs=prefs,
                    i_know_what_im_doing=True,
                )
                if self.proxy_server:
                    kwargs["proxy"] = {"server": self.proxy_server}
                self._browser = await AsyncNewBrowser(self._pw, **kwargs)
                self._context = await AsyncNewContext(
                    self._browser,
                    accept_downloads=False,
                    service_workers="block",
                )
            except BaseException:
                await self._shutdown()
                raise
            self.launches += 1
            self._last_used = time.monotonic()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_watch())
            log.info("browser started")
            return self._context

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
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await asyncio.wait_for(getattr(obj, meth)(), 10)
                except Exception:
                    pass
        self._context = self._browser = self._pw = None

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            await self._shutdown()

    async def fetch(self, url: str, timeout: float, challenge_wait: float) -> BrowserPage:
        from .detect import detect_challenge, visible_text_len

        start = time.monotonic()
        end = start + timeout
        async with self._sem:
            self._active += 1
            try:
                try:
                    context = await asyncio.wait_for(self._ensure(), max(1.0, end - time.monotonic()))
                except asyncio.TimeoutError:
                    raise FetchError("browser start timed out", "timeout") from None
                page = await context.new_page()
                try:
                    return await self._load(page, url, end, challenge_wait, detect_challenge, visible_text_len)
                finally:
                    try:
                        await asyncio.wait_for(page.close(), 5)
                    except Exception:
                        pass
            except Exception as e:
                if isinstance(e, FetchError):
                    raise
                # A dead browser gets restarted on the next call.
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    async with self._lock:
                        await self._shutdown()
                raise FetchError(f"browser error ({type(e).__name__})", "network") from None
            finally:
                self._active -= 1
                self._last_used = time.monotonic()

    async def _load(self, page, url, end, challenge_wait, detect_challenge, visible_text_len) -> BrowserPage:
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout

        def left_ms(cap: float | None = None) -> float:
            left = max(0.2, end - time.monotonic())
            return 1000 * (min(left, cap) if cap else left)

        status: int | None = None
        content_type = "text/html"
        body: bytes | None = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=left_ms())
            if resp is not None:
                status = resp.status
                content_type = (await resp.all_headers()).get("content-type", "text/html")
                mt = media_type(content_type)
                if mt and not mt.startswith(("text/html", "application/xhtml")):
                    try:
                        body = await resp.body()
                    except PWError:
                        body = None
        except PWTimeout:
            raise FetchError("page load timed out", "timeout") from None
        except PWError as e:
            msg = str(e)
            if "Download is starting" in msg:
                raise FetchError("the URL is a file download", "unsupported") from None
            if "NS_ERROR_PROXY" in msg or "NS_ERROR_UNKNOWN_HOST" in msg or "NS_ERROR_CONNECTION_REFUSED" in msg:
                raise FetchError("connection refused or not reachable", "network") from None
            raise FetchError(f"navigation failed ({msg.splitlines()[0][:80]})", "network") from None

        cleared = False
        if body is None:
            # Wait for a challenge to clear (the page usually reloads itself).
            challenge_end = min(end - 0.5, time.monotonic() + challenge_wait)
            first = True
            while True:
                try:
                    html = await page.content()
                except PWError:
                    html = ""
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
            # Let the page settle a little for JS-rendered content.
            try:
                await page.wait_for_load_state("load", timeout=left_ms(4.0))
            except (PWTimeout, PWError):
                pass
            try:
                html = await page.content()
            except PWError:
                html = ""
            if visible_text_len(html) < 500 and end - time.monotonic() > 2:
                try:
                    await page.wait_for_load_state("networkidle", timeout=left_ms(3.0))
                except (PWTimeout, PWError):
                    pass
                try:
                    html = await page.content()
                except PWError:
                    pass
        else:
            html = ""

        final_url = page.url
        if urlsplit(final_url).scheme not in ("http", "https"):
            raise FetchError("page ended on a non-web URL", "blocked-url")
        try:
            title = await page.title()
        except PWError:
            title = ""
        return BrowserPage(
            url=final_url,
            status=status,
            html=html[:5_000_000],
            title=title,
            content_type=content_type,
            body=body,
            challenge_cleared=cleared,
        )
