"""Hostile input: every extraction path must finish within its limits.

Made-up pathological fixtures are generated here, not stored.
"""

import time

import pytest

from tests.pdfgen import many_pages_pdf
from web_mcp.detect import detect_challenge, needs_javascript, visible_text_len
from web_mcp.extract import MAX_PDF_PAGES, _clean, decode, extract_body, extract_html, html_title
from web_mcp.fetchers import RawResponse
from web_mcp.reader import PageReader, ReadError
from web_mcp.worker import ExtractorPool, TooComplex

LIMIT = 8.0  # seconds; the per-extraction limit used below


@pytest.fixture(scope="module")
def pool():
    p = ExtractorPool(workers=2, timeout=LIMIT)
    yield p
    p.close()


def _alloc(n: int) -> int:
    return len(bytearray(n))


def _sleep(s: float) -> None:
    time.sleep(s)


# --- linear-time helpers in the main process ---------------------------------

REGEX_BAITS = {
    "unclosed scripts": "<script" * 200_000,
    "only angle brackets": "<" * 2_000_000,
    "huge whitespace": "<html>" + " " * 3_000_000,
    "unclosed titles": "<title" * 100_000,
    "div with huge attribute": ("<div " + "a" * 100_000) * 20,
    "nested quotes": '<div id="' * 200_000,
}


@pytest.mark.parametrize("name", list(REGEX_BAITS))
def test_detection_is_linear_on_regex_bait(name):
    html = REGEX_BAITS[name]
    t0 = time.monotonic()
    detect_challenge(html, 200)
    needs_javascript(html, 0)
    visible_text_len(html)
    html_title(html)
    assert time.monotonic() - t0 < 2.0, name


def test_clean_on_one_huge_line_of_spaces():
    t0 = time.monotonic()
    out = _clean("x" + " " * 5_000_000 + "y\n")
    assert time.monotonic() - t0 < 1.0
    assert out.startswith("x")


@pytest.mark.parametrize("charset", ["base64", "zlib", "rot13", "utf-7", "x-made-up", "utf-32", "a" * 300])
def test_odd_charsets_decode_without_error(charset):
    text = decode(b"<p>hello \xff\xfe world</p>", f"text/html; charset={charset}")
    assert isinstance(text, str) and "hello" in text or charset in ("utf-32",)


def test_deep_json_and_huge_numbers_fall_back_to_raw_text():
    deep = ("[" * 100_000 + "]" * 100_000).encode()
    ex = extract_body(deep, "application/json")
    assert ex.kind == "json" and ex.text.startswith("[[[")
    big = ("1" * 100_000).encode()
    assert extract_body(big, "application/json").text.startswith("111")


# --- the worker pool -------------------------------------------------------------

HTML_BAITS = {
    "deep nesting": "<html><body>" + "<div>" * 200_000 + "deep text" + "</div>" * 200_000 + "</body></html>",
    "millions of tiny elements": "<html><body>" + "<b>x</b>" * 600_000 + "</body></html>",
    "giant single line": "<html><body><p>" + "word " * 1_000_000 + "</p></body></html>",
    "huge attribute": '<html><body><div data-x="' + "a" * 2_500_000 + '">hello</div></body></html>',
    "unclosed tags": "<html><body>" + "<p><span><i>" * 150_000,
    "table soup": "<table>" + "<tr><td>c</td>" * 300_000 + "</table>",
}


@pytest.mark.parametrize("name", list(HTML_BAITS))
async def test_html_bait_finishes_within_limit(pool, name):
    t0 = time.monotonic()
    try:
        ex = await pool.run(extract_html, HTML_BAITS[name], "https://bait.example/")
        assert isinstance(ex.text, str)
    except TooComplex:
        pass  # a clean refusal is also fine
    assert time.monotonic() - t0 < LIMIT + 3, name


async def test_pdf_with_very_many_pages_is_capped(pool):
    pdf = many_pages_pdf(20_000, "Many pages")
    t0 = time.monotonic()
    try:
        ex = await pool.run(extract_body, pdf, "application/pdf", None)
        assert ex.text.count("Many pages") <= MAX_PDF_PAGES
    except TooComplex:
        pass
    assert time.monotonic() - t0 < LIMIT + 3


async def test_time_limit_kills_the_job_and_pool_recovers(pool):
    t0 = time.monotonic()
    with pytest.raises(TooComplex):
        await pool.run(_sleep, 60, timeout=1.0)
    assert time.monotonic() - t0 < 3
    before = pool.restarts
    ex = await pool.run(extract_body, b"<html><body><p>" + b"fine text " * 50 + b"</p></body></html>", "text/html", None)
    assert "fine text" in ex.text
    assert pool.restarts == before


async def test_memory_limit(pool):
    with pytest.raises(TooComplex):
        await pool.run(_alloc, 3 * 1024 * 1024 * 1024)  # 3 GiB > the 1 GiB worker limit
    ex = await pool.run(extract_body, b"ok text", "text/plain", None)
    assert ex.text == "ok text"


async def test_reader_turns_a_limit_into_a_short_answer():
    class SlowPool:
        async def run(self, fn, *args, timeout=None):
            raise TooComplex("took longer than 1 s")

    class Http:
        async def fetch(self, url, timeout):
            return RawResponse(url, 200, {"content-type": "text/html"}, b"<html>...</html>")

    async def allow(url):
        from web_mcp.egress import CheckedURL

        return CheckedURL(url, "https", "bait.example", 443, ["93.184.215.14"])

    browser_calls = []

    class Browser:
        async def fetch(self, *a):
            browser_calls.append(a)

    r = PageReader(Http(), Browser(), url_checker=allow, extractor=SlowPool())
    with pytest.raises(ReadError) as e:
        await r.read_page("https://bait.example/")
    assert str(e.value).startswith("Page too complex to extract")
    assert browser_calls == []  # no point escalating
