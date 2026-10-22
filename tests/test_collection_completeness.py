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
