"""Single anti-bot / challenge-page detector shared by all crawlers.

Audit (2026-11-21, all configured source pages): the old copies flagged any page whose
HTML contained "cloudflare" and the word "challenge" (Cloudflare bot-management scripts
are injected into normal pages, and hackathon/funding pages say "challenge"), so 18 of
23 flagged pages were real content (BIS, Verra, NIST, chain.link, ...). They also missed
real challenges served with 2xx (dblp's "Making sure you're not a bot!", AWS WAF 202).

Rules here:
* challenge-only markers (any page size): Cloudflare's ``cf_chl_opt`` challenge payload,
  a challenge page title, or a challenge response header;
* challenge wording only on short pages (real challenge pages carry little text);
* an empty 202 response (AWS WAF style).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

SHORT_PAGE_CHARS = 1500

_TITLE_PATTERNS = re.compile(
    r"^(?:just a moment\.*|attention required!?\s*\|\s*cloudflare|human verification|"
    r"making sure you'?re not a bot!?|vercel security checkpoint|pardon our interruption|"
    r"ddos-guard|checking your browser.*|security check(?:point)?|are you a robot\??)$",
    re.IGNORECASE,
)
_HTML_MARKERS = ("cf_chl_opt", "cf-chl-widget", "/cdn-cgi/challenge-platform/h/")
_SHORT_PAGE_PHRASES = (
    "verify you are human",
    "verify you are a human",
    "verifying you are human",
    "checking if the site connection is secure",
    "we're verifying your browser",
    "pardon our interruption",
    "vercel security checkpoint",
    "enable javascript and cookies to continue",
    "making sure you're not a bot",
)


def challenge_header(headers: Mapping[str, Any] | None) -> bool:
    if not headers:
        return False
    lowered = {str(k).lower(): str(v).lower() for k, v in dict(headers).items()}
    return (
        lowered.get("cf-mitigated") == "challenge"
        or lowered.get("x-vercel-mitigated") == "challenge"
        or lowered.get("x-amzn-waf-action", "") in {"challenge", "captcha"}
    )


def is_challenge_page(
    html: str = "",
    title: str = "",
    text: str = "",
    *,
    status: int | None = None,
    headers: Mapping[str, Any] | None = None,
) -> bool:
    html = html or ""
    title = re.sub(r"\s+", " ", title or "").strip()
    visible = re.sub(r"\s+", " ", text or "").strip()
    if challenge_header(headers):
        return True
    if title and _TITLE_PATTERNS.match(title):
        return True
    lowered_html = html.lower()
    if any(marker in lowered_html for marker in _HTML_MARKERS):
        return True
    if status == 202 and not visible:
        return True
    if len(visible) <= SHORT_PAGE_CHARS:
        sample = f"{title} {visible}".lower()
        if any(phrase in sample for phrase in _SHORT_PAGE_PHRASES):
            return True
        if "cloudflare" in sample and "ray id" in sample:
            return True
        if "just a moment" in sample and "enable javascript" in sample:
            return True
    return False
