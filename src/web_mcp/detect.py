"""Recognise bot walls, challenge pages and pages that only render with JavaScript."""

from __future__ import annotations

import re
from collections.abc import Mapping

BLOCK_STATUSES = {401, 403, 429, 503}

# Titles only challenge pages have. Always a wall.
_TITLES = [
    ("cloudflare", re.compile(r"<title>\s*just a moment\.\.\.?\s*</title>", re.I)),
    ("cloudflare", re.compile(r"<title>\s*attention required!?\s*\|\s*cloudflare", re.I)),
    ("vercel", re.compile(r"<title>\s*vercel security checkpoint", re.I)),
    ("amazon-captcha", re.compile(r"<title>\s*robot check\s*</title>", re.I)),
    ("ddos-guard", re.compile(r"<title>\s*ddos-guard\s*</title>", re.I)),
]

# Markers of challenge or block pages. They count unless the page is long
# and came with a good status (see detect_challenge).
_STRONG = [
    ("cloudflare", re.compile(r"cf[-_]chl[-_]|/cdn-cgi/challenge-platform/|cf-browser-verification|id=\"challenge-(?:form|running|stage)\"", re.I)),
    ("cloudflare", re.compile(r"checking (?:if the site connection is secure|your browser before accessing)", re.I)),
    ("datadome", re.compile(r"captcha-delivery\.com|geo\.captcha-delivery|var dd=\{", re.I)),
    ("perimeterx", re.compile(r"px-captcha|_pxCaptcha|window\._pxAppId|/_px/|perimeterx|human challenge", re.I)),
    ("incapsula", re.compile(r"_Incapsula_Resource|incapsula incident id", re.I)),
    ("akamai", re.compile(r"errors\.edgesuite\.net|<title>\s*access denied\s*</title>[\s\S]{0,2000}reference\s*#", re.I)),
    ("aws-waf", re.compile(r"awswaf|AwsWafIntegration|aws-waf-token", re.I)),
    ("kasada", re.compile(r"KPSDK|ips\.js\?", re.I)),
    ("vercel", re.compile(r"vercel security checkpoint", re.I)),
    ("reddit-block", re.compile(r"you've been blocked by network security|whoa there,? pardner", re.I)),
    ("amazon-captcha", re.compile(r"<title>\s*robot check\s*</title>|type the characters you see in this image", re.I)),
    ("ddos-guard", re.compile(r"ddos-guard\.net/|<title>\s*ddos-guard\s*</title>", re.I)),
    ("sucuri", re.compile(r"sucuri website firewall|sucuri\.net/privacy-policy", re.I)),
]

# Markers that also show up on normal pages (a login form with reCAPTCHA, a
# <noscript> hint). Counted only when the page has little readable text.
_WEAK = [
    ("captcha", re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com|recaptcha/api|cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)),
    ("captcha", re.compile(r"are you a (?:robot|human)|verify (?:that )?you are (?:a )?human|prove you(?:'re| are) (?:not a robot|human)|unusual traffic from your (?:computer )?network|press (?:&amp;|&) hold", re.I)),
    ("needs-javascript", re.compile(r"(?:please )?(?:enable|turn on) javascript|javascript is (?:required|disabled)|requires javascript|you need to enable javascript", re.I)),
    ("access-denied", re.compile(r"<title>\s*(?:access denied|forbidden|403 forbidden|blocked)\s*</title>", re.I)),
]

_TAG_RE = re.compile(r"<script\b[\s\S]*?</script>|<style\b[\s\S]*?</style>|<noscript\b[\s\S]*?</noscript>|<[^>]+>", re.I)
_WS_RE = re.compile(r"\s+")
_SPA_RE = re.compile(
    r"<div[^>]+id=\"(?:root|app|__next|__nuxt|svelte|main-app)\"[^>]*>\s*</div>|<app-root[^>]*>\s*</app-root>",
    re.I,
)

SMALL_TEXT = 1500


def visible_text_len(html: str) -> int:
    text = _TAG_RE.sub(" ", html[:2_000_000])
    return len(_WS_RE.sub(" ", text).strip())


def detect_challenge(html: str, status: int | None = None, headers: Mapping[str, str] | None = None) -> str | None:
    """Return a short reason when the response is a bot wall or challenge, else None."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare"
    if "x-datadome" in headers or "x-dd-b" in headers:
        if status in BLOCK_STATUSES or (status or 200) >= 400:
            return "datadome"
    if headers.get("x-amzn-waf-action", "").lower() in {"challenge", "captcha"}:
        return "aws-waf"
    sample = html[:400_000] if html else ""
    for name, rx in _TITLES:
        if rx.search(sample):
            return name
    text_len = visible_text_len(sample)
    # Real pages often load the same vendors' sensor scripts (Cloudflare's
    # /cdn-cgi/challenge-platform/ JS detections, PerimeterX, AWS WAF). On a
    # long page with a good status those markers do not mean a wall.
    big_ok_page = (status is None or status < 400) and text_len > 6000
    if not big_ok_page:
        for name, rx in _STRONG:
            if rx.search(sample):
                return name
    if text_len < SMALL_TEXT:
        for name, rx in _WEAK:
            if rx.search(sample):
                return name
    return None


def needs_javascript(html: str, extracted_chars: int) -> bool:
    """True when the HTML is an app shell whose content only appears with JS."""
    if extracted_chars >= 250:
        return False
    if _SPA_RE.search(html or ""):
        return True
    scripts = len(re.findall(r"<script\b", html or "", re.I))
    return scripts >= 3 or len(html or "") > 5000
