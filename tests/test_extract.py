import pytest

from tests.pdfgen import tiny_pdf
from web_mcp.extract import UnsupportedContent, extract_body, is_supported


def test_html_extracts_title_and_text():
    html = b"<html><head><title>My Page</title></head><body><nav>menu</nav><article><h1>Heading</h1><p>" + b"Real content sentence. " * 30 + b"</p></article></body></html>"
    ex = extract_body(html, "text/html; charset=utf-8", "https://example.com/")
    assert ex.kind == "html" and ex.title in {"My Page", "Heading"}
    assert "Real content sentence." in ex.text


def test_json_is_pretty_printed():
    ex = extract_body(b'{"a":1,"b":[1,2]}', "application/json")
    assert ex.kind == "json" and '"a": 1' in ex.text


def test_plain_text():
    ex = extract_body(b"hello\r\nworld\n\n\n\nend", "text/plain")
    assert ex.kind == "text" and ex.text == "hello\nworld\n\nend"


def test_pdf_text():
    ex = extract_body(tiny_pdf("Quarterly report text"), "application/pdf")
    assert ex.kind == "pdf" and "Quarterly report text" in ex.text


def test_pdf_sniffed_without_content_type():
    ex = extract_body(tiny_pdf("Sniffed"), "")
    assert ex.kind == "pdf"


def test_broken_pdf():
    with pytest.raises(UnsupportedContent):
        extract_body(b"%PDF-1.4 garbage", "application/pdf")


@pytest.mark.parametrize(
    "ct,ok",
    [
        ("text/html", True),
        ("application/pdf", True),
        ("application/json", True),
        ("application/vnd.api+json", True),
        ("text/plain; charset=utf-8", True),
        ("image/png", False),
        ("application/octet-stream", False),
        ("application/zip", False),
        ("video/mp4", False),
    ],
)
def test_content_type_filter(ct, ok):
    assert is_supported(ct) is ok


def test_index_page_gets_headline_list():
    items = "".join(
        f"<article><h3>Headline number {i} about something that happened</h3><p>Summary sentence {i} with enough words to count.</p></article>"
        for i in range(12)
    )
    html = (
        "<html><head><title>Front page</title></head><body><nav><a>Home</a><a>News</a><a>Sport</a></nav>"
        f"<header><p>Site header text that is long enough to count</p></header><main>{items}</main>"
        "<footer><p>Copyright notice that is long enough to count here</p></footer></body></html>"
    ).encode()
    ex = extract_body(html, "text/html")
    assert ex.text.count("## Headline number") == 12
    assert "Site header" not in ex.text and "Copyright" not in ex.text
