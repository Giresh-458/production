from agents.funding_agent import _is_generic_funding_title, determine_status

def test_generic_portal_titles_include_brand_suffixes():
    assert _is_generic_funding_title("Topic | SBIR")
    assert _is_generic_funding_title("Documents | Page 10")
    assert not _is_generic_funding_title("Trusted Identity Research Topic 2026")


def test_determine_status_generic_page():
    # generic page + open-looking language + no application/deadline -> UNKNOWN
    title = "Documents | Page 10"
    content = "Applications are open for this request for proposals."
    status = determine_status(explicit_status=None, opening=None, deadline=None, content=content, title=title)
    assert status == "UNKNOWN"

    title2 = "Funding Opportunities"
    content2 = "Submit a proposal now, the RFP is open."
    status2 = determine_status(explicit_status=None, opening=None, deadline=None, content=content2, title=title2)
    assert status2 == "UNKNOWN"

def test_generic_title_cannot_be_overridden_by_explicit_open_status():
    status = determine_status(
        explicit_status="OPEN",
        opening=None,
        deadline=None,
        content="Solicitation status: Open. Applications are open for this request for proposals.",
        title="Documents | Page 10",
    )
    assert status == "UNKNOWN"

def test_determine_status_explicit_open():
    title = "Specific AI Security Grant 2026"
    content = "Applications are open for this request for proposals."
    status = determine_status(explicit_status=None, opening=None, deadline=None, content=content, title=title)
    assert status == "OPEN_CALL"


def test_status_evidence_patterns_cover_call_level_status_badges_and_due_dates():
    from agents.funding_agent import _evidence_excerpt

    status_text = "Development Grant | Open | UK. Applications are accepted on a rolling basis."
    status = _evidence_excerpt(status_text, [
        r"\b(?:grant|fellowship|bounty)\s*\|\s*open\s*\|",
        r"applications? (?:are )?accepted on a rolling basis",
    ])
    assert status
    assert "Open" in status or "rolling basis" in status

    due_text = "Proposal deadline: 30 November 2026. Submit by this date."
    due = _evidence_excerpt(due_text, [
        r"(?:due date|proposal deadline|closing date|applications? due|submit(?:ted)? by|apply by|deadline date)[^.!?\n]{0,180}",
    ])
    assert due
    assert "Proposal deadline" in due


def test_explicit_active_and_accepting_status_labels_are_normalized_for_specific_calls():
    assert determine_status(
        "ACTIVE", None, None, "Application status: Active.", "Specific Stablecoin Research Grant"
    ) == "OPEN_CALL"
    assert determine_status(
        "ACCEPTING APPLICATIONS", None, None, "Currently accepting applications.",
        "Specific Decentralized Identity Grant"
    ) == "OPEN_CALL"


def test_status_parser_does_not_promote_generic_directory_status_to_open():
    assert determine_status(
        "ACTIVE", None, None, "Status: Active.", "Funding Opportunities"
    ) == "UNKNOWN"


def test_status_parser_recognizes_explicit_closed_application_language():
    assert determine_status(
        None, None, None, "The applications are closed. Sign up for future updates.",
        "XRPL Program by Brinc",
    ) == "CLOSED_CALL"


def test_multiline_structured_grant_badge_is_preserved_as_status_evidence():
    from agents.funding_agent import _status_evidence_excerpt

    text = "Startup Accelerator Grant\nGrant\n|\nOpen\n|\nGlobal\nApply Now"
    evidence = _status_evidence_excerpt(text, [
        r"\b(?:grant|fellowship|bounty)\s*\|\s*(?:open|closed|paused|upcoming)\s*\|",
    ])
    assert evidence
    assert "Grant" in evidence and "Open" in evidence


def test_navigation_titles_used_by_live_sources_are_generic():
    for title in (
        "Active funding opportunity",
        "Archived funding opportunity",
        "Program contacts",
        "How We Make Funding Decisions",
    ):
        assert _is_generic_funding_title(title)


def test_airtable_directory_and_record_context_titles_are_not_individual_calls():
    assert _is_generic_funding_title("Airtable - Solana Foundation Active RFPs")
    assert _is_generic_funding_title("Solana Foundation Public RFP Database")
    assert _is_generic_funding_title("RFP CONTEXT")
    assert not _is_generic_funding_title("Build for Breakpoint: Agentic Grants")


def test_section_value_reads_eligibility_paragraph_after_markdown_heading():
    from agents.funding_agent import _extract_section_value

    content = """# Call details
## Who can apply?
This call is addressed to small and medium enterprises (SMEs) established in eligible countries.
Applicants must have at least three full-time employees.
## Funding
The maximum grant is EUR 50,000.
"""
    value = _extract_section_value(content, ("eligibility", "who can apply", "eligible applicants"))
    assert value
    assert "small and medium enterprises" in value
    assert "three full-time employees" in value
    assert "maximum grant" not in value


def test_section_value_skips_unknown_placeholder_and_extracts_real_section():
    from agents.funding_agent import _extract_section_value

    content = """Eligibility: Unknown
## Eligibility
Applicants must be a U.S. small business with fewer than 500 employees.
## Funding
Up to $300,000.
"""
    value = _extract_section_value(content, ("eligibility", "who can apply"))
    assert value
    assert "U.S. small business" in value
    assert "Unknown" not in value
