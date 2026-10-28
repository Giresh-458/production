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
