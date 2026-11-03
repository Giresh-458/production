from types import SimpleNamespace

from agents import funding_collector
from agents.funding_agent import _update_source_verification_flags
from core import crawl4ai_web
from core.schemas import CollectionResult


SOLANA_URL = "https://solana.org/grants-funding"
SOLANA_TEXT = (
    "All funding applicants for Solana Foundation standard and convertible grants "
    "are required to fill out the application here. "
    "All applications are reviewed on a rolling basis."
)
XRPL_ROOT = "https://xrplgrants.org/"
XRPL_EMPTY = "Applications are available by invitation only."


def test_solana_rolling_program_signal_is_scoped():
    assert crawl4ai_web._reports_active_funding_program(SOLANA_URL, "Grants and Funding", SOLANA_TEXT)
    assert not crawl4ai_web._reports_active_funding_program("https://example.org/grants-funding", "Grants and Funding", SOLANA_TEXT)
    assert not crawl4ai_web._reports_active_funding_program(SOLANA_URL, "Grants and Funding", "General funding information.")


def test_solana_rolling_program_is_verified_after_complete_crawl(monkeypatch):
    monkeypatch.setattr(crawl4ai_web, "_get_robots_policy", lambda _url: (True, 0.0, None))
    monkeypatch.setattr(crawl4ai_web, "_wait_for_host_slot", lambda _url, _delay: True)

    def fetch(url):
        return {
            "html": f"<html><head><title>Grants and Funding</title></head><body><main>{SOLANA_TEXT}</main></body></html>",
            "markdown": SOLANA_TEXT, "success": True, "error_message": None,
            "status_code": 200, "redirected_status_code": None, "url": url,
            "requested_url": url, "redirected_url": None, "title": "Grants and Funding",
            "links": [], "pagination_links": [],
        }

    monkeypatch.setattr(crawl4ai_web, "_fetch_sync", fetch)
    result = crawl4ai_web.crawl_funding_source(
        "Solana Grants and Funding", SOLANA_URL,
        {"start_urls": [SOLANA_URL], "max_depth": 0, "max_pages": 1, "pagination": False},
        ["solana.org"],
    )
    assert result.pages_fetched == 1
    assert result.discovery_exhausted is True
    assert result.active_program_confirmed is True
    assert result.no_active_opportunities_confirmed is False
    assert result.final_status == "VERIFIED"
    assert result.opportunities == []


def test_xrpl_invitation_only_is_scoped_to_canonical_root():
    assert crawl4ai_web._reports_no_active_funding(XRPL_ROOT, "XRPL Grants", XRPL_EMPTY)
    assert not crawl4ai_web._reports_no_active_funding("https://xrplgrants.org/awardees", "XRPL Grants", XRPL_EMPTY)
    assert not crawl4ai_web._reports_no_active_funding("https://example.org/", "XRPL Grants", XRPL_EMPTY)


def test_xrpl_empty_state_survives_collector_normalization(monkeypatch):
    crawled = CollectionResult(
        source_id="XRPL Grants Program", method="web", pages_fetched=2,
        discovery_exhausted=True, no_active_opportunities_confirmed=True,
        final_status="NO_ACTIVE_OPPORTUNITIES",
    )
    source = SimpleNamespace(
        name="XRPL Grants Program", url=XRPL_ROOT, direct_page=False,
        collection={"allowed_domains": [], "discovery": {}},
    )
    monkeypatch.setattr(funding_collector, "crawl_funding_source", lambda *args, **kwargs: crawled)
    monkeypatch.setattr(funding_collector, "_fetch_status_verification_context", lambda *_args: "")
    result = funding_collector._collect_web_source(source, area=None, warnings=None)
    assert result.no_active_opportunities_confirmed is True
    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"
    assert result.opportunities == []


def test_active_program_verification_requires_complete_coverage():
    result = CollectionResult(
        source_id="Solana Grants and Funding", method="web", pages_fetched=1,
        discovery_exhausted=True, active_program_confirmed=True, final_status="VERIFIED",
    )
    _update_source_verification_flags(result)
    assert result.evidence_verified is True
    assert result.currentness_verified is True

    incomplete = CollectionResult(
        source_id="Solana Grants and Funding", method="web", pages_fetched=1,
        discovery_exhausted=False, active_program_confirmed=True, final_status="PARTIAL",
    )
    _update_source_verification_flags(incomplete)
    assert incomplete.evidence_verified is False
    assert incomplete.currentness_verified is False
