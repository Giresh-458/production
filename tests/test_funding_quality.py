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


def test_single_article_pdf_title_is_not_a_valid_funding_identity():
    from agents.funding_agent import _is_generic_funding_title

    assert _is_generic_funding_title("the") is True
    assert _is_generic_funding_title("SME Market Expansion Open Call 2027") is False

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


def test_unverified_sbir_mirror_topic_is_filtered_not_currentness_verified(monkeypatch):
    from agents.funding_collector import _collect_web_source
    from agents.funding_agent import FundingSource, _update_source_verification_flags
    from core.schemas import CollectionResult
    import agents.funding_collector as collector

    source = FundingSource(
        name="NSF SBIR Trusted Identity",
        url="https://www.sbir.gov/topics/1012406",
        tier="primary",
        focus="NSF SBIR Trusted Identity",
        research_domains=["DID"],
        collection={
            "mode": "web",
            "engine": "crawl4ai",
            "allowed_domains": ["sbir.gov", "www.sbir.gov"],
            "discovery": {"start_urls": ["https://www.sbir.gov/topics/1012406"], "max_depth": 0},
        },
        direct_page=True,
    )
    result = CollectionResult(
        source_id=source.name,
        method="web",
        final_status="VERIFIED",
        pages_fetched=1,
        records_discovered=1,
        discovery_exhausted=True,
        opportunities=[{
            "type": "html",
            "url": source.url,
            "title": "Topic",
            "content": (
                "## Trusted Identity and Identity Management\n"
                "Solicitation Number: NSF 26-510. Solicitation Status: Open. "
                "These topics are copies and are not necessarily the latest and most up-to-date."
            ),
        }],
    )
    monkeypatch.setattr(collector, "crawl_funding_source", lambda *args, **kwargs: result)
    monkeypatch.setattr(collector, "_fetch_linked_official_sbir_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(collector, "_fetch_currentness_verification_context", lambda *args, **kwargs: "")

    normalized = collector._collect_web_source(source, None, warnings=[])

    assert normalized.records_discovered == 1
    assert normalized.records_valid == 0
    assert normalized.records_filtered == 1
    assert normalized.records_filtered_by_reason == {"unverified_sbir_mirror_topic": 1}
    assert normalized.opportunities == []
    # With no independently verified topic and no publisher-confirmed empty state,
    # source-level currentness remains unverified and production acceptance stays blocked.
    _update_source_verification_flags(normalized)
    assert normalized.currentness_verified is False
    assert normalized.evidence_verified is False


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


def test_esgf_climate_projection_call_is_not_misclassified_as_esg():
    from core.schemas import rank_research_areas

    source_text = """
    Sustainability and resilience of climate projections and related data records
    for European and global science and policy.
    The resilience of the Earth System Grid Federation (ESGF) is strengthened.
    Secure access to climate observations and projections for climate science.
    Improve data infrastructure, interoperability, and long-term operations.
    """
    ranked = rank_research_areas(source_text)
    esg = next(item for item in ranked if item["domain"] == "ESG")

    # "ESGF", climate science, and generic sustainability language are not
    # sufficient evidence for the separate ESG / carbon-accounting RIF domain.
    assert esg["score"] == 0
    assert esg["raw_score"] == 0
    assert esg["matched_terms"] == []


def test_direct_esg_carbon_or_mrv_evidence_still_matches_esg_domain():
    from core.schemas import rank_research_areas

    source_text = """
    The call supports ESG reporting, carbon emissions accounting, and
    measurement, reporting and verification (MRV) workflows.
    """
    ranked = rank_research_areas(source_text)
    esg = next(item for item in ranked if item["domain"] == "ESG")

    assert esg["score"] > 0
    assert esg["raw_score"] > 0
    assert any(term in esg["matched_terms"] for term in ("esg", "carbon", "emissions", "mrv"))


def test_general_research_questions_do_not_inject_esg_carbon_or_mrv_themes():
    from agents.funding_agent import generate_research_mission

    context = generate_research_mission(
        ["Climate Projections", "Climate Data Infrastructure"],
        [],
        "Sustainability and resilience of climate projections",
        "General",
        research_intent="research",
        research_intent_evidence=["Climate projections and scientific data are in scope."],
    )
    questions = " ".join(context["investigation_questions"])
    mission = " ".join(context["inferred_challenges"])

    assert "Climate Projections" in questions
    assert "Climate Data Infrastructure" in questions
    assert "carbon" not in questions.casefold()
    assert "mrv" not in questions.casefold()
    assert "ESG" not in questions
    assert "commercialization" not in mission
    assert "adoption" not in mission


def test_official_esgf_climate_call_stays_unscoped_and_questions_follow_source_topics():
    import re
    from agents.funding_agent import FundingDocument, analyze_document

    document = FundingDocument(
        organization="EU Open and Forthcoming Funding Calls",
        title="Sustainability and resilience of climate projections and related data records for European and global science and policy",
        source="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-BRIDGING-2027-04-08",
        source_type="Official EU Funding & Tenders Search API",
        year=2026,
        content="""
        Expected Outcome: The EU climate data and infrastructure sovereignty is
        strengthened, securing access to essential observation and modelling data
        for European research community. The resilience of the Earth System Grid
        Federation (ESGF) is strengthened in Europe and globally.
        Scope: The evolution of the geopolitical situation and changes in science
        funding priorities and data access policies represent a major threat to
        evidence-based decision making. The continuity of production, maintenance
        and open access to climate data, including observations and projections,
        is fundamental for European and global climate science and policy action.
        Improve architecture and interoperability, governance, service quality,
        operational aspects, storage capacity, and long-term data availability.
        """,
    )

    analysis = analyze_document(document)
    questions = " ".join((analysis.research_context or {}).get("investigation_questions", []))

    assert analysis.focus_area == "General"
    assert questions
    assert not re.search(r"\bESG\b|\bcarbon\b|\bMRV\b", questions, flags=re.IGNORECASE)
    assert "climate" in questions.casefold() or "data" in questions.casefold()
