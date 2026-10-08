from core.problem_extraction import extract_problem_intelligence
from core.mission_gate import relevance
from core.funding_selection import FundingCallContext


def test_background_sentence_is_not_a_problem():
    r = extract_problem_intelligence("One of the key concepts of Construction 4.0 is cyber-physical systems.", "x", {}, allow_llm_refinement=False)
    assert r["selected_problem"] is None
    assert not r["problem_candidates"]


def test_real_limitation_is_problem():
    text = "Existing approaches cannot reliably maintain service availability under intermittent connectivity and this remains unresolved in deployed environments."
    r = extract_problem_intelligence(text, "x", {}, allow_llm_refinement=False)
    assert r["selected_problem"]
    assert r["problem_confidence"] in {"Medium", "High"}


def _ctx():
    return FundingCallContext(
        funding_call_id="x", funding_body="Ethereum", program_name="University Program", call_id="x", call_title="Road to Devcon 8 India - University Program",
        status="OPEN", opening_date=None, deadline="2026-09-30", funding_amount="$500", currency="USD", duration=None, eligible_costs=None,
        eligibility="Indian university academic audience", geography="India", trl=None, research_priorities=["Ethereum", "Devcon"], required_partners=None,
        deliverables="public event and outcomes report", evaluation_criteria=None, application_url="https://example.org", source_url="https://example.org",
        source_name="Ethereum", publication_date=None, last_updated=None, research_area=None, selection_mode="test", selection_score=1.0, selection_reasons=[]
    )


def test_mission_gate_rejects_unrelated_document():
    g = relevance({"title":"Bengal Delta geology", "problem_statement":"lithologic data and sediment", "context_summary":"geology study", "keywords":[]}, _ctx())
    assert not g["passed"]
