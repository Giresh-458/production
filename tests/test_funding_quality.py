from core.funding_quality import assess_batch_acceptance


def test_acceptance_requires_successful_scan_and_actionable_call():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        record_accounting={
            "reconciled": True,
            "unaccounted_source_to_documents_delta": 0,
            "unaccounted_documents_to_outputs_delta": 0,
        },
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
        and "fetch/processing failures" in item["message"]
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



def test_missing_configured_source_summary_blocks_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        expected_source_ids=["source-a", "source-b"],
        source_summary=[{
            "source_id": "source-a",
            "final_status": "VERIFIED",
            "discovery_exhausted": True,
            "evidence_verified": True,
            "currentness_verified": True,
        }],
    )
    assert result["status"] == "BLOCKED"
    assert any(
        item["code"] == "source_coverage_telemetry_missing"
        and item.get("source_id") == "source-b"
        for item in result["blockers"]
    )


def test_unknown_source_status_blocks_even_when_verification_flags_are_true():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "unknown-source",
            "final_status": "UNKNOWN",
            "discovery_exhausted": True,
            "evidence_verified": True,
            "currentness_verified": True,
        }],
    )
    assert result["status"] == "BLOCKED"
    blocker = next(
        item for item in result["blockers"]
        if item.get("source_id") == "unknown-source"
    )
    assert "source status unknown" in blocker["message"]


def test_non_opportunity_generic_record_does_not_invalidate_source_flags():
    from types import SimpleNamespace
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    call_document = SimpleNamespace(
        source="https://example.org/call",
        title="Example call",
        content="Applications open 1 October 2026. Deadline 1 December 2026.",
    )
    call_analysis = SimpleNamespace(
        program_name="Example call",
        status="OPEN_CALL",
        evidence={"status": "Applications open", "deadline": "1 December 2026"},
    )
    support_document = SimpleNamespace(
        source="https://example.org/files/call-manual.pdf",
        title="the",
        content="Call manual. Application closes 1 December 2026.",
    )
    support_analysis = SimpleNamespace(
        program_name="the",
        status="OPEN_CALL",
        evidence={},
    )
    result = CollectionResult(
        source_id="source-with-supporting-document",
        method="web",
        final_status="VERIFIED",
        pages_fetched=1,
        detail_pages_fetched=1,
        documents_fetched=1,
        records_valid=2,
        discovery_exhausted=True,
        opportunities=[
            (call_document, call_analysis),
            (support_document, support_analysis),
        ],
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is True
    assert result.currentness_verified is True


def test_source_verification_flags_require_explicit_evidence_and_complete_acquisition():
    from types import SimpleNamespace
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    document = SimpleNamespace(
        source="https://example.org/current-call",
        content="Applications are open. Deadline: 31 December 2027.",
    )
    analysis = SimpleNamespace(
        status="OPEN_CALL",
        evidence={
            "status": "Applications are open.",
            "deadline": "Deadline: 31 December 2027.",
            "eligibility": "Eligible research organisations may apply.",
        },
    )
    result = CollectionResult(
        source_id="complete-source",
        method="web",
        final_status="VERIFIED",
        pages_fetched=1,
        records_valid=1,
        discovery_exhausted=True,
        opportunities=[(document, analysis)],
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is True
    assert result.currentness_verified is True


def test_partial_source_can_have_record_evidence_but_not_verified_currentness():
    from types import SimpleNamespace
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    document = SimpleNamespace(
        source="https://example.org/current-call",
        content="Applications are open. Deadline: 31 December 2027.",
    )
    analysis = SimpleNamespace(
        status="OPEN_CALL",
        evidence={"status": "Applications are open.", "deadline": "Deadline: 31 December 2027."},
    )
    result = CollectionResult(
        source_id="partial-source",
        method="web",
        final_status="PARTIAL",
        pages_fetched=1,
        records_valid=1,
        discovery_exhausted=False,
        relevant_urls_skipped=1,
        coverage_gaps=[{"url": "https://partner.example/apply", "reason": "robots_policy_unverified"}],
        opportunities=[(document, analysis)],
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is True
    assert result.currentness_verified is False


def test_empty_source_is_verified_only_with_explicit_empty_state_and_exhausted_discovery():
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    result = CollectionResult(
        source_id="explicitly-empty",
        method="web",
        final_status="NO_ACTIVE_OPPORTUNITIES",
        pages_fetched=1,
        discovery_exhausted=True,
        no_active_opportunities_confirmed=True,
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is True
    assert result.currentness_verified is True


def test_unknown_record_status_does_not_verify_source_currentness():
    from types import SimpleNamespace
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    document = SimpleNamespace(
        source="https://example.org/call",
        content="Research eligibility details are published.",
    )
    analysis = SimpleNamespace(
        status="UNKNOWN",
        evidence={"eligibility": "Research organisations may apply."},
    )
    result = CollectionResult(
        source_id="unknown-status",
        method="web",
        final_status="VERIFIED",
        pages_fetched=1,
        records_valid=1,
        discovery_exhausted=True,
        opportunities=[(document, analysis)],
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is True
    assert result.currentness_verified is False


def test_missing_record_accounting_blocks_batch_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        source_summary=[{
            "source_id": "complete",
            "final_status": "VERIFIED",
            "discovery_exhausted": True,
            "evidence_verified": True,
            "currentness_verified": True,
        }],
    )
    assert result["status"] == "BLOCKED"
    assert any(item["code"] == "record_accounting_missing" for item in result["blockers"])


def test_unreconciled_record_accounting_blocks_batch_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        record_accounting={
            "reconciled": False,
            "unaccounted_source_to_documents_delta": 7,
            "unaccounted_documents_to_outputs_delta": 0,
        },
    )
    assert result["status"] == "BLOCKED"
    blocker = next(item for item in result["blockers"] if item["code"] == "record_accounting_unreconciled")
    assert "source-to-documents delta=7" in blocker["message"]


def test_record_level_processing_failures_block_source_acceptance():
    result = assess_batch_acceptance(
        run_status="success",
        raw_records_count=10,
        selected_actionable_count=1,
        record_accounting={
            "reconciled": True,
            "unaccounted_source_to_documents_delta": 0,
            "unaccounted_documents_to_outputs_delta": 0,
        },
        source_summary=[{
            "source_id": "record-failures",
            "final_status": "VERIFIED",
            "records_failed": 2,
            "discovery_exhausted": True,
            "evidence_verified": True,
            "currentness_verified": True,
        }],
    )
    assert result["status"] == "BLOCKED"
    blocker = next(
        item for item in result["blockers"]
        if item.get("source_id") == "record-failures"
    )
    assert "records=2" in blocker["message"]


def test_empty_source_without_a_successful_fetch_is_not_verified():
    from core.schemas import CollectionResult
    from agents.funding_agent import _update_source_verification_flags

    result = CollectionResult(
        source_id="empty-without-fetch",
        method="web",
        final_status="NO_ACTIVE_OPPORTUNITIES",
        discovery_exhausted=True,
        no_active_opportunities_confirmed=True,
    )

    _update_source_verification_flags(result)

    assert result.evidence_verified is False
    assert result.currentness_verified is False
