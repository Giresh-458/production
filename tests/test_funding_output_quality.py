"""Regression tests for evidence-grounded funding output quality."""

from agents.funding_agent import (
    FundingDocument,
    FundingOpportunity,
    _extract_currency,
    _extract_funding_amount,
    _extract_general_themes,
    analyze_document,
    extract_funding_problem_intelligence,
)


def _document(content: str, title: str = "Test Funding Call") -> FundingDocument:
    return FundingDocument(
        organization="Test Foundation",
        title=title,
        source="https://example.org/funding-call",
        source_type="Manual",
        content=content,
        year=2026,
    )


def _analysis(document: FundingDocument) -> FundingOpportunity:
    return FundingOpportunity(
        funding_body="Test Foundation",
        program_name=document.title,
        call_id=None,
        status="OPEN_CALL",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography=None,
        trl=None,
        research_priorities=[],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        opportunity_type="FUNDING_CALL",
        focus_area="General",
        keywords=[],
        research_context={},
        source_method="deterministic_extraction",
        source_url=document.source,
        source_title=document.title,
        retrieved_at="2026-10-23T00:00:00+00:00",
        published_at=None,
        last_verified_at="2026-10-23T00:00:00+00:00",
        evidence={},
    )


def test_funding_problem_signal_is_concise_and_evidence_backed():
    content = (
        "About the programme.\n\n"
        "The current verification architecture cannot process high-volume "
        "transactions within the required latency.\n\n"
        "Applications are accepted online."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is not None
    assert "cannot process high-volume transactions" in intelligence["selected_problem"]
    assert intelligence["selected_problem"] in intelligence["problem_evidence"][0]
    assert intelligence["selected_problem"] != content


def test_funding_problem_signal_stays_unresolved_for_boilerplate():
    content = (
        "Welcome to our website. All rights reserved. Cookie policy is updated. "
        "Log in to read more. Please click here to subscribe."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"
    assert intelligence["problem_evidence"] == []


def test_token_denominated_award_amount_and_currency_are_extracted():
    text = "Builders receive a $200 USDG cheque for the winning project."
    amount = _extract_funding_amount(text)

    assert amount == "$200 USDG"
    assert _extract_currency(amount) == "USDG"


def test_general_themes_do_not_promote_navigation_terms_as_singletons():
    themes = _extract_general_themes(
        "Fun Earn opportunities. Fun Earn. Image processing support for builders."
    )

    assert "fun earn" not in [theme.casefold() for theme in themes]
    assert "image" not in [theme.casefold() for theme in themes]
    assert "processing" not in [theme.casefold() for theme in themes]


def test_markdown_heading_markers_are_removed_from_structured_titles():
    document = _document(
        "Applications are open. Deadline: January 5, 2027.",
        title="## NIH Small Business Innovation Research Phase IIB Award",
    )
    analysis = analyze_document(document)

    assert not analysis.program_name.lstrip().startswith("#")
