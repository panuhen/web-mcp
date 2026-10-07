"""Turn fetched bytes into clean readable text."""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass

for _name in ("trafilatura", "htmldate", "courlan", "pypdf"):
    logging.getLogger(_name).setLevel(logging.ERROR)

# Hard cap on what we keep per page (the cache stores this much).
MAX_KEEP_CHARS = 200_000
MAX_PDF_PAGES = 300

HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("application/xml", "application/rss+xml", "application/atom+xml", "application/javascript")
JSON_TYPES = ("application/json", "application/ld+json", "text/json")
PDF_TYPES = ("application/pdf", "application/x-pdf")


class UnsupportedContent(Exception):
    pass


@dataclass
class Extracted:
    text: str
    title: str
    kind: str  # html | pdf | text | json


def media_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def is_supported(content_type: str | None) -> bool:
    mt = media_type(content_type)
    if not mt:
        return True  # sniff later
    if mt.endswith("+json") or mt.endswith("+xml"):
        return True
    return mt.startswith(HTML_TYPES + JSON_TYPES + PDF_TYPES) or mt.startswith("text/") or mt in TEXT_TYPES


def _charset(content_type: str | None) -> str | None:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    return m.group(1) if m else None


def decode(body: bytes, content_type: str | None) -> str:
    cs = _charset(content_type)
    if cs:
        try:
            return body.decode(cs, errors="replace")
        except LookupError:
            pass
    head = body[:4096].decode("ascii", errors="ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w\-]+)", head, re.I)
    if m:
        try:
            return body.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1", errors="replace")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_KEEP_CHARS]


def html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html[:200_000], re.I)
    if not m:
        return ""
    import html as _h

    return re.sub(r"\s+", " ", _h.unescape(m.group(1))).strip()[:300]


def extract_html(html: str, url: str | None = None) -> Extracted:
    import trafilatura
    from trafilatura.metadata import extract_metadata

    html = html[:5_000_000]
    text = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=True,
        include_tables=True,
        include_links=False,
        include_images=False,
        favor_recall=True,
        deduplicate=True,
    ) or ""
    if len(text) < 1500:
        # Index pages (news fronts, docs landing pages): trafilatura keeps one
        # teaser. A list of headings and paragraphs is more useful there.
        alt = listing_text(html)
        if len(alt) > 3 * len(text):
            text = alt
    if len(text) < 200:
        # Very short pages and app shells: fall back to all visible text.
        try:
            alt = trafilatura.html2txt(html) or ""
        except Exception:
            alt = ""
        if len(alt) > len(text):
            text = alt
    title = ""
    try:
        meta = extract_metadata(html, default_url=url)
        title = (meta.title or "") if meta else ""
    except Exception:
        title = ""
    title = title or html_title(html)
    return Extracted(text=_clean(text), title=title, kind="html")


def listing_text(html: str, limit: int = 30_000) -> str:
    """Headings and paragraphs of the main area, in order, without nav/header/footer."""
    try:
        import lxml.html

        doc = lxml.html.fromstring(html)
    except Exception:
        return ""
    for bad in doc.xpath("//script|//style|//noscript|//nav|//header|//footer|//form|//svg|//*[@aria-hidden='true']"):
        bad.drop_tree()
    roots = doc.xpath("//main") or doc.xpath("//body") or [doc]
    lines: list[str] = []
    seen: set[str] = set()
    size = 0
    for el in roots[0].iter("h1", "h2", "h3", "h4", "p", "li"):
        t = " ".join(el.text_content().split())
        if len(t) < 25 or t in seen:
            continue
        if el.tag == "li" and el.xpath(".//p|.//h2|.//h3"):
            continue  # its children are listed on their own
        seen.add(t)
        line = ("## " + t) if el.tag in ("h1", "h2", "h3", "h4") else t
        lines.append(line)
        size += len(line)
        if size > limit:
            break
    return "\n\n".join(lines)


def extract_pdf(body: bytes, char_budget: int = MAX_KEEP_CHARS) -> Extracted:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise UnsupportedContent("encrypted PDF") from None
        parts: list[str] = []
        total = 0
        for i, page in enumerate(reader.pages):
            if total >= char_budget or i >= MAX_PDF_PAGES:
                break
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
        title = ""
        try:
            title = (reader.metadata.title or "") if reader.metadata else ""
        except Exception:
            pass
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        return Extracted(text=_clean(text), title=str(title).strip(), kind="pdf")
    except UnsupportedContent:
        raise
    except Exception as e:  # malformed PDF
        raise UnsupportedContent(f"could not read PDF ({type(e).__name__})") from None


def is_pdf(body: bytes, content_type: str | None) -> bool:
    return media_type(content_type) in PDF_TYPES or body[:5] == b"%PDF-"


def extract_body(body: bytes, content_type: str | None, url: str | None = None) -> Extracted:
    if is_pdf(body, content_type):
        return extract_pdf(body)
    mt = media_type(content_type)
    text = decode(body, content_type)
    if mt in JSON_TYPES or mt.endswith("+json"):
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
        return Extracted(text=_clean(text), title="", kind="json")
    if mt.startswith(HTML_TYPES) or (not mt and re.search(r"<html|<!doctype html", text[:2000], re.I)):
        return extract_html(text, url)
    if mt.startswith("text/") or mt in TEXT_TYPES or mt.endswith("+xml") or not mt:
        return Extracted(text=_clean(text), title="", kind="text")
    raise UnsupportedContent(f"unsupported content type {mt}")
