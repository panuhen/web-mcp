"""web_search: SearXNG first, Brave Search API and Exa as optional fallbacks."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

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


def parse_searxng(data: dict) -> tuple[list[Result], list[str], list[str]]:
    """Return results, the engines that answered, and the unresponsive ones."""
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
    unresponsive = [str(e[0]) if isinstance(e, (list, tuple)) and e else str(e) for e in data.get("unresponsive_engines") or []]
    return results, sorted(responded), unresponsive


def is_thin(results: list[Result], responded: list[str], unresponsive: list[str], max_results: int) -> bool:
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
    ):
        self.searxng_url = searxng_url.rstrip("/")
        self.brave_api_key = brave_api_key
        self.exa_api_key = exa_api_key
        self.timeout = timeout
        self._client = client
        self.stats: dict[str, int] = {}

    def fallbacks(self) -> list[str]:
        out = []
        if self.brave_api_key:
            out.append("brave")
        if self.exa_api_key:
            out.append("exa")
        return out

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

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

        searx_results: list[Result] = []
        searx_problem = ""
        try:
            searx_results, responded, unresponsive = await self._searxng(query)
            thin = is_thin(searx_results, responded, unresponsive, max_results)
            if not thin:
                out = SearchOutcome("searxng", shape(searx_results, max_results))
                self._done("searxng", t0, len(out.results))
                return out
            searx_problem = (
                "SearXNG returned no results" if not searx_results else "most SearXNG engines did not respond"
            )
        except SearchError as e:
            searx_problem = str(e)

        tried = [f"searxng: {searx_problem}"]
        for name in self.fallbacks():
            try:
                results = await (self._brave(query, max_results) if name == "brave" else self._exa(query, max_results))
            except SearchError as e:
                tried.append(f"{name}: {e}")
                continue
            if results:
                out = SearchOutcome(name, shape(results, max_results), note=f"fallback used because {searx_problem}")
                self._done(name, t0, len(out.results))
                return out
            tried.append(f"{name}: no results")

        if searx_results:
            out = SearchOutcome("searxng", shape(searx_results, max_results), note=searx_problem)
            self._done("searxng", t0, len(out.results))
            return out
        if searx_problem == "SearXNG returned no results":
            self._done("searxng", t0, 0)
            note = "" if not self.fallbacks() else "; ".join(tried[1:])
            return SearchOutcome("searxng", [], note=note)
        self._done("none", t0, 0, status="failed")
        hint = "" if self.fallbacks() else " No fallback provider is configured (BRAVE_API_KEY / EXA_API_KEY)."
        raise SearchError("Search failed. " + "; ".join(tried) + "." + hint)

    def _done(self, provider: str, t0: float, n: int, status: str = "ok") -> None:
        self._count(provider)
        log.info("web_search status=%s provider=%s results=%d ms=%d", status, provider, n, int((time.monotonic() - t0) * 1000))

    async def _searxng(self, query: str) -> tuple[list[Result], list[str], list[str]]:
        try:
            r = await self._http().get(
                f"{self.searxng_url}/search",
                params={"q": query, "format": "json", "categories": "general"},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            raise SearchError(f"SearXNG at {self.searxng_url} timed out") from None
        except httpx.HTTPError:
            raise SearchError(f"SearXNG is not reachable at {self.searxng_url} (is the container running?)") from None
        if r.status_code == 403:
            raise SearchError("SearXNG refused the JSON API (enable 'json' under search.formats in settings.yml)")
        if r.status_code >= 400:
            raise SearchError(f"SearXNG answered HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise SearchError("SearXNG did not return JSON") from None
        return parse_searxng(data)

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
