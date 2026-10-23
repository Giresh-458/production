from core.funding_quality import assess_batch_acceptance


def test_acceptance_requires_successful_scan_and_actionable_call():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "complete",
            "final_status": "VERIFIED",
            "discovery_exhausted": True,
            "pagination_detected": True,
            "pagination_complete": True,
            "evidence_verified": True,
            "currentness_verified": True,
        }],
    )
    assert result["status"] == "ACCEPTED"
    assert result["accepted"] is True
    assert result["blockers"] == []


def test_selected_manual_review_flags_block_batch_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        selected_manual_review_flags=[{
            "title": "Example call",
            "flags": ["direct_application_url_missing_manual_review"],
        }],
    )
    assert result["status"] == "BLOCKED"
    assert any(
        blocker["code"] == "selected_record_requires_manual_review"
        for blocker in result["blockers"]
    )


def test_verified_source_with_incomplete_coverage_is_blocked():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "mislabelled",
            "final_status": "VERIFIED",
            "coverage_gaps_count": 2,
            "discovery_exhausted": False,
            "pagination_detected": True,
            "pagination_complete": False,
        }],
    )
    assert result["status"] == "BLOCKED"
    blocker = next(
        item for item in result["blockers"]
        if item.get("source_id") == "mislabelled"
    )
    assert blocker["code"] == "source_coverage_incomplete"
    assert "coverage gap" in blocker["message"]
    assert "pagination incomplete" in blocker["message"]


def test_partial_source_and_fetch_failures_are_explicit_blockers():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "partial-source",
            "final_status": "PARTIAL",
            "pages_failed": 1,
            "relevant_urls_skipped": 3,
            "errors": [{"error": "timeout"}],
            "discovery_exhausted": False,
        }],
    )
    assert result["status"] == "BLOCKED"
    assert any(
        item.get("source_id") == "partial-source"
        and "relevant URL(s) skipped" in item["message"]
        and "fetch failures" in item["message"]
        for item in result["blockers"]
    )


def test_no_selected_call_blocks_even_when_scan_itself_succeeded():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=0,
    )
    assert result["status"] == "BLOCKED"
    assert any(item["code"] == "no_actionable_calls" for item in result["blockers"])



def test_eu_source_searches_all_seven_configured_research_domains():
    from pathlib import Path
    import yaml

    config = yaml.safe_load(Path("sources/funding_sources.yaml").read_text(encoding="utf-8"))
    source = next(
        item for item in config["sources"]["tier_3"]
        if item["name"] == "EU Open and Forthcoming Funding Calls"
    )
    api = source["collection"]["api"]
    queries = {query.casefold() for query in api["search_texts"]}

    assert "tokenized real-world assets" in queries
    assert "carbon credit" in queries
    assert "mrv emissions" in queries
    assert "zero-knowledge" in queries
    assert "internet of vehicles" in queries
    assert "decentralized identity" in queries
    assert "physical infrastructure" in queries
    assert "maximal extractable value" in queries
    assert "stablecoin payments" in queries
    assert source["collection"]["limits"]["max_pages"] >= len(queries)



def test_unverified_source_evidence_or_currentness_blocks_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "unverified",
            "final_status": "VERIFIED",
            "discovery_exhausted": True,
            "pagination_detected": True,
            "pagination_complete": True,
            "evidence_verified": False,
            "currentness_verified": False,
        }],
    )
    assert result["status"] == "BLOCKED"
    blocker = next(
        item for item in result["blockers"]
        if item.get("source_id") == "unverified"
    )
    assert "evidence not verified" in blocker["message"]
    assert "currentness not verified" in blocker["message"]
