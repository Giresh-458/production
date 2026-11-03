from __future__ import annotations

from core import crawl4ai_web


XRPL_SUBMIT_URL = "https://submit.xrplgrants.org/submit"
XRPL_EMPTY_STATE = "There are presently no open calls for applications."


def test_xrpl_application_manager_waits_for_rendered_status_text():
    options = crawl4ai_web._xrpl_application_manager_wait_options(XRPL_SUBMIT_URL)

    assert "wait_for" in options
    assert "there are presently no open calls for applications" in options["wait_for"]
    assert "open call" in options["wait_for"]
    assert options["delay_before_return_html"] == 0.75
    assert crawl4ai_web._xrpl_application_manager_wait_options(
        "https://xrplgrants.org/"
    ) == {}


def test_xrpl_empty_state_detector_is_scoped_to_authoritative_application_route():
    assert crawl4ai_web._reports_no_active_funding(
        XRPL_SUBMIT_URL,
        "RippleX Ecosystem Programs",
        XRPL_EMPTY_STATE,
    )
    assert not crawl4ai_web._reports_no_active_funding(
        "https://xrplgrants.org/",
        "XRPL Grants Program",
        XRPL_EMPTY_STATE,
    )
    assert not crawl4ai_web._reports_no_active_funding(
        "https://submit.xrplgrants.org/other",
        "RippleX Ecosystem Programs",
        XRPL_EMPTY_STATE,
    )


def test_xrpl_empty_state_is_not_emitted_as_a_funding_call(monkeypatch):
    monkeypatch.setattr(
        crawl4ai_web, "_get_robots_policy", lambda _url: (True, 0.0, None)
    )
    monkeypatch.setattr(
        crawl4ai_web, "_wait_for_host_slot", lambda _url, _delay: True
    )

    def fetch(url: str) -> dict:
        if url == XRPL_SUBMIT_URL:
            title = "RippleX Ecosystem Programs"
            body = (
                "RippleX Grants and funding application programme. "
                + XRPL_EMPTY_STATE
            )
        else:
            title = "XRPL Grants"
            body = "Official XRP Ledger ecosystem programme information."
        return {
            "html": f"<html><head><title>{title}</title></head><body><main>{body}</main></body></html>",
            "markdown": body,
            "success": True,
            "error_message": None,
            "status_code": 200,
            "redirected_status_code": None,
            "url": url,
            "requested_url": url,
            "redirected_url": None,
            "title": title,
            "links": [],
            "pagination_links": [],
        }

    monkeypatch.setattr(crawl4ai_web, "_fetch_sync", fetch)
    result = crawl4ai_web.crawl_funding_source(
        "XRPL Grants Program",
        "https://xrplgrants.org/",
        {
            "start_urls": ["https://xrplgrants.org/", XRPL_SUBMIT_URL],
            "max_depth": 0,
            "max_pages": 2,
            "pagination": False,
        },
        ["xrplgrants.org", "submit.xrplgrants.org"],
    )

    assert result.pages_fetched == 2
    assert result.discovery_exhausted is True
    assert result.no_active_opportunities_confirmed is True
    assert result.opportunities == []
    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"
