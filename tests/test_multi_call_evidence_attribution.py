import json
import pytest
from core.agent_interface import assign_funding_calls, _resolve_funding_call_ids
from core.funding_selection import FundingCallContext

def _mock_ctx(**kwargs):
    base = {
        "funding_body": "body1", "call_id": "call-1", "opening_date": "2026", "deadline": "2026",
        "funding_amount": "100", "currency": "USD", "duration": "12", "eligible_costs": "none",
        "eligibility": "open", "geography": "global", "trl": "1", "research_priorities": [],
        "required_partners": "none", "deliverables": "report", "evaluation_criteria": "none",
        "application_url": "http://example.com/xyz123", "source_url": "http://example.com/xyz123", "research_area": "test", "selection_score": 0.0,
        "opportunity_type": "GRANT", "status": "OPEN", "source_name": "Test",
        "publication_date": "2026", "last_updated": "2026", "selection_mode": "manual", "selection_reasons": []
    }
    base.update(kwargs)
    return FundingCallContext.from_dict(base)

class DummyDoc:
    def __init__(self, title, content):
        self.title = title
        self.content = content
        self.source = "http://example.com"
        self.year = 2026
        self.organization = "TestOrg"

class DummyAnalysis:
    def __init__(self):
        pass

def _get_matched_ids(title, content, contexts):
    doc = DummyDoc(title, content)
    analysis = DummyAnalysis()
    # Call the actual production function
    return assign_funding_calls(title, content, contexts)

def test_stale_document_call_ids_are_recomputed_from_current_evidence():
    call_a = _mock_ctx(funding_call_id="CALL-CURRENT-A", call_title="Current Program Alpha", program_name="Current Program Alpha")
    call_b = _mock_ctx(funding_call_id="CALL-CURRENT-B", call_title="Current Program Bravo", program_name="Current Program Bravo")
    stale_document = {"funding_call_ids": ["CALL-CURRENT-B", "CALL-FROM-OLD-RUN"]}

    ids = _resolve_funding_call_ids(
        stale_document,
        title="Current Program Alpha application evidence",
        content="This evidence explicitly names CALL-CURRENT-A and its application requirements.",
        funding_context=None,
        funding_contexts=[call_a, call_b],
    )

    assert ids == ["CALL-CURRENT-A"]


def test_explicit_call_context_overrides_stale_document_ids():
    call_a = _mock_ctx(funding_call_id="CALL-CURRENT-A", call_title="Current Program Alpha")
    call_b = _mock_ctx(funding_call_id="CALL-CURRENT-B", call_title="Current Program Bravo")

    ids = _resolve_funding_call_ids(
        {"funding_call_ids": ["CALL-CURRENT-B"]},
        title="Evidence mentioning CALL-CURRENT-B",
        content="This document mentions CALL-CURRENT-B but is being collected for the explicit current call.",
        funding_context=call_a,
        funding_contexts=[call_a, call_b],
    )

    assert ids == ["CALL-CURRENT-A"]


def test_large_context_cross_contamination():
    # CASE A: Generic document + 100 contexts -> []
    contexts = []
    for i in range(100):
        contexts.append(_mock_ctx(
            funding_call_id=f"CALL-GENERIC-{i}",
            call_title=f"Generic Title {i}",
            program_name=f"Generic Program {i}"
        ))
        
    title = "ConsenSys / Ethereum / research / stablecoins / security / data"
    content = "We need funding for relevant problems and existing technologies in Web3."
    
    ids = _get_matched_ids(title, content, contexts)
    assert ids == [], f"Contamination occurred! Assigned to {len(ids)} calls."

def test_explicit_attribution():
    # CASE B: Explicit Call ID/URL + 100 contexts -> [target_call_id]
    contexts = []
    for i in range(100):
        contexts.append(_mock_ctx(
            funding_call_id=f"CALL-TEST-{i}",
            call_title=f"Some specific title number {i}",
            program_name=f"Program number {i}"
        ))
        
    title = "A targeted proposal document"
    content = "This document is specifically for CALL-TEST-42 and it mentions some AI research."
    
    ids = _get_matched_ids(title, content, contexts)
    assert ids == ["CALL-TEST-42"]

def test_legitimate_shared_attribution():
    # CASE C: Document genuinely shared by A and B -> [A, B]
    ctx_a = _mock_ctx(funding_call_id="CALL-SHARED-A", call_title="Target Program A Requires Long Title", program_name="Prog A")
    ctx_b = _mock_ctx(funding_call_id="CALL-SHARED-B", call_title="Target Program B Also Has Long Title", program_name="Prog B")
    ctx_c = _mock_ctx(funding_call_id="CALL-IGNORED-C", call_title="Target Program C", program_name="Prog C")
    
    title = "Shared Platform Evidence"
    content = "Supports both CALL-SHARED-A and Target Program B Also Has Long Title."
    
    ids = _get_matched_ids(title, content, [ctx_a, ctx_b, ctx_c])
    assert set(ids) == {"CALL-SHARED-A", "CALL-SHARED-B"}
    assert len(ids) == 2

def make_context(cid: str, title: str, priorities: list[str]) -> FundingCallContext:
    return FundingCallContext(
        funding_call_id=cid,
        funding_body="NSF",
        program_name="NSF",
        call_id="NSF",
        call_title=title,
        status="NSF",
        opening_date="NSF",
        deadline="NSF",
        funding_amount="NSF",
        currency="NSF",
        duration="NSF",
        eligible_costs="NSF",
        eligibility="NSF",
        geography="NSF",
        trl="NSF",
        research_priorities=priorities,
        required_partners="NSF",
        deliverables="NSF",
        evaluation_criteria="NSF",
        application_url="NSF",
        source_url="NSF",
        source_name="NSF",
        publication_date="NSF",
        last_updated="NSF",
        research_area="NSF",
        selection_mode="manual_text",
        selection_score=0.0,
        selection_reasons=[],
    )


def test_relevance_fallback_is_used_when_no_explicit_identity_matches():
    relevant = _mock_ctx(
        funding_call_id="CALL-FALLBACK-A",
        call_title="A funding program for quantum systems",
        program_name="Quantum Device Research Fund",
        research_priorities=["quantum device coherence"],
    )
    unrelated = _mock_ctx(
        funding_call_id="CALL-FALLBACK-B",
        call_title="A fellowship for clinical research",
        program_name="Clinical Trials Fellowship",
        research_priorities=["clinical trials patient outcomes"],
    )

    ids = _get_matched_ids(
        "Evidence on quantum device behavior",
        "The quantum device maintains coherence across repeated measurements.",
        [relevant, unrelated],
    )

    assert ids == ["CALL-FALLBACK-A"]


def test_duplicate_explicit_matches_return_each_funding_call_once():
    context = _mock_ctx(
        funding_call_id="CALL-DEDUP-1",
        call_title="A Specific Research Program With a Long Title",
        program_name="A Specific Research Program",
    )

    ids = _get_matched_ids(
        "A Specific Research Program With a Long Title",
        "This evidence explicitly names CALL-DEDUP-1 and the A Specific Research Program.",
        [context, context],
    )

    assert ids == ["CALL-DEDUP-1"]
