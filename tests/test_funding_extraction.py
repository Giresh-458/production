from agents.funding_agent import _extract_funding_amount

def test_extract_funding_amount_valid():
    text = "We will award a grant of $7M for the project. Apply now."
    assert "$7M" in _extract_funding_amount(text)

    text2 = "The maximum award is up to EUR2,500,000 per project for startups."
    assert "2,500,000" in _extract_funding_amount(text2)

def test_extract_funding_amount_invalid():
    text = "Our platform has 7B users.\nAnnual revenue was $1M.\nThe company is not offering any grants."
    assert _extract_funding_amount(text) is None

    text2 = "We raised $2158B in a Series X funding round."
    assert _extract_funding_amount(text2) is None

from agents.funding_agent import analyze_document, FundingDocument

def test_analyze_document_general_fallback():
    doc = FundingDocument(
        organization="TestOrg",
        title="Testing Call",
        source="http://test.com",
        source_type="Manual",
        content="We provide a research grant for computational infrastructure and laboratory equipment. Our research focus is computational infrastructure. We also care about laboratory equipment.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    rc = opp.research_context
    assert rc is not None
    assert len(rc["selected_domains"]) == 0
    themes = rc["technology_themes"]
    assert "computational infrastructure" in themes
    assert "laboratory equipment" in themes
    
    questions = rc["investigation_questions"]
    assert any("computational infrastructure" in q for q in questions)
    assert not any("relevant technologies" in q for q in questions)

def test_analyze_document_general_negative():
    doc = FundingDocument(
        organization="TestOrg",
        title="Generic Funding Call",
        source="http://test.com",
        source_type="Manual",
        content="We provide general research funding. We offer a grant program for researchers. Apply for funding today.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    rc = opp.research_context
    assert rc is not None
    assert len(rc["technology_themes"]) == 0
    
    questions = rc["investigation_questions"]
    assert any("relevant technologies" in q for q in questions)
