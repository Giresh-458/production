from core.schemas import CollectionResult
from core.crawl4ai_web import _finalize_collection_status


def test_zero_results_are_negative_only_after_exhausted_unpaginated_discovery():
    result = CollectionResult(
        source_id="empty-unpaginated",
        method="web",
        pages_fetched=1,
        discovery_exhausted=True,
        no_active_opportunities_confirmed=True,
    )

    assert _finalize_collection_status(result) == "NO_ACTIVE_OPPORTUNITIES"


def test_exhausted_zero_result_without_explicit_empty_state_is_not_claimed_inactive():
    result = CollectionResult(
        source_id="no-positive-empty-state",
        method="web",
        pages_fetched=1,
        discovery_exhausted=True,
    )

    assert _finalize_collection_status(result) == "PARTIAL"


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
        no_active_opportunities_confirmed=True,
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



def test_funding_agent_output_cannot_keep_no_active_status_with_skipped_routes():
    from agents.funding_agent import _normalize_collection_coverage_status

    result = CollectionResult(
        source_id="solana",
        method="web",
        pages_fetched=1,
        final_status="NO_ACTIVE_OPPORTUNITIES",
        discovery_exhausted=False,
        relevant_urls_skipped=4,
        coverage_gaps=[
            {"url": "https://forms.example/apply", "reason": "relevant_link_outside_allowed_domains"}
        ],
    )

    _normalize_collection_coverage_status(result)

    assert result.final_status == "PARTIAL"
    assert result.discovery_exhausted is False
    assert result.pagination_complete is False


def test_funding_agent_output_downgrades_verified_status_when_coverage_gaps_exist():
    from agents.funding_agent import _normalize_collection_coverage_status

    result = CollectionResult(
        source_id="source-with-gap",
        method="web",
        pages_fetched=1,
        opportunities=[{"title": "Example"}],
        final_status="VERIFIED",
        discovery_exhausted=True,
        coverage_gaps=[{"url": "https://partner.example/apply", "reason": "external_route"}],
    )

    _normalize_collection_coverage_status(result)

    assert result.final_status == "PARTIAL"
    assert result.discovery_exhausted is False
    assert result.pagination_complete is False


def test_funding_agent_output_downgrades_empty_source_when_discovery_not_exhausted():
    from agents.funding_agent import _normalize_collection_coverage_status

    result = CollectionResult(
        source_id="not-exhausted",
        method="web",
        pages_fetched=1,
        final_status="NO_ACTIVE_OPPORTUNITIES",
        discovery_exhausted=False,
    )

    _normalize_collection_coverage_status(result)

    assert result.final_status == "PARTIAL"

def test_external_pagination_is_recorded_as_coverage_gap(monkeypatch):
    import core.crawl4ai_web as crawler

    external_page = "https://partner.example/page/2"

    monkeypatch.setattr(
        crawler, "_get_robots_policy",
        lambda url: (True, 0, ""),
    )
    monkeypatch.setattr(
        crawler, "_wait_for_host_slot",
        lambda *args: True,
    )
    monkeypatch.setattr(
        crawler, "_fetch_sync",
        lambda url: {
            "success": True,
            "url": url,
            "status_code": 200,
            "markdown": "Welcome to our website",
            "html": "",
            "title": "Home",
            "links": [],
            "pagination_links": [external_page],
        },
    )

    result = crawler.crawl_funding_source(
        source_id="external-pagination-test",
        url="https://example.org/grants",
        discovery_config={
            "max_depth": 1,
            "max_pages": 5,
            "pagination": True,
            "follow_detail_pages": False,
            "respect_robots_txt": True,
        },
        allowed_domains=[],
    )

    assert result.final_status == "PARTIAL"
    assert result.pagination_detected is True
    assert result.pagination_complete is False
    assert result.discovery_exhausted is False
    assert result.relevant_urls_skipped == 1
    assert any(
        gap["url"] == external_page
        and gap["reason"] == "pagination_outside_allowed_domains"
        for gap in result.coverage_gaps
    )




def test_funding_problem_extracts_concise_source_grounded_threat_sentence():
    from core.problem_extraction import extract_problem_intelligence

    content = (
        "## Scope\n"
        "The evolution of science funding priorities and data access policies represents "
        "a major threat to evidence-based decision making. The programme aims to improve "
        "long-term data access and interoperability.\n"
        "[Apply here](https://example.org/apply)"
    )
    result = extract_problem_intelligence(
        content, "Climate data call", {}, allow_llm_refinement=False,
        research_area="ESG", agent_name="funding",
    )

    problem = result["selected_problem"]
    assert problem is not None
    assert "major threat to evidence-based decision making" in problem
    assert len(problem) < 300
    assert "https://" not in problem
    assert "[Apply here]" not in problem
    assert problem in result["problem_evidence"][0]


def test_funding_problem_does_not_promote_imperative_instruction():
    from core.problem_extraction import extract_problem_intelligence

    content = (
        "Example implementation: Block, limit and/or track unwanted addresses "
        "from entering an application. Submit a proposal with milestones and deliverables."
    )
    result = extract_problem_intelligence(
        content, "Development grant", {}, allow_llm_refinement=False,
        research_area="DID", agent_name="funding",
    )

    assert result["selected_problem"] is None
    assert result["problem_type"] == "unresolved"
    assert result["problem_evidence"] == []


def test_repeated_coverage_gap_urls_are_deduplicated_before_status_reporting():
    result = CollectionResult(
        source_id="repeated-frontier",
        method="web",
        pages_fetched=1,
        relevant_urls_skipped=3,
        coverage_gaps=[
            {"url": "https://example.org/call/1?utm_source=a", "parent_url": "https://example.org/", "reason": "depth_limit_reached"},
            {"url": "https://example.org/call/1?utm_source=b", "parent_url": "https://example.org/list", "reason": "depth_limit_reached"},
            {"url": "https://example.org/call/2", "parent_url": "https://example.org/", "reason": "depth_limit_reached"},
        ],
    )

    assert _finalize_collection_status(result) == "PARTIAL"
    assert len(result.coverage_gaps) == 2
    assert result.relevant_urls_skipped == 2


def test_funding_agent_serializes_explicit_empty_state_confirmation(tmp_path):
    from unittest.mock import patch
    from agents.funding_agent import load_sources, run_agent

    from pathlib import Path

    source = load_sources(Path("sources/funding_sources.yaml"))
    result = CollectionResult(
        source_id=source.name,
        method="web",
        pages_fetched=1,
        discovery_exhausted=True,
        no_active_opportunities_confirmed=True,
        final_status="NO_ACTIVE_OPPORTUNITIES",
    )
    output_dir = tmp_path / "out"
    db_path = tmp_path / "funding_memory.db"
    checkpoint_path = tmp_path / "checkpoint.pkl"

    with patch("agents.funding_collector.collect_auto_sources", return_value=iter([result])):
        response = run_agent(
            "configured_scan",
            None,
            {
                "sources_path": "sources/funding_sources.yaml",
                "posts_per_source": 1,
                "output_dir": str(output_dir),
                "db_path": str(db_path),
                "checkpoint_path": str(checkpoint_path),
                "analysis_mode": "collect_only",
                "is_test_mode": True,
                "force_refresh": True,
                "run_id": "empty-state-serialization-test",
            },
        )

    assert response["collection_results"][0]["final_status"] == "NO_ACTIVE_OPPORTUNITIES"
    assert response["collection_results"][0]["no_active_opportunities_confirmed"] is True

