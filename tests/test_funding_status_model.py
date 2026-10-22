from agents.funding_agent import _is_generic_funding_title, determine_status

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
