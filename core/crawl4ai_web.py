"""Crawl4AI-based web acquisition engine for funding source collection.

Implements:
- BFS queue-based depth-aware traversal
- Two-stage discovery (high-recall link extraction → high-precision funding classification)
- Full PDF processing (no silent truncation)
- Pagination detection and following
- Explicit error tracking
- Domain boundary enforcement
- Honest completeness reporting
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import threading
import zipfile
import xml.etree.ElementTree as ET
from urllib.robotparser import RobotFileParser
from collections import deque
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, List, Optional
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl, urlencode

from core.schemas import CollectionResult
from core.http_client import get_response

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

LOGGER = logging.getLogger("rif.crawl4ai_web")

# Use an identifiable crawler identity and respect publisher-specified access
# rules. Cache robots policy per origin; fail closed if the rules cannot be read.
_ROBOTS_USER_AGENT = "RIF/3.1"
_ROBOTS_CACHE_TTL = 6 * 60 * 60
_ROBOTS_FAILURE_TTL = 5 * 60
_DEFAULT_HOST_DELAY = 1.0
_ROBOTS_CACHE: dict[str, tuple[float, RobotFileParser | None, bool, float, str | None]] = {}
_ROBOTS_LOCK = threading.Lock()
_HOST_LAST_REQUEST: dict[str, float] = {}
_HOST_LOCK = threading.Lock()


def _get_robots_policy(url: str) -> tuple[bool, float, str | None]:
    """Return (allowed, minimum_delay_seconds, reason) for a URL."""
    parts = urlparse(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False, _DEFAULT_HOST_DELAY, "unsupported URL scheme or missing host"

    origin = f"{parts.scheme}://{parts.netloc.lower()}"
    now = time.monotonic()
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(origin)
        if cached and cached[0] > now:
            _, parser, allow_all, delay, error = cached
            if error:
                return False, delay, error
            if allow_all:
                return True, delay, None
            assert parser is not None
            allowed = parser.can_fetch(_ROBOTS_USER_AGENT, url)
            return allowed, delay, None if allowed else "robots.txt disallows this URL"

    robots_url = f"{origin}/robots.txt"
    # Count/police the robots request too. The first content fetch for a new
    # host must still wait at least one configured interval after robots.txt.
    if not _wait_for_host_slot(robots_url, _DEFAULT_HOST_DELAY):
        return False, _DEFAULT_HOST_DELAY, "host delay would exceed the crawl deadline before robots.txt could be checked"

    try:
        response = get_response(
            robots_url,
            headers={"User-Agent": _ROBOTS_USER_AGENT},
            timeout=(5.0, 10.0),
            # A 4xx response (except 429) means robots.txt is unavailable under
            # RFC 9309; this crawler explicitly permits the resource in that case.
            # A 429 remains fail-closed, and 5xx/network failures remain fail-closed.
            retries=2,
            expected_status=(200, *range(400, 500)),
        )
        status_code = int(response.status_code)
        if status_code == 429:
            raise RuntimeError("robots.txt returned HTTP 429; rate-limited access is deferred")
        if 400 <= status_code < 500:
            with _ROBOTS_LOCK:
                _ROBOTS_CACHE[origin] = (
                    time.monotonic() + _ROBOTS_FAILURE_TTL, None, True,
                    _DEFAULT_HOST_DELAY, None,
                )
            return True, _DEFAULT_HOST_DELAY, None
        if status_code != 200:
            raise RuntimeError(f"robots.txt returned HTTP {status_code}")

        parser = RobotFileParser(robots_url)
        parser.parse(str(response.text or "").splitlines())
        delays = [
            float(parser.crawl_delay(_ROBOTS_USER_AGENT) or 0.0),
            float(parser.crawl_delay("*") or 0.0),
        ]
        for rate in (parser.request_rate(_ROBOTS_USER_AGENT), parser.request_rate("*")):
            if rate and rate.requests > 0:
                delays.append(float(rate.seconds) / float(rate.requests))
        delay = max([_DEFAULT_HOST_DELAY, *delays])
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[origin] = (
                time.monotonic() + _ROBOTS_CACHE_TTL, parser, False, delay, None,
            )
        allowed = parser.can_fetch(_ROBOTS_USER_AGENT, url)
        return allowed, delay, None if allowed else "robots.txt disallows this URL"
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        reason = f"could not verify robots.txt policy ({status_code or type(exc).__name__}: {exc})"
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[origin] = (
                time.monotonic() + _ROBOTS_FAILURE_TTL, None, False,
                _DEFAULT_HOST_DELAY, reason,
            )
        return False, _DEFAULT_HOST_DELAY, reason


def _wait_for_host_slot(url: str, delay_seconds: float) -> bool:
    """Reserve a per-host request slot without serializing unrelated hosts."""
    host = urlparse(url).netloc.lower()
    now = time.monotonic()
    with _HOST_LOCK:
        last = _HOST_LAST_REQUEST.get(host)
        request_start = now if last is None else max(
            now, last + max(delay_seconds, _DEFAULT_HOST_DELAY)
        )
        # Reserve before sleeping so concurrent workers cannot launch multiple
        # requests to the same host at once. Different hosts are not serialized.
        _HOST_LAST_REQUEST[host] = request_start

    wait_seconds = max(0.0, request_start - now)
    if wait_seconds:
        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx and ctx.deadline and time.time() + wait_seconds >= ctx.deadline:
            return False
        time.sleep(wait_seconds)
    return True

# --- Stage B funding classifier keywords (used on page CONTENT, not just URLs) ---
FUNDING_CONTENT_SIGNALS = [
    "grant", "funding", "rfp", "request for proposals", "call for proposals",
    "call for applications", "funding opportunity", "foa", "accelerator",
    "builder funding", "research funding", "challenge prize", "bounty",
    "application program", "open call", "apply now", "apply", "application",
    "submission form", "submit", "submit a proposal", "deadline", "eligibility",
    "award", "fellowship", "scholarship",
    "seed funding", "ecosystem support", "devgrant", "hackathon funding",
]

# --- Stage A URL-path signals (broad recall, NOT sole classifier) ---
DISCOVERY_PATH_SIGNALS = [
    "grant", "funding", "rfp", "apply", "call", "opportunit",
    "award", "fellowship", "challenge", "bounty", "proposal", "foa",
    "open-call", "accelerat", "builder-fund", "devgrant",
    "prize", "competition", "devhub", "campaign", "sponsorship", "pots",
]

# --- Pagination link signals ---
PAGINATION_SIGNALS = [
    "next", "next page", "older", "more", "load more", "show more",
    "page 2", "page 3", "page 4", "page 5",
]


try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    HAS_CRAWL4AI = True
except ImportError:
    HAS_CRAWL4AI = False


def _normalize_url(url: str) -> str:
    """Remove fragments without changing the requested path or trailing slash."""
    defragged, _ = urldefrag(url)
    return defragged


def _url_dedupe_key(url: str) -> str:
    """Normalize URL aliases and strip common tracking parameters for identity checks."""
    parsed = urlparse(_normalize_url(url))
    path = parsed.path.rstrip("/") or "/"
    # Tracking parameters do not identify distinct content. Remove them before
    # sorting query pairs so links to the same call from multiple campaign cards
    # do not consume the crawl budget or inflate the unresolved coverage frontier.
    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid",
    }
    query_pairs = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in tracking_params
    ]
    query_pairs.sort(key=lambda pair: pair[0])
    normalized_query = urlencode(query_pairs, doseq=True)
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        query=normalized_query,
        fragment="",
    ).geturl()


def _is_allowed_domain(url: str, base_domain: str, allowed_domains: list[str]) -> bool:
    """Check if URL is within source boundary."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return False
    allowed = {base_domain.lower()} | {d.lower() for d in allowed_domains}
    return any(host == d or host.endswith("." + d) for d in allowed)


def _matches_excluded_url(url: str, patterns: list[str]) -> bool:
    """Return True when a configured source-specific exclusion matches a URL."""
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            if re.search(pattern, url, flags=re.IGNORECASE):
                return True
        except re.error as exc:
            LOGGER.warning("Ignoring invalid exclude_url_patterns regex %r: %s", pattern, exc)
    return False


def _classify_link_stage_a(url: str, anchor_text: str, surrounding_text: str) -> float:
    """Stage A: high-recall scoring. Returns a 0-1 relevance score."""
    score = 0.0
    url_lower = url.lower()
    anchor_lower = (anchor_text or "").lower()
    context_lower = (surrounding_text or "").lower()

    # Distinguish individual-call routes from broad navigation labels.
    # A path like /your-opportunities or /funding is usually a directory, not
    # evidence that the linked page is itself an opportunity. Specific call
    # routes and item slugs retain the high-recall score; broad terms alone are
    # weak signals and must not create a coverage blocker without corroboration.
    path = urlparse(url_lower).path.rstrip("/") or "/"
    strong_path_patterns = (
        r"(?:^|/)(?:open-calls?|calls?-for-proposals?|calls?-for-applications?|"
        r"funding-opportunities|grant-opportunities|rfp|rfps|foa|nofo|solicitation)(?:/|$)",
        r"(?:^|/)(?:grants?|opportunities|campaigns?|devhub|devgrants)/[^/]+(?:/|$)",
        r"(?:^|/)calls?-funding/[^/]+(?:/|$)",
        r"(?:^|/)(?:builder-fund|open-call|devgrant)(?:[-/]|$)",
    )
    if any(re.search(pattern, path) for pattern in strong_path_patterns):
        score += 0.4
    elif any(signal in path for signal in DISCOVERY_PATH_SIGNALS):
        score += 0.25

    # Explicit document filenames can identify useful evidence even when the
    # directory path is generic (e.g. proposal-template.docx). Keep this narrower
    # than a file-extension bonus: annual reports and privacy PDFs are not calls.
    document_name = path.rsplit("/", 1)[-1]
    is_document = bool(re.search(r"\.(?:pdf|docx?|xlsx?|odt|rtf)$", document_name))
    document_evidence = re.search(
        r"(?:\bproposal\b|\bapplication\b|\bguidelines?\b|\bcall\b|\bgrants?\b|"
        r"\bfunding\b|\beligibility\b|\btemplate\b|\bsolicitation\b|\bnofo\b|"
        r"innovation(?:%20|[-_ ])projects?(?:%20|[-_ ])20\d{2})",
        f"{document_name} {anchor_lower}",
        flags=re.IGNORECASE,
    )
    if is_document and document_evidence:
        score = max(score, 0.4)

    # Anchor text signals
    for signal in FUNDING_CONTENT_SIGNALS:
        if signal in anchor_lower:
            score += 0.5
            break

    # Official opportunity attachments can have opaque/generated URLs and a
    # filename such as PA-27-101-Revised-Full-Announcement.html. Treat only
    # explicit solicitation/NOFO/full-announcement markers as strong evidence;
    # do not make every generic attachment or PDF relevant.
    official_notice_signal = re.search(
        r"(?:full[-_ ]announcement|notice[-_ ]of[-_ ]funding[-_ ]opportunity|"
        r"(?:^|[/_.-])solicitation(?:[/_.-]|$)|(?:^|[/_.-])nofo(?:[/_.-]|$))",
        f"{path} {anchor_lower}",
        flags=re.IGNORECASE,
    )
    if official_notice_signal:
        score = max(score, 0.5)

    # Surrounding text is weak evidence: a page about funding can contain
    # navigation, policy, account, or vendor links that are not opportunities.
    # Do not let context alone cross the 0.4 crawl threshold; path or anchor
    # evidence (or an explicit document URL) must support candidate relevance.
    for signal in FUNDING_CONTENT_SIGNALS:
        if signal in context_lower:
            score += 0.1
            break

    # File type alone is not evidence of funding relevance: these sites also link
    # privacy policies, procurement archives, and generic manuals as PDFs. A PDF
    # with funding-specific path or anchor evidence already receives a normal score.
    return min(score, 1.0)


def _reports_no_active_funding(url: str, title: str, content: str) -> bool:
    """Detect empty-state messages on the configured source's actual listing page.

    Keep URL scoping tight: publishers sometimes repeat notice text in a site-wide
    modal/footer, so a message on one programme must not erase evidence from every
    detail page on the same domain.
    """
    path = urlparse(url).path.rstrip("/").lower()
    combined = re.sub(r"\s+", " ", f"{title} {content}".lower())
    if path == "/applicants/rfp" and re.search(
        r"\bno rfp items available\b|\bthere are currently no active requests? for proposals? available for application\b",
        combined,
        flags=re.IGNORECASE,
    ):
        return True
    if path == "/applicants/open-rounds" and re.search(
        r"\bthere are no active grant rounds?\b|\bthere are no active rounds? at this time\b",
        combined,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _is_generic_funding_hub(url: str, title: str, content: str) -> bool:
    """Keep funding directories as traversal roots, not individual grant records."""
    path = urlparse(url).path.rstrip("/").lower()
    title_lower = re.sub(r"\s+", " ", (title or "").lower()).strip()
    title_root = re.split(r"\s+[|·]\s+", title_lower, maxsplit=1)[0].strip()
    generic_titles = {
        "grant", "grants", "funding", "funding opportunities",
        "grants and funding", "funding and grants", "open rounds",
        "request for proposals", "request for proposals (rfps)", "rfps",
    }
    origin = f"{urlparse(url).scheme.lower()}://{urlparse(url).netloc.lower()}"
    combined = re.sub(r"\s+", " ", f"{title} {content}".lower())

    # ESP applicant/RFP/open-round/wishlist routes are landing pages, not one
    # individual opportunity. A route fallback to the parent applicant page
    # must never be emitted as a funding call.
    if origin == "https://esp.ethereum.foundation" and path in {
        "/applicants", "/applicants/rfp", "/applicants/open-rounds", "/applicants/wishlist",
    }:
        return True

    # XRPL's home page is a multi-program directory. Its current-looking
    # headline can conflict with linked application pages whose deadlines have
    # passed, so verify each linked programme instead of normalizing the hub.
    if origin == "https://xrplgrants.org" and path in {"", "/"}:
        return True
    # Awardee listings are historical output, not individual application calls.
    if origin == "https://xrplgrants.org" and path == "/awardees":
        return True

    # NEARN and POTLOCK pages below are opportunity directories. Keep them as
    # traversal roots and let their individual child pages provide call evidence.
    if origin == "https://nearn.io" and path in {"/", "/devhub", "/infra-committee", "/nf-bo"}:
        return True
    if origin == "https://app.potlock.org" and path in {"/campaigns", "/pots"}:
        return True

    # Superteam Earn landing/category pages list many individual grants. Keep them
    # as traversal roots; classify the linked /earn/grants/<slug> pages independently.
    superteam_hub_paths = {
        "https://superteam.fun": {
            "/earn",
            "/earn/opportunities/development-grants",
            "/earn/regions/india",
        },
        "https://earn.superteam.fun": {"", "/grants", "/regions/india"},
    }
    if path in superteam_hub_paths.get(origin, set()):
        return True

    if title_root in generic_titles or re.fullmatch(
        r"different grants? and funding opportunities within .+ ecosystem",
        title_root,
    ):
        return True

    # These routes are directory/search results by definition, even if their
    # aggregated cards contain many deadline/application phrases. Keep them as
    # traversal roots so individual call pages are classified independently.
    explicit_directory_paths = {
        "https://fil.org": {"/grants"},
        "https://www.fil.org": {"/grants"},
        "https://climate-kic.org": {"/get-involved/open-calls"},
        "https://www.climate-kic.org": {"/get-involved/open-calls"},
        "https://sbir.gov": {"/topics"},
        "https://www.sbir.gov": {"/topics"},
        "https://ec.europa.eu": {
            "/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals"
        },
    }
    if path in explicit_directory_paths.get(origin, set()):
        return True

    # Use directory copy only for known landing-page URLs. A detail page may
    # include this language in a reused footer or navigation section.
    known_hub_paths = {
        "https://www.near.org": {"/funding"},
        "https://near.org": {"/funding"},
        "https://solana.org": {"/grants-funding", "/grants"},
    }
    origin = f"{urlparse(url).scheme.lower()}://{urlparse(url).netloc.lower()}"
    if path not in known_hub_paths.get(origin, set()):
        return False
    directory_markers = (
        r"\bexplore grants, incentives, and community-backed resources\b",
        r"\bthere are numerous grants programs run by teams\b",
        r"\bfollowing ecosystem teams run their own grants programs\b",
    )
    combined = re.sub(r"\s+", " ", f"{title} {content}".lower())
    return any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in directory_markers)


def _classify_page_stage_b(url: str, title: str, content: str) -> bool:
    """Stage B: high-precision funding classification on actual page content."""
    url_lower = url.lower()
    combined = f"{title}\n{content}".lower()

    # Explicitly empty listings and ecosystem directories are traversal roots,
    # not individual applications. Their links are still explored below.
    if _reports_no_active_funding(url, title, content) or _is_generic_funding_hub(url, title, content):
        return False
    
    # 1. Broad generic hubs and search forms
    index_patterns = [
        "/search", "/index", "/list", "/initiatives", 
        "awardsearch", "simple-search"
    ]
    is_hub = any(p in url_lower for p in index_patterns)
    
    # 2. Action signals (Must be present for an opportunity page)
    action_signals = [
        "eligibility", "how to apply", "submit a proposal", "apply now", 
        "deadline", "call for proposals", "call for applications", 
        "request for proposals", "evaluation criteria", "award amount",
        "funding amount", "eligible applicants", "submission instructions",
        "proposal preparation", "concept paper", "letter of intent"
    ]
    
    # 3. Context signals
    context_signals = [
        "grant", "funding", "rfp", "accelerator", "bounty", "fellowship",
        "scholarship", "seed funding", "ecosystem support", "devgrant", "award"
    ]
    
    action_hits = sum(1 for s in action_signals if s in combined)
    context_hits = sum(1 for s in context_signals if s in combined)
    
    # Superteam Earn's individual grant pages are structured opportunity records.
    # Some (including paused calls) lack generic action phrases, so retain them when
    # both the URL and title identify a specific grant and the page has funding context.
    # The generic category hub is excluded earlier by _is_generic_funding_hub.
    parsed_url = urlparse(url_lower)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    path = parsed_url.path.rstrip("/")
    title_identity = re.split(r"\s+[|·]\s+", (title or "").lower(), maxsplit=1)[0].strip().rstrip(" .:-")
    generic_titles = {
        "grant", "grants", "funding", "funding opportunities",
        "grants and funding", "funding and grants",
    }
    is_superteam_detail = (
        origin in {"https://superteam.fun", "https://www.superteam.fun"}
        and bool(re.fullmatch(r"/earn/grants/[^/]+", path))
    )
    has_specific_grant_title = (
        title_identity not in generic_titles
        and bool(re.search(r"\b(?:grant|grants|funding|fellowship|bounty|challenge)\b", title_identity))
    )
    if is_superteam_detail and has_specific_grant_title and context_hits >= 1:
        return True

    if is_hub:
        # A hub needs strong proof to be classified as a detail opportunity
        return action_hits >= 3 and context_hits >= 2

    # Detail page needs both context and some actionable requirement language
    return action_hits >= 1 and context_hits >= 2


def _is_pagination_link(anchor_text: str, rel: str, url: str) -> bool:
    """Detect pagination links from anchor text, rel attribute, or URL patterns."""
    anchor_lower = (anchor_text or "").strip().lower()
    rel_lower = (rel or "").lower()

    if "next" in rel_lower:
        return True
    for signal in PAGINATION_SIGNALS:
        if signal == anchor_lower or anchor_lower.startswith(signal):
            return True
    # URL pattern: ?page=N
    if re.search(r"[?&]page=\d+", url):
        return True
    return False


def _get_final_page_url(result: Any, requested_url: str) -> str:
    """Prefer Crawl4AI's explicit redirect destination over the original request URL."""
    return (
        getattr(result, "redirected_url", None)
        or getattr(result, "url", None)
        or requested_url
    )


def _superteam_directory_wait_options(url: str) -> dict[str, Any]:
    """Wait for current grant-card anchors on known Superteam directory routes."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    directory_paths = {
        "/earn",
        "/earn/opportunities/development-grants",
        "/earn/regions/india",
    }
    if host not in {"superteam.fun", "www.superteam.fun"} or path not in directory_paths:
        return {}

    # Superteam loads individual grant cards client-side and may populate them after
    # DOMContentLoaded. Scroll to trigger lazy-loaded cards, then wait specifically
    # for detail links before extracting HTML/Markdown; do not apply this delay to
    # ordinary opportunity detail pages or unrelated funding sources.
    return {
        "js_code_before_wait": "window.scrollTo(0, document.body.scrollHeight);",
        "wait_for": 'js:() => document.querySelectorAll(\'a[href*="/earn/grants/"]\').length > 0',
        "delay_before_return_html": 1.0,
    }


def _ethereum_esp_wait_options(url: str) -> dict[str, Any]:
    """Wait for ESP's client-rendered RFP cards before extracting links."""
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() not in {
        "esp.ethereum.foundation", "www.esp.ethereum.foundation"
    }:
        return {}
    if parsed.path.rstrip("/") not in {"/applicants/rfp", "/applicants/open-rounds"}:
        return {}
    return {
        "js_code_before_wait": "window.scrollTo(0, document.body.scrollHeight);",
        "wait_for": (
            "js:() => document.querySelectorAll("
            "'a[href*=\"/applicants/rfp/\"], a[href*=\"/applicants/open-rounds/\"]'"
            ").length > 0 || document.body.innerText.toLowerCase().includes("
            "'there are no active grant rounds')"
        ),
        "delay_before_return_html": 0.75,
    }


def _raw_markdown_text(result: Any) -> str:
    """Read the generated Markdown across Crawl4AI result API variants."""
    for attribute in ("markdown", "markdown_v2"):
        candidate = getattr(result, attribute, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        for text_attribute in ("raw_markdown", "fit_markdown"):
            value = getattr(candidate, text_attribute, None)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _extract_markdown_links(markdown: str, page_url: str) -> list[dict]:
    """Extract navigable links from Markdown when rendered HTML omits card anchors."""
    if not markdown:
        return []

    pattern = re.compile(
        r'(?<!!)\[([^\n]{1,300}?)\]\(\s*(?:<([^>]+)>|([^\s)]+))'
        r'(?:\s+"[^"]*")?\s*\)'
    )

    links: list[dict] = []
    seen: set[str] = set()
    excluded_assets = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf")
    excluded_paths = ("/terms", "/privacy", "/cookies", "/login", "/signup", "/auth", "/signin")

    for match in pattern.finditer(markdown):
        label = re.sub(r"[*_~]", "", match.group(1)).strip()
        target = (match.group(2) or match.group(3) or "").strip()
        if not target or target.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        resolved = _normalize_url(urljoin(page_url, target))
        if not resolved.startswith(("http://", "https://")):
            continue
        parsed = urlparse(resolved)
        path_lower = parsed.path.lower()
        if path_lower.endswith(excluded_assets) or any(path_lower.startswith(item) for item in excluded_paths):
            continue
        key = _url_dedupe_key(resolved)
        if key in seen:
            continue
        seen.add(key)

        line_start = markdown.rfind("\n", 0, match.start()) + 1
        line_end = markdown.find("\n", match.end())
        if line_end < 0:
            line_end = len(markdown)
        context = re.sub(r"\s+", " ", markdown[line_start:line_end]).strip()[:300]
        links.append({
            "url": resolved,
            "anchor_text": label,
            "surrounding_text": context,
            "rel": "",
        })
    return links


async def _fetch_url(url: str) -> dict:
    """Fetch a single URL using Crawl4AI and extract links with context."""
    from core.crawl_context import current_crawl_context
    import asyncio
    import time
    
    ctx = current_crawl_context.get(None)
    timeout = 30.0
    if ctx and ctx.deadline:
        timeout = max(5.0, ctx.deadline - time.time())
        
    browser_config = BrowserConfig(headless=True)
    run_options = {
        "cache_mode": CacheMode.BYPASS,
        "page_timeout": int(timeout * 1000),
        **_superteam_directory_wait_options(url),
        **_ethereum_esp_wait_options(url),
    }
    run_config = CrawlerRunConfig(**run_options)
    async with AsyncWebCrawler(config=browser_config) as crawler:
        try:
            result = await asyncio.wait_for(crawler.arun(url=url, config=run_config), timeout=timeout + 5.0)
        except asyncio.TimeoutError:
            class DummyResult:
                success = False
                error_message = "Timeout"
                html = ""
                markdown_v2 = type("M", (), {"raw_markdown": ""})
            result = DummyResult()

        page_url = _get_final_page_url(result, url)
        markdown_text = _raw_markdown_text(result)
        links = []
        pagination_links = []
        title = ""
        if result.html:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(result.html, "html.parser")
                # Extract title
                title_tag = soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else ""

                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    if not href or href.startswith("#"):
                        continue

                    # Exclude non-content assets and protocol schemes
                    href_lower = href.lower()
                    if any(p in href_lower for p in [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", "mailto:", "tel:", "javascript:"]):
                        continue

                    anchor_text = a_tag.get_text(strip=True)
                    rel = a_tag.get("rel", "")
                    if isinstance(rel, list):
                        rel = " ".join(rel)

                    # Get surrounding text from immediate parent, not whole sidebar/body
                    container = a_tag.find_parent(["p", "li", "dd", "td", "h1", "h2", "h3", "h4", "h5", "h6"])
                    surrounding = ""
                    if container:
                        container_text = container.get_text(strip=True)
                        if len(container_text) <= 300:
                            surrounding = container_text

                    # Resolve relative URLs
                    resolved = urljoin(page_url, href)
                    resolved = _normalize_url(resolved)

                    if not resolved.startswith("http"):
                        continue

                    # Exclude common non-content paths and duplicate language mirrors
                    path_lower = urlparse(resolved.lower()).path
                    if (
                        any(ex in path_lower for ex in ["/terms", "/privacy", "/cookies", "/login", "/signup", "/auth", "/signin"])
                        or path_lower == "/hi"
                        or path_lower.startswith("/hi/")
                    ):
                        continue

                    links.append({
                        "url": resolved,
                        "anchor_text": anchor_text,
                        "surrounding_text": surrounding,
                        "rel": rel,
                    })

                    if _is_pagination_link(anchor_text, rel, resolved):
                        pagination_links.append(resolved)

            except Exception as e:
                LOGGER.warning("Link extraction failed for %s: %s", url, e)

        # Some JS-rendered card grids are present in Crawl4AI's Markdown but
        # expose no usable <a href> in the returned HTML. Supplement HTML links
        # with Markdown links so opportunity cards remain discoverable.
        known_link_keys = {_url_dedupe_key(item["url"]) for item in links}
        for markdown_link in _extract_markdown_links(markdown_text, page_url):
            link_key = _url_dedupe_key(markdown_link["url"])
            if link_key in known_link_keys:
                continue
            known_link_keys.add(link_key)
            links.append(markdown_link)
            if _is_pagination_link(
                markdown_link["anchor_text"], markdown_link["rel"], markdown_link["url"]
            ):
                pagination_links.append(markdown_link["url"])

        return {
            "html": result.html,
            "markdown": markdown_text,
            "success": result.success,
            "error_message": getattr(result, "error_message", None),
            # Crawl4AI exposes first-response and final redirect statuses separately.
            "status_code": getattr(result, "status_code", None),
            "redirected_status_code": getattr(result, "redirected_status_code", None),
            "url": page_url,
            "requested_url": url,
            "redirected_url": getattr(result, "redirected_url", None),
            "title": title,
            "links": links,
            "pagination_links": pagination_links,
        }


def _fetch_sync(url: str) -> dict:
    """Synchronous wrapper for _fetch_url."""
    return asyncio.run(_fetch_url(url))


def _fetch_pdf(url: str, max_pdf_pages: int | None = None) -> dict:
    """Fetch and extract text from a PDF. No silent truncation."""
    if PdfReader is None:
        return {"success": False, "error": "pypdf not installed", "url": url,
                "content": "", "total_pages": 0, "pages_processed": 0, "truncated": False}

    try:
        response = get_response(url, timeout=(8.0, 30.0), retries=2)
        reader = PdfReader(BytesIO(response.content))
        total_pages = len(reader.pages)

        if max_pdf_pages is not None and total_pages > max_pdf_pages:
            pages_to_process = reader.pages[:max_pdf_pages]
            truncated = True
        else:
            pages_to_process = reader.pages
            truncated = False

        pages_processed = len(pages_to_process)
        content = "\n".join((page.extract_text() or "") for page in pages_to_process)

        return {
            "success": True,
            "url": url,
            "content": content,
            "total_pages": total_pages,
            "pages_processed": pages_processed,
            "truncated": truncated,
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "content": "",
            "total_pages": 0,
            "pages_processed": 0,
            "truncated": False,
            "error": str(e),
        }


def _extract_docx_title(paragraphs: list[str], url: str) -> str:
    """Prefer a call/fellowship heading in the document over a filename slug."""
    title_pattern = re.compile(
        r"\b(?:call for|proposal call|request for proposals?|funding opportunity|"
        r"grant(?:s)? program|fellowships?|scholarships?|application for)\b",
        flags=re.IGNORECASE,
    )
    for raw in paragraphs[:40]:
        candidate = re.sub(r"\s+", " ", str(raw or "")).strip(" \t:.-")
        if not candidate or len(candidate) > 180 or len(candidate) < 5:
            continue
        if "@" in candidate or re.search(r"\b(?:phone|email|www\.)\b", candidate, re.IGNORECASE):
            continue
        # Many downloadable forms begin with a repeated document label such as
        # "CHANAKYA FELLOWSHIP – APPLICATION FORM". It identifies the artifact,
        # not the particular fellowship category; continue to the next heading.
        generic_form_title = re.fullmatch(
            r"(?:(?:[A-Z0-9][A-Z0-9 .&()/–—-]{1,60})\s+)?"
            r"(?:FELLOWSHIP|SCHOLARSHIP)\s*[–—-]\s*APPLICATION FORM",
            candidate,
            flags=re.IGNORECASE,
        )
        if generic_form_title:
            continue
        if title_pattern.search(candidate):
            return candidate
    filename = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[-_]+", " ", filename).strip() or "Untitled application document"


def _fetch_docx(url: str, max_chars: int = 50000) -> dict:
    """Download a DOCX directly and extract paragraph text without browser navigation."""
    try:
        response = get_response(url, timeout=(8.0, 30.0), retries=2)
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            try:
                document_xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise ValueError("DOCX archive has no word/document.xml") from exc

        root = ET.fromstring(document_xml)
        word_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(f"{word_ns}p"):
            parts: list[str] = []
            for node in paragraph.iter():
                if node.tag == f"{word_ns}t":
                    parts.append(node.text or "")
                elif node.tag == f"{word_ns}tab":
                    parts.append("\t")
                elif node.tag in {f"{word_ns}br", f"{word_ns}cr"}:
                    parts.append("\n")
            value = "".join(parts).strip()
            if value:
                paragraphs.append(value)

        content = "\n".join(paragraphs).strip()
        if not content:
            raise ValueError("DOCX downloaded successfully but contained no extractable paragraph text")

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]

        return {
            "success": True,
            "url": url,
            "title": _extract_docx_title(paragraphs, url),
            "content": content,
            "paragraphs_extracted": len(paragraphs),
            "truncated": truncated,
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "url": url,
            "content": "",
            "paragraphs_extracted": 0,
            "truncated": False,
            "error": str(exc),
        }


def _finalize_collection_status(result: CollectionResult, *, test_limits: bool = False) -> str:
    """Set a conservative source status from observed coverage, not record count alone.

    A candidate may be linked by multiple cards or parent pages. Preserve the first
    parent for diagnostics but count each normalized URL/reason pair only once.
    """
    unique_gaps = []
    seen_gap_keys = set()
    for gap in result.coverage_gaps or []:
        url = str(gap.get("url") or "").strip()
        reason = str(gap.get("reason") or "")
        key = (_url_dedupe_key(url) if url else "", reason)
        if key in seen_gap_keys:
            continue
        seen_gap_keys.add(key)
        unique_gaps.append(gap)
    result.coverage_gaps = unique_gaps
    if result.coverage_gaps:
        result.relevant_urls_skipped = len({
            _url_dedupe_key(str(gap.get("url") or ""))
            for gap in result.coverage_gaps if gap.get("url")
        })
    successful_fetches = result.pages_fetched > 0 or result.documents_fetched > 0
    has_collection_failures = bool(
        result.errors or result.pages_failed > 0 or result.documents_failed > 0
    )
    has_coverage_gaps = bool(result.coverage_gaps or result.relevant_urls_skipped)

    if has_coverage_gaps:
        result.discovery_exhausted = False
        result.pagination_complete = False

    if not successful_fetches and result.pages_failed > 0:
        result.final_status = "FAIL"
    elif result.truncated:
        result.final_status = "TEST_TRUNCATED" if test_limits else "PARTIAL"
    elif has_collection_failures:
        result.final_status = "PARTIAL" if successful_fetches else "FAIL"
    elif has_coverage_gaps:
        result.final_status = "PARTIAL" if successful_fetches else "FAIL"
    elif successful_fetches:
        if result.opportunities:
            complete = (
                (result.pagination_detected and result.pagination_complete)
                or (not result.pagination_detected and result.discovery_exhausted)
            )
            result.final_status = "VERIFIED" if complete else "PARTIAL"
        else:
            # A zero-result source is negative evidence only after its known
            # discovery boundary was exhausted without fetch errors or skipped
            # relevant routes. Otherwise preserve uncertainty as PARTIAL.
            result.final_status = (
                "NO_ACTIVE_OPPORTUNITIES"
                if result.discovery_exhausted and result.no_active_opportunities_confirmed
                else "PARTIAL"
            )
    else:
        result.final_status = "FAIL"
    return result.final_status


def crawl_funding_source(
    source_id: str,
    url: str,
    discovery_config: dict,
    allowed_domains: list[str],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Crawl a funding source using BFS traversal with two-stage discovery.

    Parameters
    ----------
    source_id : str
        Human-readable source name.
    url : str
        Primary URL for the source.
    discovery_config : dict
        YAML discovery block (start_urls, max_depth, pagination, etc.).
    allowed_domains : list[str]
        Additional domains allowed beyond the source's own domain.

    Returns
    -------
    CollectionResult
        With honest completeness reporting.
    """
    result = CollectionResult(source_id=source_id, method="web")

    start_urls = discovery_config.get("start_urls", [url])
    configured_max_depth = discovery_config.get("max_depth", 2)
    configured_max_pages = discovery_config.get("max_pages")  # None = no ceiling
    follow_pdfs = discovery_config.get("follow_pdfs", True)
    follow_documents = bool(discovery_config.get("follow_documents", False))
    follow_detail = discovery_config.get("follow_detail_pages", True)
    direct_page = bool(discovery_config.get("direct_page", False))
    raw_direct_page_urls = discovery_config.get("direct_page_urls", []) or []
    if isinstance(raw_direct_page_urls, str):
        raw_direct_page_urls = [raw_direct_page_urls]
    direct_page_url_keys = {
        _url_dedupe_key(_normalize_url(candidate))
        for candidate in raw_direct_page_urls
        if isinstance(candidate, str)
        and urlparse(candidate).scheme in {"http", "https"}
        and urlparse(candidate).netloc
    }
    raw_exclusion_patterns = discovery_config.get("exclude_url_patterns", []) or []
    if isinstance(raw_exclusion_patterns, str):
        raw_exclusion_patterns = [raw_exclusion_patterns]
    exclude_url_patterns = [p for p in raw_exclusion_patterns if isinstance(p, str) and p]
    max_pdf_pages = discovery_config.get("max_pdf_pages")  # None = process all

    if test_limits:
        configured_max_pages = test_limits.get("max_pages", configured_max_pages)
        # also apply it as a hard stop flag in the loop later, but we can just override the vars here
    if source_limits and "max_pages" in source_limits:
        configured_max_pages = source_limits["max_pages"]

    result.max_depth = configured_max_depth

    # Determine base domain from primary URL
    base_domain = urlparse(url).netloc.lower()

    # BFS queue: (url, depth)
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set()

    for start in start_urls:
        normalized = _normalize_url(start)
        dedupe_key = _url_dedupe_key(normalized)
        if dedupe_key not in visited:
            queue.append((normalized, 0))
            visited.add(dedupe_key)

    # max_pages is a request-attempt budget, not a successful-fetch budget.
    # Count failures too so repeated network errors cannot evade the source cap.
    pages_attempted_count = 0

    while queue:
        current_url, depth = queue.popleft()

        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        source_max_documents = None
        source_max_seconds = None
        if test_limits:
            source_max_documents = test_limits.get("max_documents")
            source_max_seconds = test_limits.get("max_seconds")
        if source_limits:
            if "max_documents" in source_limits:
                source_max_documents = source_limits["max_documents"]
            if "max_seconds" in source_limits:
                source_max_seconds = source_limits["max_seconds"]
        is_limit = ctx.check_limits(
            source_max_pages=configured_max_pages,
            source_max_documents=source_max_documents,
            source_max_seconds=source_max_seconds,
            source_max_depth=configured_max_depth,
            current_depth=depth
        ) if ctx else False

        if is_limit:
            result.truncated = True
            result.pagination_complete = False
            LOGGER.info("Source %s: reached configured limits, marking PARTIAL", source_id)
            break

        # Check configured page ceiling (fallback if context isn't used)
        if configured_max_pages is not None and pages_attempted_count >= configured_max_pages:
            result.truncated = True
            result.pagination_complete = False
            LOGGER.info("Source %s: reached configured max_pages=%d, marking PARTIAL",
                        source_id, configured_max_pages)
            break

        # Respect publisher robots.txt before fetching each HTML/PDF URL.
        # Policy-denied or unverifiable URLs are recorded as partial coverage;
        # the crawler never solves CAPTCHAs or attempts to evade site controls.
        respect_robots = bool(discovery_config.get("respect_robots_txt", True))
        delay_seconds = max(
            _DEFAULT_HOST_DELAY,
            float(discovery_config.get("minimum_delay_seconds", _DEFAULT_HOST_DELAY) or _DEFAULT_HOST_DELAY),
        )
        if respect_robots:
            permitted, robots_delay, robots_reason = _get_robots_policy(current_url)
            delay_seconds = max(delay_seconds, robots_delay)
            if not permitted:
                # A publisher policy restriction is an explicit access limitation,
                # not a crawler-budget truncation. Record the skipped route as a
                # coverage gap and keep the source PARTIAL; never bypass robots.txt.
                result.urls_skipped += 1
                result.relevant_urls_skipped += 1
                result.coverage_gaps.append({
                    "url": current_url,
                    "parent_url": None,
                    "reason": "robots_policy_restriction",
                    "detail": robots_reason or "robots.txt does not permit this URL",
                })
                result.errors.append({
                    "url": current_url,
                    "operation": "robots_txt_policy",
                    "error": robots_reason or "robots.txt does not permit this URL",
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                LOGGER.info("Skipping %s for %s: %s", current_url, source_id, robots_reason)
                continue

        if not _wait_for_host_slot(current_url, delay_seconds):
            result.truncated = True
            result.pagination_complete = False
            result.errors.append({
                "url": current_url,
                "operation": "host_politeness_delay",
                "error": "Required host delay would exceed the crawl deadline",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            break

        pages_attempted_count += 1

        # Track max depth reached
        if depth > result.max_depth_reached:
            result.max_depth_reached = depth

        # Determine if this is a PDF
        is_pdf = current_url.lower().split("?", 1)[0].endswith(".pdf")

        if is_pdf:
            result.documents_discovered += 1
            if not follow_pdfs:
                # A discovered document may contain the only current call details.
                # Do not claim a complete/empty source when its content was skipped.
                result.urls_skipped += 1
                result.truncated = True
                result.pagination_complete = False
                continue
            pdf_result = _fetch_pdf(current_url, max_pdf_pages=max_pdf_pages)
            if pdf_result["success"]:
                result.documents_fetched += 1
                if ctx:
                    ctx.add_document()
                    ctx.add_page()
                if pdf_result["truncated"]:
                    result.truncated = True
                result.opportunities.append({
                    "type": "pdf",
                    "url": current_url,
                    "content": pdf_result["content"],
                    "title": urlparse(current_url).path.split("/")[-1].replace("-", " ").replace("_", " "),
                    "total_pages": pdf_result["total_pages"],
                    "pages_processed": pdf_result["pages_processed"],
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
            else:
                result.documents_failed += 1
                result.failed_urls.append(current_url)
                result.errors.append({
                    "url": current_url,
                    "operation": "pdf_fetch",
                    "error": pdf_result["error"],
                    "timestamp": datetime.now(UTC).isoformat(),
                })
            continue

        # DOCX links are downloads, not browser pages. Use the direct document
        # path only for sources which explicitly opt in to document following.
        is_docx = urlparse(current_url).path.lower().endswith(".docx")
        if is_docx:
            result.documents_discovered += 1
            if not follow_documents:
                result.urls_skipped += 1
                result.truncated = True
                result.pagination_complete = False
                continue

            docx_result = _fetch_docx(current_url)
            if docx_result["success"]:
                result.documents_fetched += 1
                if ctx:
                    ctx.add_document()
                    ctx.add_page()
                if docx_result["truncated"]:
                    result.truncated = True
                result.opportunities.append({
                    "type": "docx",
                    "url": current_url,
                    "content": docx_result["content"],
                    "title": docx_result.get("title") or urlparse(current_url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ").replace("_", " "),
                    "paragraphs_extracted": docx_result["paragraphs_extracted"],
                    "truncated": docx_result["truncated"],
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
            else:
                result.documents_failed += 1
                result.failed_urls.append(current_url)
                result.errors.append({
                    "url": current_url,
                    "operation": "docx_fetch",
                    "error": docx_result["error"],
                    "timestamp": datetime.now(UTC).isoformat(),
                })
            continue

        # Page counters intentionally exclude PDFs/DOCX; those use document counters.
        result.pages_discovered += 1

        # Fetch HTML page
        try:
            res = _fetch_sync(current_url)
        except Exception as e:
            result.pages_failed += 1
            result.failed_urls.append(current_url)
            result.errors.append({
                "url": current_url,
                "operation": "page_fetch",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        page_url = res.get("url") or current_url
        # A configured detail/listing route that redirects to its parent is not
        # evidence that the requested route was successfully crawled. Preserve
        # the gap instead of silently treating the parent page as the child.
        requested = urlparse(current_url)
        final = urlparse(page_url)
        requested_path = requested.path.rstrip("/") or "/"
        final_path = final.path.rstrip("/") or "/"
        if (
            depth == 0
            and (requested.hostname or "").lower() == (final.hostname or "").lower()
            and final_path != "/"
            and requested_path.startswith(final_path + "/")
        ):
            result.relevant_urls_skipped += 1
            result.coverage_gaps.append({
                "url": current_url,
                "final_url": page_url,
                "reason": "configured_start_url_redirected_to_parent_path",
            })
        # Record the final URL too, preventing redirects from being fetched again.
        visited.add(_url_dedupe_key(page_url))

        status_code = res.get("status_code")
        status_error = None
        if status_code is not None:
            try:
                numeric_status = int(status_code)
            except (TypeError, ValueError):
                numeric_status = None
            # Crawl4AI reports the first 3xx status separately from the
            # destination's final status. Judge the response that was crawled.
            final_redirect_status = res.get("redirected_status_code")
            if numeric_status is not None and 300 <= numeric_status < 400 and final_redirect_status is not None:
                try:
                    numeric_status = int(final_redirect_status)
                except (TypeError, ValueError):
                    pass
            if numeric_status is not None and not (200 <= numeric_status < 300):
                status_error = f"HTTP {numeric_status}"

        if not res["success"] or status_error:
            result.pages_failed += 1
            result.failed_urls.append(current_url)
            result.errors.append({
                "url": current_url,
                "operation": "page_fetch",
                "error": res.get("error_message") or status_error or "fetch failed",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        result.pages_fetched += 1
        if ctx:
            ctx.add_page()

        page_content = res["markdown"] or res["html"] or ""
        page_title = res.get("title", "")
        if depth == 0 and _reports_no_active_funding(page_url, page_title, page_content):
            result.no_active_opportunities_confirmed = True

        # Stage B: classify this page
        is_funding_page = _classify_page_stage_b(page_url, page_title, page_content)
        # direct_page explicitly identifies the configured starting URL as a
        # programme-level record, even when its page title looks like a hub.
        # Known empty-state listings remain excluded.
        if (
            depth == 0
            and (
                (direct_page and _url_dedupe_key(page_url) == _url_dedupe_key(url))
                or _url_dedupe_key(page_url) in direct_page_url_keys
            )
            and not _reports_no_active_funding(page_url, page_title, page_content)
        ):
            is_funding_page = True

        if is_funding_page:
            if depth > 0:
                # This is a detail page with funding content
                result.detail_pages_discovered += 1
                result.detail_pages_fetched += 1
                
            result.opportunities.append({
                "type": "html",
                "url": page_url,
                "content": page_content,
                "title": page_title,
                "depth": depth,
                "retrieved_at": datetime.now(UTC).isoformat(),
            })

        # Discover child links if within depth boundary
        if depth < configured_max_depth:
            # Process pagination links (same depth)
            if discovery_config.get("pagination", False):
                for pag_url in res.get("pagination_links", []):
                    normalized_pag = _normalize_url(pag_url)
                    pag_key = _url_dedupe_key(normalized_pag)
                    if _matches_excluded_url(normalized_pag, exclude_url_patterns):
                        result.urls_skipped += 1
                        continue
                    if pag_key not in visited:
                        result.pagination_detected = True
                        if _is_allowed_domain(normalized_pag, base_domain, allowed_domains):
                            visited.add(pag_key)
                            queue.append((normalized_pag, depth))  # same depth for pagination
                        else:
                            result.urls_skipped += 1
                            result.relevant_urls_skipped += 1
                            result.coverage_gaps.append({
                                "url": normalized_pag,
                                "parent_url": page_url,
                                "reason": "pagination_outside_allowed_domains",
                            })

            # Process discovered links (depth + 1)
            if follow_detail:
                for link_info in res.get("links", []):
                    link_url = _normalize_url(link_info["url"])
                    link_key = _url_dedupe_key(link_url)
                    if _matches_excluded_url(link_url, exclude_url_patterns):
                        result.urls_skipped += 1
                        continue
                    if link_key in visited:
                        result.duplicates_removed += 1
                        continue

                    # Score before enforcing the domain boundary so likely call or
                    # application routes on partner domains are visible in the audit.
                    score = _classify_link_stage_a(
                        link_url,
                        link_info.get("anchor_text", ""),
                        link_info.get("surrounding_text", ""),
                    )
                    if not _is_allowed_domain(link_url, base_domain, allowed_domains):
                        result.urls_skipped += 1
                        if score >= 0.4:
                            result.relevant_urls_skipped += 1
                            result.coverage_gaps.append({
                                "url": link_url,
                                "parent_url": page_url,
                                "reason": "relevant_link_outside_allowed_domains",
                            })
                        continue

                    if score >= 0.4:
                        child_depth = depth + 1
                        if child_depth <= configured_max_depth:
                            visited.add(link_key)
                            queue.append((link_url, child_depth))
                        else:
                            result.urls_skipped += 1

        # At the depth boundary, explicitly report relevant links that could not
        # be traversed. Pagination links are same-depth links, but a depth limit
        # can otherwise prevent the crawler from following them silently.
        if follow_detail and configured_max_depth > 0 and depth >= configured_max_depth:
            for link_info in res.get("links", []):
                link_url = _normalize_url(link_info.get("url", ""))
                if not link_url or _matches_excluded_url(link_url, exclude_url_patterns):
                    continue
                link_key = _url_dedupe_key(link_url)
                if link_key in visited:
                    continue
                score = _classify_link_stage_a(
                    link_url,
                    link_info.get("anchor_text", ""),
                    link_info.get("surrounding_text", ""),
                )
                if score < 0.4:
                    continue
                result.urls_skipped += 1
                result.relevant_urls_skipped += 1
                result.coverage_gaps.append({
                    "url": link_url,
                    "parent_url": page_url,
                    "reason": (
                        "depth_limit_reached"
                        if _is_allowed_domain(link_url, base_domain, allowed_domains)
                        else "relevant_link_outside_allowed_domains"
                    ),
                })
            if discovery_config.get("pagination", False):
                for pag_url in res.get("pagination_links", []):
                    normalized_pag = _normalize_url(pag_url)
                    pag_key = _url_dedupe_key(normalized_pag)
                    if pag_key in visited or _matches_excluded_url(normalized_pag, exclude_url_patterns):
                        continue
                    result.pagination_detected = True
                    result.urls_skipped += 1
                    result.relevant_urls_skipped += 1
                    result.coverage_gaps.append({
                        "url": normalized_pag,
                        "parent_url": page_url,
                        "reason": "pagination_not_followed_at_depth_limit",
                    })

    # Determine completeness
    if not result.truncated:
        # Queue was fully exhausted within the configured discovery boundary.
        # Relevant routes skipped by domain/depth boundaries still leave a gap.
        result.discovery_exhausted = not result.coverage_gaps
        if result.pagination_detected and result.discovery_exhausted:
            result.pagination_complete = True
        else:
            result.pagination_complete = False

    _finalize_collection_status(result, test_limits=test_limits)
    result.records_discovered = len(result.opportunities)

    return result
