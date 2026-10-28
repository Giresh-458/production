"""Regression tests for evidence-grounded funding output quality."""

from agents.funding_agent import (
    FundingDocument,
    FundingOpportunity,
    _extract_currency,
    _extract_eligibility_from_prose,
    _extract_funding_amount,
    _extract_funding_body,
    _extract_program_name,
    extract_call_dates,
    _extract_general_themes,
    _extract_geography_from_prose,
    _merge_related_eligibility_context,
    _resolve_application_url,
    analyze_document,
    extract_funding_problem_intelligence,
)


def _document(
    content: str,
    title: str = "Test Funding Call",
    source: str = "https://example.org/funding-call",
) -> FundingDocument:
    return FundingDocument(
        organization="Test Foundation",
        title=title,
        source=source,
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


def test_bioeconomy_giants_pdf_metadata_uses_call_factsheet_not_project_metadata():
    content = """
    Project Name FosterinG collaboratIon among primAry producers and beyond
    Start Date June 1st 2025
    Call Title Call for Scaling Up and Piloting
    Granting Authority Circular Bio-based Europe Joint Undertaking
    Opening Date 1st November 2026
    Closing Date 31st December 2026 at 17:00 CET
    Total Budget € 280,000
    Max Funding per Project € 70,000 (Max. 60,000€ per granted organization/legal entity)
    """
    opening, deadline = extract_call_dates(content)
    assert opening == "2026-11-01"
    assert deadline == "2026-12-31"
    assert _extract_program_name(
        content,
        "BIOECONOMY GIANTS Open Call for Scaling Up Piloting OC2.pdf",
    ) == "Call for Scaling Up and Piloting"
    assert _extract_funding_body(content, "Climate KIC Open Calls") == "Circular Bio-based Europe Joint Undertaking"
    assert _extract_funding_amount(content) == "€ 70,000"


def test_funding_problem_signal_rejects_potential_application_scope_as_problem():
    content = (
        "Potential application fields include, but are not limited to, sustainable "
        "bio-based packaging, bio-based materials, bio-based chemical intermediates, "
        "bio-based food or feed ingredients, bio-based specialty chemicals, "
        "biofertilizers, organic amendments, or similar high added-value bio-based products."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))
    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"
    assert intelligence["problem_evidence"] == []


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


def test_funding_problem_signal_rejects_call_objectives_that_mention_risk():
    content = (
        "The call is designed to accelerate market uptake by reducing development "
        "and deployment risk."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"


def test_funding_problem_signal_rejects_call_focus_statement_about_risk():
    content = (
        "The Call focus is on accelerating the market uptake of impactful innovations "
        "by reducing the risk of development and deployment."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"


def test_funding_problem_signal_rejects_required_scope_as_a_problem():
    content = (
        "The action should address specified aspects, including tracking climate-projection "
        "data availability, gaps and risks."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"


def test_eligibility_extraction_collects_multiple_explicit_criteria():
    content = (
        "This Call is a mono-beneficiary call addressed to SMEs supported by an end client. "
        "Applicants must have at least three full-time equivalent employees. "
        "The solution must be commercially mature at TRL 7."
    )

    eligibility = _extract_eligibility_from_prose(content)

    assert eligibility is not None
    assert "SMEs supported by an end client" in eligibility
    assert "at least three full-time equivalent employees" in eligibility
    assert "TRL 7" in eligibility


def test_sample_application_form_is_not_mislabeled_as_submission_endpoint():
    source_url = "https://example.org/calls/example-open-call"
    content = (
        "The call is open for applications. "
        "[Sample application form](https://example.org/SAMPLE_Application_form.pdf). "
        "Access the programme portal and submit your application before the deadline."
    )

    assert _resolve_application_url(content, source_url, "OPEN_CALL") is None


def test_eit_sme_market_expansion_call_extraction_is_date_independent():
    source_url = (
        "https://www.eiturbanmobility.eu/call-for-proposals/"
        "sme-market-expansion-open-call-2027/"
    )
    content = (
        "SME Market Expansion Open Call 2027. "
        "The Call is designed to promote business growth and market expansion. "
        "The Call will focus on five topics: Public and Shared Transport, Urban Logistics, "
        "Electrification of transport and alternative fuels, Mobility data management, "
        "Health and mobility. "
        "Each awarded entity will receive a fixed lump sum grant of EUR 60,000. "
        "Who can apply? This Call is a mono-beneficiary call addressed to small and "
        "medium enterprises (SMEs) that are supported by an end client, a public or "
        "private entity, committed to testing and demonstrating the solution. "
        "Call opening: 9 October 2026. "
        "Call closing (submission of completed application): 1 December 2026 at 17.00 CET. "
        "[Sample application form](https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/"
        "SAMPLE_Application_form_SME-Market-Expansion-Call-2027.pdf). "
        "Access the EIT UM Programmes Portal and submit your application form within the given deadline."
    )
    document = _document(content, title="SME Market Expansion Open Call 2027", source=source_url)
    analysis = analyze_document(document)

    assert analysis.program_name == "SME Market Expansion Open Call 2027"
    assert analysis.opening_date == "2026-10-09"
    assert analysis.deadline == "2026-12-01"
    assert analysis.funding_amount is not None and "60,000" in analysis.funding_amount
    assert analysis.eligibility is not None
    assert "SMEs" in analysis.eligibility
    assert "end client" in analysis.eligibility
    # A sample PDF and a call-information page do not prove the real submission endpoint.
    assert analysis.application_url is None


def test_application_portal_link_is_preferred_over_sample_form():
    content = (
        "[Application form sample](https://example.org/SAMPLE_Application_form.pdf). "
        "Access the [Programme Portal](https://portal.example.org/apply) and submit "
        "your application there before the deadline."
    )

    assert _resolve_application_url(
        content,
        "https://example.org/call-information",
        "OPEN_CALL",
    ) == "https://portal.example.org/apply"


def test_related_official_context_merges_eligibility_with_provenance():
    from types import SimpleNamespace

    primary_url = (
        "https://www.eiturbanmobility.eu/call-for-proposals/"
        "sme-market-expansion-open-call-2027/"
    )
    context_url = "https://www.eiturbanmobility.eu/sme-market-expansion-open-call-2027/"
    primary = _document(
        "This Call is a mono-beneficiary call addressed to SMEs supported by an end client.",
        title="SME Market Expansion Open Call 2027",
        source=primary_url,
    )
    context = _document(
        "The call is aimed at established SMEs with at least three full-time equivalent "
        "employees and a commercially mature urban mobility solution at TRL 7 or above. "
        "Applicants should demonstrate previous customers, deployments, pilots or sales.",
        title="Scaling proven solutions to new markets: SME Market Expansion Open Call 2027",
        source=context_url,
    )
    primary_analysis = _analysis(primary)
    primary_analysis.eligibility = (
        "This Call is a mono-beneficiary call addressed to SMEs supported by an end client."
    )
    context_analysis = _analysis(context)
    source = SimpleNamespace(collection={
        "related_context_pages": [{
            "primary_url": primary_url,
            "context_url": context_url,
            "fields": ["eligibility"],
        }]
    })

    supplemental_urls, warnings = _merge_related_eligibility_context(
        [(primary, primary_analysis), (context, context_analysis)],
        [source],
    )

    assert "three full-time equivalent employees" in primary_analysis.eligibility
    assert "TRL 7" in primary_analysis.eligibility
    assert "end client" in primary_analysis.eligibility
    assert primary_analysis.evidence["eligibility_supplemental_context_status"] == "verified"
    assert primary_analysis.evidence["eligibility_supplemental_source_url"] == context_url
    assert context_url.rstrip("/") in supplemental_urls
    assert warnings == []


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



def test_specific_sbir_h2_heading_overrides_generic_topic_page_title():
    content = (
        "# Topic\n"
        "Funding Opportunities\n"
        "## NIH Small Business Technology Transfer Grant "
        "(Parent STTR [R41/R42] Clinical Trial Optional)\n"
        "Solicitation Status: Open\n"
        "Due Date(s): January 5, 2027; April 5, 2027\n"
    )
    document = _document(content, title="Topic | SBIR")
    analysis = analyze_document(document)

    assert analysis.program_name.startswith(
        "NIH Small Business Technology Transfer Grant"
    )
    assert "Topic" != analysis.program_name
    assert analysis.status == "OPEN_CALL"
    assert analysis.deadline == "2027-01-05"



def test_sbir_topic_title_without_funding_keyword_is_not_dropped():
    content = (
        "# Topic\n"
        "## Trusted Identity and Identity Management\n"
        "### Funding Agency\n"
        "Topic Number: DL14\n"
        "Solicitation Status: Open\n"
        "Due Date(s): January 5, 2027; April 5, 2027\n"
        "Small Business Innovation Research (SBIR)\n"
    )
    document = _document(content, title="Topic | SBIR")
    analysis = analyze_document(document)

    assert analysis.program_name == "Trusted Identity and Identity Management"
    assert analysis.status == "OPEN_CALL"
    assert analysis.deadline == "2027-01-05"



def test_docx_fellowship_title_skips_generic_application_form_heading():
    from core.crawl4ai_web import _extract_docx_title

    title = _extract_docx_title(
        [
            "CHANAKYA FELLOWSHIP – APPLICATION FORM",
            "CHANAKYA UG Fellowship – I",
            "The fellowship is for motivated undergraduate students.",
        ],
        "https://idapthub.org/frontend/docs/chanakya/2_chanakya_ug_fellowship_II.docx",
    )

    assert title == "CHANAKYA UG Fellowship – I"


def test_explicit_fellowship_monthly_amount_and_currency_are_extracted():
    amount = _extract_funding_amount(
        "The fellowship amount is Rs. 4,000/- per month."
    )

    assert amount == "Rs. 4,000"
    assert _extract_currency(amount) == "INR"



def test_consolidated_fellowship_amount_is_extracted_without_inference():
    amount = _extract_funding_amount(
        "The fellowship will be Rs. 80,000/- (Consolidated)."
    )

    assert amount == "Rs. 80,000"
    assert _extract_currency(amount) == "INR"


def test_fellowship_form_eligibility_is_extracted_from_explicit_prose():
    sentence = (
        "B.Tech./B.E. students registered in any recognised college "
        "can apply through a mentor."
    )

    assert _extract_eligibility_from_prose(sentence) == sentence


def test_direct_downloadable_application_form_is_application_url():
    url = "https://idapthub.org/frontend/docs/chanakya/2_chanakya_ug_fellowship_II.docx"

    assert _resolve_application_url(
        "CHANAKYA FELLOWSHIP – APPLICATION FORM\nCHANAKYA UG Fellowship – I",
        url,
        "UNKNOWN",
    ) == url



def test_explicit_geographic_eligibility_is_extracted_without_inference():
    assert _extract_geography_from_prose(
        "If eligible for funding, legal entities established in low- to middle-income "
        "countries automatically eligible for Horizon Europe funding may exceptionally participate."
    ) == "Low- and middle-income countries (exceptionally eligible)"

    assert _extract_geography_from_prose(
        "Legal entities established in all African Union member states are exceptionally eligible."
    ) == "African Union member states (exceptionally eligible)"

    assert _extract_geography_from_prose(
        "Applicants must be based in an EU Member State or a country associated with Horizon Europe."
    ) == "EU Member States and Horizon Europe Associated Countries"

    assert _extract_geography_from_prose(
        "Eligible applicants must be based in the United States."
    ) == "United States"


def test_duplicate_eligibility_prefix_is_normalized():
    text = (
        "Eligible applicants must be Applicants must be based in an EU Member State "
        "or a country associated with Horizon Europe (Pillar 2)."
    )

    assert _extract_eligibility_from_prose(text) == (
        "Eligible applicants must be based in an EU Member State or a country "
        "associated with Horizon Europe (Pillar 2)."
    )



def test_labelled_eligibility_section_also_normalizes_duplicate_prefix():
    document = _document(
        "Eligibility: Eligible applicants must be Applicants must be based in an "
        "EU Member State or a country associated with Horizon Europe (Pillar 2).",
        title="Climate-smart use of wood",
    )
    analysis = analyze_document(document)

    assert analysis.eligibility == (
        "Eligible applicants must be based in an EU Member State or a country "
        "associated with Horizon Europe (Pillar 2)."
    )



def test_sbir_mirror_disclaimer_prevents_unverified_open_status_and_deadline():
    content = (
        "# Topic\n"
        "## Trusted Identity and Identity Management\n"
        "Topic Number: DL14\n"
        "Solicitation Status: Open\n"
        "Due Date(s): January 5, 2027; April 5, 2027\n"
        "The topics listed on this site are copies from agency solicitations and "
        "are not necessarily the latest and most up-to-date.\n"
    )
    document = _document(
        content,
        title="Topic | SBIR",
        source="https://www.sbir.gov/topics/1012406",
    )
    analysis = analyze_document(document)

    assert analysis.program_name == "Trusted Identity and Identity Management"
    assert analysis.status == "UNKNOWN"
    assert analysis.opening_date is None
    assert analysis.deadline is None


def test_funding_problem_signal_rejects_subcontracting_proportionality_rule():
    content = (
        "Limited parts mean subcontracting remains proportionate to the needs "
        "of each funded action."
    )
    document = _document(content)
    intelligence = extract_funding_problem_intelligence(document, _analysis(document))

    assert intelligence["selected_problem"] is None
    assert intelligence["problem_type"] == "unresolved"
    assert intelligence["problem_evidence"] == []
