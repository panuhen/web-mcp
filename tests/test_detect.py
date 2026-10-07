from pathlib import Path

import pytest

from web_mcp.detect import detect_challenge, needs_javascript
from web_mcp.extract import extract_html

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text()


@pytest.mark.parametrize(
    "name,status,expected",
    [
        ("cloudflare_jsc.html", 403, "cloudflare"),
        ("cloudflare_jsc.html", 200, "cloudflare"),
        ("cloudflare_block.html", 403, "cloudflare"),
        ("datadome.html", 403, "datadome"),
        ("perimeterx.html", 403, "perimeterx"),
        ("perimeterx.html", 200, "perimeterx"),
        ("generic_captcha.html", 200, "captcha"),
        ("enable_js.html", 200, "needs-javascript"),
        ("reddit_block.html", 403, "reddit-block"),
    ],
)
def test_challenge_pages_are_detected(name, status, expected):
    assert detect_challenge(load(name), status) == expected


def test_normal_page_with_captcha_widget_and_cf_script_is_not_a_wall():
    assert detect_challenge(load("normal_article.html"), 200) is None


def test_cf_mitigated_header():
    assert detect_challenge("<html></html>", 403, {"CF-Mitigated": "challenge"}) == "cloudflare"


def test_datadome_header_only_counts_on_error_status():
    assert detect_challenge("<html><body>hello</body></html>", 403, {"x-datadome": "protected"}) == "datadome"
    assert detect_challenge("<html><body>hello</body></html>", 200, {"x-datadome": "protected"}) is None


def test_plain_small_page_is_not_a_wall():
    assert detect_challenge("<html><head><title>Hi</title></head><body><p>Hello world.</p></body></html>", 200) is None


def test_needs_javascript():
    shell = load("enable_js.html")
    assert needs_javascript(shell, len(extract_html(shell).text))
    article = load("normal_article.html")
    assert not needs_javascript(article, len(extract_html(article).text))


def test_challenge_params_are_stripped_from_final_url():
    from web_mcp.fetchers import strip_challenge_params

    u = "https://www.reddit.com/r/x/comments/1/t/?solution=abc&js_challenge=1&jsc_token=def&jsc_orig_r="
    assert strip_challenge_params(u) == "https://www.reddit.com/r/x/comments/1/t/"
    assert strip_challenge_params("https://a.example/p?q=1&__cf_chl_tk=z") == "https://a.example/p?q=1"
    assert strip_challenge_params("https://a.example/p?q=1") == "https://a.example/p?q=1"
