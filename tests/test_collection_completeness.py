from core.schemas import CollectionResult
from core.crawl4ai_web import _finalize_collection_status


def test_zero_results_are_negative_only_after_exhausted_unpaginated_discovery():
    result = CollectionResult(
        source_id="empty-unpaginated",
        method="web",
        pages_fetched=1,
        discovery_exhausted=True,
    )

    assert _finalize_collection_status(result) == "NO_ACTIVE_OPPORTUNITIES"


def test_zero_results_with_relevant_skipped_route_are_partial_not_negative():
    result = CollectionResult(
        source_id="external-application-route",
        method="web",
        pages_fetched=1,
        discovery_exhausted=True,
        relevant_urls_skipped=1,
        coverage_gaps=[{
            "url": "https://partner.example/apply",
            "reason": "relevant_link_outside_allowed_domains",
        }],
    )

    assert _finalize_collection_status(result) == "PARTIAL"
    assert result.discovery_exhausted is False
    assert result.pagination_complete is False


def test_zero_results_after_truncation_are_partial_not_negative():
    result = CollectionResult(
        source_id="truncated-source",
        method="web",
        pages_fetched=1,
        truncated=True,
        discovery_exhausted=False,
    )

    assert _finalize_collection_status(result) == "PARTIAL"


def test_opportunities_with_incomplete_pagination_are_partial():
    result = CollectionResult(
        source_id="incomplete-pagination",
        method="web",
        pages_fetched=1,
        opportunities=[{"title": "Example call"}],
        pagination_detected=True,
        pagination_complete=False,
        discovery_exhausted=False,
    )

    assert _finalize_collection_status(result) == "PARTIAL"


def test_opportunities_from_exhausted_unpaginated_source_are_verified():
    result = CollectionResult(
        source_id="complete-unpaginated",
        method="web",
        pages_fetched=1,
        opportunities=[{"title": "Example call"}],
        discovery_exhausted=True,
    )

    assert _finalize_collection_status(result) == "VERIFIED"


def test_relevant_unfollowed_pagination_is_not_reported_as_complete():
    result = CollectionResult(
        source_id="incomplete-paged-source",
        method="web",
        pages_fetched=1,
        pagination_detected=True,
        pagination_complete=False,
        discovery_exhausted=False,
        relevant_urls_skipped=1,
        coverage_gaps=[{
            "url": "https://example.org/page/2",
            "reason": "pagination_not_followed_at_depth_limit",
        }],
    )

    assert _finalize_collection_status(result) == "PARTIAL"
    assert result.pagination_complete is False



def test_web_collector_does_not_overwrite_partial_empty_crawl_with_coverage_gaps():
    from types import SimpleNamespace
    from unittest.mock import patch
    from agents.funding_collector import _collect_web_source

    crawl_result = CollectionResult(
        source_id="XRPL Grants Program",
        method="web",
        final_status="PARTIAL",
        pages_fetched=2,
        records_discovered=0,
        discovery_exhausted=False,
        relevant_urls_skipped=2,
        coverage_gaps=[
            {
                "url": "https://brinc.io/xrpl-program",
                "reason": "relevant_link_outside_allowed_domains",
            }
        ],
    )
    source = SimpleNamespace(
        name="XRPL Grants Program",
        url="https://xrplgrants.org/",
        collection={"allowed_domains": ["xrplgrants.org"], "discovery": {"max_depth": 1}},
        direct_page=False,
    )

    with (
        patch("agents.funding_collector.crawl_funding_source", return_value=crawl_result),
        patch("agents.funding_collector._fetch_status_verification_context", return_value=""),
    ):
        result = _collect_web_source(source, area=None, warnings=[])

    assert result.final_status == "PARTIAL"
    assert result.discovery_exhausted is False
    assert result.relevant_urls_skipped == 2


def test_web_collector_can_report_empty_only_after_exhausted_gap_free_crawl():
    from types import SimpleNamespace
    from unittest.mock import patch
    from agents.funding_collector import _collect_web_source

    crawl_result = CollectionResult(
        source_id="empty-source",
        method="web",
        final_status="NO_ACTIVE_OPPORTUNITIES",
        pages_fetched=1,
        records_discovered=0,
        discovery_exhausted=True,
    )
    source = SimpleNamespace(
        name="empty-source",
        url="https://example.org/grants",
        collection={"allowed_domains": ["example.org"], "discovery": {"max_depth": 1}},
        direct_page=False,
    )

    with (
        patch("agents.funding_collector.crawl_funding_source", return_value=crawl_result),
        patch("agents.funding_collector._fetch_status_verification_context", return_value=""),
    ):
        result = _collect_web_source(source, area=None, warnings=[])

    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"
