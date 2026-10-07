"""Parse search engine result pages fetched by the browser search fallback.

Only engines that answered from a normal browser in testing are listed. The
parsers look for stable structure (result containers, title elements) rather
than generated CSS class names, and drop links back to the engine itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from .search import Result


def _doc(html: str):
    import lxml.html

    doc = lxml.html.fromstring(html or "<html></html>")
    for bad in doc.xpath("//style|//script|//noscript|//svg"):
        bad.drop_tree()
    return doc


def _text(el) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def _first(nodes):
    return nodes[0] if nodes else None


def _external(href: str, own_host: str) -> bool:
    if not href or not href.startswith(("http://", "https://")):
        return False
    host = (urlsplit(href).hostname or "").lower()
    return not (host == own_host or host.endswith("." + own_host))


def parse_startpage(html: str) -> list[Result]:
    doc = _doc(html)
    out: list[Result] = []
    for r in doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " result ")]'):
        a = _first(r.xpath('.//a[contains(concat(" ", normalize-space(@class), " "), " result-title ")]'))
        if a is None:
            a = _first(r.xpath(".//a[@href]"))
        href = a.get("href") if a is not None else ""
        if not _external(href, "startpage.com"):
            continue
        title = _text(_first(r.xpath(".//h2"))) or _text(a)
        snippet = _text(_first(r.xpath('.//p[contains(concat(" ", normalize-space(@class), " "), " description ")]')))
        out.append(Result(title=title, url=href, snippet=snippet, engines=["startpage (browser)"]))
    return out


def parse_brave(html: str) -> list[Result]:
    doc = _doc(html)
    out: list[Result] = []
    for r in doc.xpath('//div[@data-type="web"]'):
        a = None
        for cand in r.xpath(".//a[@href]"):
            if _external(cand.get("href"), "brave.com"):
                a = cand
                break
        if a is None:
            continue
        title = _text(_first(r.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " title ")]'))) or _text(a)
        snippet = _text(
            _first(
                r.xpath(
                    './/*[contains(@class, "snippet-description") or contains(@class, "line-clamp-dynamic")'
                    ' or contains(concat(" ", normalize-space(@class), " "), " content ")]'
                )
            )
        )
        out.append(Result(title=title, url=a.get("href"), snippet=snippet, engines=["brave (browser)"]))
    return out


@dataclass(frozen=True)
class SerpEngine:
    name: str
    url: Callable[[str], str]
    parse: Callable[[str], list[Result]]


ENGINES: dict[str, SerpEngine] = {
    "startpage": SerpEngine(
        "startpage", lambda q: "https://www.startpage.com/sp/search?query=" + quote_plus(q), parse_startpage
    ),
    "brave": SerpEngine(
        "brave", lambda q: "https://search.brave.com/search?source=web&q=" + quote_plus(q), parse_brave
    ),
}
