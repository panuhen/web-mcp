"""web_search, in layers (first that gives usable results wins):

1. cache            same normalised query and max_results within 1 h (empty results: 5 min)
2. searxng          the local SearXNG, paced by a token bucket
3. searxng-retry    once more, with only the engines that are healthy right now
4. browser:<engine> a normal results page (Startpage, Brave) in Camoufox, rate-limited
5. brave-api/exa-api optional paid APIs, only with a key (off by default)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .pacing import RateLimited, TokenBucket
from .state import TTLCache

log = logging.getLogger("web_mcp.search")

SNIPPET_CHARS = 240
MAX_RESULTS_CAP = 20


class SearchError(Exception):
    """No provider could answer. The message is safe to show the agent."""


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    published: str = ""


@dataclass
class SearchOutcome:
    provider: str
    results: list[Result]
    note: str = ""


# Engines SearXNG lists under "general" that are not web search (translators,
# converters, knowledge graph). Never used for the healthy-engine retry.
NON_WEB_ENGINES = {"currency", "dictzone", "lingva", "mymemory translated", "wikidata", "wikipedia", "wiktionary"}
NEWS_HINT = re.compile(r"\b(news|latest|today|yesterday|breaking|announced|headlines?|this week)\b", re.I)


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


class EngineHealth:
    """Engines SearXNG reported as unresponsive, remembered for a while."""

    LONG = 900.0   # rate limited, captcha, access denied, suspended
    SHORT = 300.0  # timeouts and other errors

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._until: dict[str, float] = {}
        self._reason: dict[str, str] = {}

    def update(self, responded: list[str], unresponsive: list[tuple[str, str]]) -> None:
        for name in responded:
            self._until.pop(name, None)
            self._reason.pop(name, None)
        for name, reason in unresponsive:
            r = (reason or "").lower()
            long = any(k in r for k in ("too many", "suspend", "captcha", "denied", "403", "429"))
            self._until[name] = self.clock() + (self.LONG if long else self.SHORT)
            self._reason[name] = reason or "error"

    def unhealthy(self) -> dict[str, str]:
        now = self.clock()
        for name in [n for n, t in self._until.items() if t <= now]:
            self._until.pop(name, None)
            self._reason.pop(name, None)
        return dict(self._reason)


def trim(text: str | None, limit: int = SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    i = cut.rfind(" ")
    if i > limit * 0.6:
        cut = cut[:i]
    return cut.rstrip(" ,;:.") + "…"


def _dedupe_key(url: str) -> str:
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}?{p.query}"


def _date(value) -> str:
    if not value:
        return ""
    s = str(value)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s[:40]


def shape(results: list[Result], max_results: int) -> list[Result]:
    """De-duplicate by URL, keep the first occurrence and the original order."""
    seen: set[str] = set()
    out: list[Result] = []
    for r in results:
        if not r.url or not r.url.startswith(("http://", "https://")):
            continue
        key = _dedupe_key(r.url)
        if key in seen:
            continue
        seen.add(key)
        r.title = trim(r.title, 200) or r.url
        r.snippet = trim(r.snippet)
        out.append(r)
        if len(out) >= max_results:
            break
    return out


def parse_searxng(data: dict) -> tuple[list[Result], list[str], list[tuple[str, str]]]:
    """Return results, the engines that answered, and the unresponsive ones (name, reason)."""
    results: list[Result] = []
    responded: set[str] = set()
    for item in data.get("results") or []:
        engines = list(item.get("engines") or ([item["engine"]] if item.get("engine") else []))
        responded.update(engines)
        results.append(
            Result(
                title=item.get("title") or "",
                url=item.get("url") or "",
                snippet=item.get("content") or "",
                engines=engines,
                published=_date(item.get("publishedDate")),
            )
        )
    unresponsive: list[tuple[str, str]] = []
    for e in data.get("unresponsive_engines") or []:
        if isinstance(e, (list, tuple)) and e:
            unresponsive.append((str(e[0]), str(e[1]) if len(e) > 1 else ""))
        elif e:
            unresponsive.append((str(e), ""))
    return results, sorted(responded), unresponsive


def is_thin(results: list[Result], responded: list[str], unresponsive: list, max_results: int) -> bool:
    """SearXNG answered, but badly enough that a fallback provider should be tried."""
    if not results:
        return True
    return len(unresponsive) > len(responded) and len(results) < max_results


def render(query: str, outcome: SearchOutcome) -> str:
    head = f'Search results for "{query}" (provider: {outcome.provider}; {len(outcome.results)} results; untrusted web content)'
    lines = [head]
    if outcome.note:
        lines.append(f"Note: {outcome.note}")
    if not outcome.results:
        lines.append("No results.")
    for i, r in enumerate(outcome.results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        meta = []
        if r.engines:
            meta.append("engines: " + ", ".join(r.engines))
        if r.published:
            meta.append("published: " + r.published)
        if meta:
            lines.append("   " + " · ".join(meta))
    return "\n".join(lines)


class Searcher:
    def __init__(
        self,
        searxng_url: str,
        *,
        brave_api_key: str = "",
        exa_api_key: str = "",
        timeout: float = 12.0,
        client: httpx.AsyncClient | None = None,
        browser=None,
        browser_engines: list[str] | None = None,
        browser_per_minute: int = 4,
        pacer: TokenBucket | None = None,
        max_queue_wait: float = 15.0,
        cache_ttl: float = 3600.0,
        empty_ttl: float = 300.0,
        deadline: float = 35.0,
        clock=time.monotonic,
        extractor=None,
    ):
        self.extractor = extractor  # worker.ExtractorPool for parsing result pages
        self.searxng_url = searxng_url.rstrip("/")
        self.brave_api_key = brave_api_key
        self.exa_api_key = exa_api_key
        self.timeout = timeout
        self._client = client
        self.browser = browser
        self.browser_engines = list(browser_engines if browser_engines is not None else ["startpage", "brave"])
        self.browser_bucket = TokenBucket(max(1, browser_per_minute), 60.0, clock=clock)
        self.pacer = pacer or TokenBucket(3, 10.0)
        self.max_queue_wait = max_queue_wait
        self.cache = TTLCache(cache_ttl, 512, clock=clock)
        self.empty_ttl = empty_ttl
        self.deadline = deadline
        self.health = EngineHealth(clock)
        self._pool: list[str] | None = None
        self._pool_at = 0.0
        self.clock = clock
        self.stats: dict[str, int] = {}

    def fallbacks(self) -> list[str]:
        out = []
        if self.brave_api_key:
            out.append("brave-api")
        if self.exa_api_key:
            out.append("exa-api")
        return out

    def layers(self) -> list[str]:
        out = ["cache", "searxng", "searxng-retry"]
        if self.browser is not None:
            out += [f"browser:{e}" for e in self.browser_engines]
        return out + self.fallbacks()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    # ------------------------------------------------------------------ main

    async def search(self, query: str, max_results: int = 6) -> SearchOutcome:
        t0 = time.monotonic()
        query = (query or "").strip()
        if not query:
            raise SearchError("Empty query.")
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 6
        max_results = max(1, min(MAX_RESULTS_CAP, max_results))

        key = f"{normalize_query(query)}|{max_results}"
        hit = self.cache.get(key)
        if hit is not None:
            self._done("cache", t0, len(hit.results))
            note = f"cached answer from {hit.provider}" + (f"; {hit.note}" if hit.note else "")
            return SearchOutcome("cache", hit.results, note=note)

        try:
            async with asyncio.timeout(self.deadline):
                out = await self._layers(query, max_results, t0)
        except TimeoutError:
            self._done("none", t0, 0, status="failed")
            raise SearchError(f"Search did not finish within {self.deadline:.0f} s.") from None
        self.cache.set(key, out, None if out.results else self.empty_ttl)
        return out

    async def _layers(self, query: str, max_results: int, t0: float) -> SearchOutcome:
        searx_results: list[Result] = []
        problem = ""
        tried: list[str] = []

        # Layers 2 and 3: SearXNG, then once more with only healthy engines.
        try:
            first_engines = await self._first_engines(query)
            await self._pace()
            results, responded, unresponsive = await self._searxng(query, first_engines)
            self.health.update(responded, unresponsive)
            searx_results = results
            if not is_thin(results, responded, unresponsive, max_results):
                return self._finish("searxng", shape(results, max_results), t0)
            problem = "SearXNG returned no results" if not results else "most SearXNG engines did not respond"
            failed = {n for n, _ in unresponsive}
            retry = [e for e in await self._healthy_pool() if e not in failed and e not in responded]
            if failed and retry:
                await self._pace()
                r2, resp2, unresp2 = await self._searxng(query, retry)
                self.health.update(resp2, unresp2)
                merged = results + r2
                searx_results = merged
                if r2 and not is_thin(merged, sorted(set(responded) | set(resp2)), unresp2, max_results):
                    return self._finish("searxng-retry", shape(merged, max_results), t0, note=self._health_note())
                tried.append("searxng-retry: " + ("too few results" if r2 else "no results"))
        except SearchError as e:
            problem = str(e)
        tried.insert(0, f"searxng: {problem}")

        # Layer 4: a normal results page in the stealth browser.
        if self.browser is not None and self.browser_engines:
            if self.browser_bucket.try_take():
                for name in self.browser_engines:
                    try:
                        results = await self._browser_search(name, query)
                    except SearchError as e:
                        tried.append(f"browser:{name}: {e}")
                        continue
                    if results:
                        merged = results + searx_results
                        return self._finish(f"browser:{name}", shape(merged, max_results), t0, note=f"SearXNG was weak ({problem})")
                    tried.append(f"browser:{name}: no results")
            else:
                tried.append("browser: skipped (browser search rate limit)")

        # Layer 5: paid APIs, only with a key.
        for name in self.fallbacks():
            try:
                results = await (self._brave(query, max_results) if name == "brave-api" else self._exa(query, max_results))
            except SearchError as e:
                tried.append(f"{name}: {e}")
                continue
            if results:
                return self._finish(name, shape(results, max_results), t0, note=f"fallback used because {problem}")
            tried.append(f"{name}: no results")

        if searx_results:
            return self._finish("searxng", shape(searx_results, max_results), t0, note=problem)
        if problem == "SearXNG returned no results":
            return self._finish("searxng", [], t0, note="; ".join(tried[1:]))
        self._done("none", t0, 0, status="failed")
        raise SearchError("Search failed. " + "; ".join(tried) + ".")

    def _finish(self, provider: str, results: list[Result], t0: float, note: str = "") -> SearchOutcome:
        self._done(provider, t0, len(results))
        return SearchOutcome(provider, results, note=note)

    def _health_note(self) -> str:
        bad = self.health.unhealthy()
        return ("engines resting: " + ", ".join(f"{n} ({r})" for n, r in sorted(bad.items()))) if bad else ""

    def _done(self, provider: str, t0: float, n: int, status: str = "ok") -> None:
        self._count(provider)
        log.info("web_search status=%s provider=%s results=%d ms=%d", status, provider, n, int((time.monotonic() - t0) * 1000))

    async def _pace(self) -> None:
        try:
            await self.pacer.acquire(self.max_queue_wait)
        except RateLimited as e:
            raise SearchError(f"local search pacing: too many searches right now, retry in about {e.wait:.0f} s") from None

    # ------------------------------------------------------------------ engines

    async def _healthy_pool(self) -> list[str]:
        """Web engines enabled in SearXNG, minus the ones resting after errors."""
        if self._pool is None or self.clock() - self._pool_at > 600:
            try:
                r = await self._http().get(f"{self.searxng_url}/config", timeout=self.timeout)
                data = r.json()
                self._pool = [
                    e["name"]
                    for e in data.get("engines", [])
                    if e.get("enabled") and "general" in (e.get("categories") or []) and e["name"] not in NON_WEB_ENGINES
                ]
                self._pool_at = self.clock()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return []
        bad = self.health.unhealthy()
        return [e for e in self._pool if e not in bad]

    async def _first_engines(self, query: str) -> list[str] | None:
        """None means SearXNG's own default set. An explicit list skips resting
        engines and adds news engines for news-like queries."""
        news = bool(NEWS_HINT.search(query))
        if not self.health.unhealthy() and not news:
            return None
        pool = await self._healthy_pool()
        if not pool:
            return None
        return pool + (["bing news"] if news else [])

    async def _searxng(self, query: str, engines: list[str] | None = None):
        params = {"q": query, "format": "json"}
        if engines:
            params["engines"] = ",".join(engines)
        else:
            params["categories"] = "general"
        try:
            r = await self._http().get(
                f"{self.searxng_url}/search", params=params, headers={"Accept": "application/json"}, timeout=self.timeout
            )
        except httpx.TimeoutException:
            raise SearchError(f"SearXNG at {self.searxng_url} timed out") from None
        except httpx.HTTPError:
            raise SearchError(f"SearXNG is not reachable at {self.searxng_url} (is the container running?)") from None
        if r.status_code == 403:
            raise SearchError("SearXNG refused the JSON API (enable 'json' under search.formats in settings.yml)")
        if r.status_code == 429:
            raise SearchError("SearXNG rate-limited the request (turn server.limiter off for a local instance)")
        if r.status_code >= 400:
            raise SearchError(f"SearXNG answered HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise SearchError("SearXNG did not return JSON") from None
        return parse_searxng(data)

    async def _browser_search(self, name: str, query: str) -> list[Result]:
        from .detect import detect_challenge
        from .fetchers import FetchError
        from .serp import ENGINES

        engine = ENGINES.get(name)
        if engine is None:
            raise SearchError("unknown engine")
        await self._pace()
        try:
            page = await self.browser.fetch(engine.url(query), 20.0, 8.0)
        except FetchError as e:
            raise SearchError(str(e)) from None
        reason = detect_challenge(page.html, None)
        if reason:
            raise SearchError(f"blocked ({reason})")
        if self.extractor is not None:
            from .worker import TooComplex

            try:
                results = await self.extractor.run(engine.parse, page.html, timeout=10.0)
            except TooComplex as e:
                raise SearchError(f"result page too complex ({e})") from None
        else:
            results = await asyncio.to_thread(engine.parse, page.html)
        self._count(f"browser:{name}:pages")
        return results

    async def _brave(self, query: str, max_results: int) -> list[Result]:
        try:
            r = await self._http().get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": min(20, max_results)},
                headers={"Accept": "application/json", "X-Subscription-Token": self.brave_api_key},
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            raise SearchError("not reachable") from None
        if r.status_code in (401, 403):
            raise SearchError("API key rejected")
        if r.status_code == 429:
            raise SearchError("rate limited")
        if r.status_code >= 400:
            raise SearchError(f"HTTP {r.status_code}")
        try:
            items = ((r.json().get("web") or {}).get("results")) or []
        except ValueError:
            raise SearchError("bad response") from None
        return [
            Result(
                title=i.get("title") or "",
                url=i.get("url") or "",
                snippet=re.sub(r"</?strong>", "", i.get("description") or ""),
                engines=["brave api"],
                published=_date(i.get("page_age") or i.get("age")),
            )
            for i in items
        ]

    async def _exa(self, query: str, max_results: int) -> list[Result]:
        try:
            r = await self._http().post(
                "https://api.exa.ai/search",
                json={
                    "query": query,
                    "numResults": min(20, max_results),
                    "contents": {"text": {"maxCharacters": 400}},
                },
                headers={"x-api-key": self.exa_api_key, "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            raise SearchError("not reachable") from None
        if r.status_code in (401, 403):
            raise SearchError("API key rejected")
        if r.status_code == 429:
            raise SearchError("rate limited")
        if r.status_code >= 400:
            raise SearchError(f"HTTP {r.status_code}")
        try:
            items = r.json().get("results") or []
        except ValueError:
            raise SearchError("bad response") from None
        out = []
        for i in items:
            snippet = ""
            if i.get("highlights"):
                snippet = " ".join(i["highlights"])
            elif i.get("summary"):
                snippet = i["summary"]
            elif i.get("text"):
                snippet = i["text"]
            out.append(
                Result(
                    title=i.get("title") or "",
                    url=i.get("url") or "",
                    snippet=snippet,
                    engines=["exa"],
                    published=_date(i.get("publishedDate")),
                )
            )
        return out
