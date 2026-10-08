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
        title="Targeted STEM Infusion Projects",
        source="http://test.com",
        source_type="Manual",
        content="Targeted STEM Infusion Projects could, for example, enhance academic infrastructure by systematically adding traditional knowledge to the scope or content of a STEM course, updating curricula, modernizing laboratory research equipment, developing and delivering professional development for K-12 STEM educators, or improving the computational infrastructure. The objective of this strand is to expand STEM degrees or significantly enhance instructional approaches. This is a research grant.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    rc = opp.research_context
    assert rc is not None
    assert len(rc["selected_domains"]) == 0
    themes = rc["technology_themes"]
    
    assert "computational infrastructure" in themes
    assert any(t in themes for t in ["laboratory research equipment", "academic infrastructure", "stem course", "stem educators"])
    
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

def test_analyze_document_general_administrative_phrases():
    doc = FundingDocument(
        organization="TestOrg",
        title="Administrative Grants",
        source="http://test.com",
        source_type="Manual",
        content="Please submit your grant application to the tribal colleges and universities program. The tribal colleges and universities program welcomes your grant application. This is a research grant.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    rc = opp.research_context
    assert len(rc["technology_themes"]) == 0

def test_analyze_document_general_machine_learning():
    doc = FundingDocument(
        organization="TestOrg",
        title="ML Call",
        source="http://test.com",
        source_type="Manual",
        content="This research funding supports machine learning for scientific applications and intelligent decision systems.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    themes = opp.research_context["technology_themes"]
    assert "machine learning" in themes

def test_analyze_document_general_robotics():
    doc = FundingDocument(
        organization="TestOrg",
        title="Robotics Call",
        source="http://test.com",
        source_type="Manual",
        content="The call supports robotics and autonomous systems research for advanced manufacturing.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    themes = opp.research_context["technology_themes"]
    assert "robotics" in themes
    assert any("autonomous systems" in t for t in themes)

def test_analyze_document_general_computer_vision():
    doc = FundingDocument(
        organization="TestOrg",
        title="CV Call",
        source="http://test.com",
        source_type="Manual",
        content="Research priorities include computer vision and image understanding for industrial inspection.",
        year=2026
    )
    opp = analyze_document(doc)
    assert opp.focus_area == "General"
    themes = opp.research_context["technology_themes"]
    assert "computer vision" in themes
    assert not any("research priorities" in t for t in themes)
