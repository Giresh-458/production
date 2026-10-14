from datetime import UTC, datetime, timedelta

from agents.funding_agent import _format_collection_error, determine_status, extract_call_dates
from run_pipeline import get_all_open_funding_calls


def test_live_date_extraction_marks_current_call_open():
    text = "Road to Devcon 8 India University Program. Opens: Apr 20, 2026. Closes: Sep 30, 2029."
    opening, deadline = extract_call_dates(text)
    assert opening == "2026-04-20"
    assert deadline == "2029-09-30"
    assert determine_status(None, opening, deadline, text) == "OPEN_CALL"


def test_github_open_grant_proposal_issue_date_is_not_call_opening_date():
    # GitHub's "opened on <date>" metadata is an issue creation timestamp,
    # not the date that the grant program started accepting applications.
    # The title and GitHub metadata can become adjacent after whitespace cleanup,
    # so no punctuation separates "Open Grant Proposal" from "opened on".
    text = (
        "[Open Grant Proposal] Example Filecoin project proposal "
        "Alice opened on Oct 9, 2026."
    )
    opening, deadline = extract_call_dates(text)

    assert opening is None
    assert deadline is None
    assert determine_status(None, opening, deadline, text, title="Open Grant Proposal") != "OPEN_CALL"


def test_select_open_funding_call_rejects_closed_and_future_calls():
    now = datetime.now(UTC)
    open_deadline = (now + timedelta(days=14)).date().isoformat()
    future_open = (now + timedelta(days=5)).date().isoformat()
    records = {
        "outputs": [
            {
                "title": "Open Call",
                "program_name": "Open Call",
                "status": "OPEN_CALL",
                "deadline": open_deadline,
                "domain_relevance": [{"domain": "RWA", "score": 0.75, "raw_score": 3.0, "matched_terms": ["rwa"]}],
                "source_research_domains": ["RWA"],
                "application_url": "https://example.org/apply",
            },
            {
                "title": "Closed Call",
                "program_name": "Closed Call",
                "status": "CLOSED_CALL",
                "deadline": (now - timedelta(days=1)).date().isoformat(),
                "domain_relevance": [{"domain": "RWA", "score": 0.9}],
            },
            {
                "title": "Future Call",
                "program_name": "Future Call",
                "status": "UPCOMING_CALL",
                "opening_date": future_open,
                "deadline": (now + timedelta(days=30)).date().isoformat(),
                "domain_relevance": [{"domain": "RWA", "score": 1.0}],
            },
        ]
    }
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 1
    assert selected[0]["program_name"] == "Open Call"
    assert any(item["title"] == "Closed Call" for item in rejected)
    assert any(item["title"] == "Future Call" for item in rejected)


def test_date_parser_handles_real_world_ordinal_and_day_month_formats():
    from agents.funding_agent import parse_date_string
    assert parse_date_string("30 September 2026") .date().isoformat() == "2026-09-30"
    assert parse_date_string("September 30th, 2026").date().isoformat() == "2026-09-30"
    assert parse_date_string("2026-09-30T23:59:00+05:30").date().isoformat() == "2026-09-30"
    assert parse_date_string("30/09/2026").date().isoformat() == "2026-09-30"


def test_extract_call_dates_handles_prose_deadline_and_opening():
    from agents.funding_agent import extract_call_dates
    opening, deadline = extract_call_dates(
        "Applications open on 20 April 2026 and close at 23:59 IST on 30 September 2026."
    )
    assert opening == "2026-04-20"
    assert deadline == "2026-09-30"


def test_status_does_not_call_generic_program_page_open():
    from agents.funding_agent import determine_status
    assert determine_status(None, None, None, "Our program supports researchers. Apply for membership at any time.") == "UNKNOWN"


def test_status_requires_stronger_call_evidence_for_date_less_pages():
    from agents.funding_agent import determine_status
    assert determine_status(None, None, None, "Call for proposals. Applications are now open. Apply now.") == "OPEN_CALL"


def test_open_call_with_future_deadline_and_application_url_is_current_call():
    from agents.funding_agent import FundingDocument, analyze_document
    future = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    doc = FundingDocument(
        organization="Test Foundation",
        title="Research Innovation Challenge 2026",
        source="https://example.org/calls/research-innovation",
        source_type="html",
        content=(
            f"Research Innovation Challenge 2026. Deadline: {future}. "
            "Eligible applicants: universities. Apply here: https://example.org/calls/research-innovation/apply."
        ),
        year=2026,
    )
    result = analyze_document(doc)
    assert result.status == "OPEN_CALL"
    assert result.deadline == future
    assert result.application_url


def test_official_filecoin_open_grants_welcome_language_is_a_live_call():
    from agents.funding_agent import FundingDocument, analyze_document

    content = (
        "# Open Grants\n"
        "Open Grants applications for funding up to $50,000 are welcome in the following categories:\n"
        "Developer and Data Tooling; Integrations; Research & protocols; Storage; Retrieval; FVM.\n"
        "The program supports projects that advance the Filecoin ecosystem and directly advance the Filecoin mission statement.\n"
        "Note: The Devgrants program will prioritize functionality for Filecoin Network.\n"
        "To submit an Open Grant proposal, use [create a new issue]"
        "(https://github.com/filecoin-project/devgrants/issues/new?assignees=&labels=Open+Grant&template=open_grant.yml&title=Open+Grant+Proposal%3A).\n"
        "We review grant proposals continuously."
    )
    document = FundingDocument(
        organization="Filecoin Grants and Funding",
        title="Open Grants",
        source=(
            "https://raw.githubusercontent.com/filecoin-project/devgrants/master/"
            "Program%20Resources/Open%20Grants%20README.md"
        ),
        source_type="html",
        content=content,
        year=2026,
    )
    opportunity = analyze_document(document)

    assert opportunity.status == "OPEN_CALL"
    assert "welcome" in opportunity.evidence.get("status", "").lower()
    assert opportunity.funding_amount
    assert opportunity.application_url
    assert "/issues/new" in opportunity.application_url


def test_filecoin_open_grants_pass_strict_gate_with_depin_evidence():
    from agents.funding_agent import FundingDocument, analyze_document, build_domain_relevance

    content = (
        "# Open Grants\n"
        "Open Grants applications for funding up to $50,000 are welcome in the following categories:\n"
        "Developer and Data Tooling; Integrations; Research & protocols; Storage; Retrieval; FVM.\n"
        "The program supports projects that advance the Filecoin ecosystem and directly advance the Filecoin mission statement.\n"
        "The Devgrants program will prioritize functionality for Filecoin Network.\n"
        "To submit an Open Grant proposal, use [create a new issue]"
        "(https://github.com/filecoin-project/devgrants/issues/new?labels=Open+Grant&template=open_grant.yml).\n"
        "We review grant proposals continuously."
    )
    doc = FundingDocument(
        organization="Filecoin Grants and Funding",
        title="Open Grants",
        source="https://raw.githubusercontent.com/filecoin-project/devgrants/master/Program%20Resources/Open%20Grants%20README.md",
        source_type="html",
        content=content,
        year=2026,
    )
    opportunity = analyze_document(doc)
    record = {
        "title": opportunity.program_name,
        "program_name": opportunity.program_name,
        "status": opportunity.status,
        "source_url": opportunity.source_url,
        "application_url": opportunity.application_url,
        "funding_amount": opportunity.funding_amount,
        "eligibility": opportunity.eligibility,
        "evaluation_criteria": opportunity.evaluation_criteria,
        "evidence": opportunity.evidence,
        "domain_relevance": build_domain_relevance(content),
        "source_research_domains": ["DePIN"],
    }
    selected, rejected = get_all_open_funding_calls({"outputs": [record]}, require_domain_match=True)

    assert len(selected) == 1, rejected
    assert selected[0]["matched_rif_domains"] == ["DePIN"]


def test_literature_unscoped_mode_uses_funding_mission_terms():
    from agents.literature_agent import get_keywords, build_query
    from core.funding_context import current_funding_queries
    token = current_funding_queries.set(["privacy preserving clinical data interoperability"])
    try:
        terms = get_keywords(None)
        query = build_query(None)
    finally:
        current_funding_queries.reset(token)
    assert "privacy" in terms
    assert "clinical" in terms
    assert "privacy" in query.lower()



def test_rolling_mev_grant_language_survives_funding_extraction():
    from agents.funding_agent import FundingDocument, analyze_document

    document = FundingDocument(
        organization="Flashbots MEV Research Grants",
        title="MEV Fellowship Grants",
        source="https://raw.githubusercontent.com/flashbots/mev-research/main/grants.md",
        source_type="html",
        content=(
            "MEV Fellowship Grants. The MEV fellowship supports research on MEV. "
            "The grant is a total of $15k. There is no deadline; fellows are accepted on a "
            "rolling basis. Submit a research proposal describing the approach. "
            "A research blog post or academic paper is an expected deliverable."
        ),
        year=2026,
    )

    opportunity = analyze_document(document)

    assert opportunity.status == "OPEN_CALL"
    assert "rolling basis" in opportunity.evidence.get("status", "").lower()
    assert opportunity.funding_amount

def test_solana_current_program_uses_contextual_application_link_and_rolling_review():
    from agents.funding_agent import FundingDocument, analyze_document

    application_url = "https://share.hsforms.com/example-solana-grant-application"
    content = (
        "# Grants and Funding\n"
        "## Solana Foundation Funding\n"
        "The Solana Foundation supports public goods through grants and funding.\n"
        "All funding applicants are required to fill out the application [here]("
        + application_url
        + ").\n"
        "All applications are reviewed on a rolling basis.\n"
        "Applicants should provide a project overview, budget, and milestones."
    )
    opportunity = analyze_document(FundingDocument(
        organization="Solana Grants and Funding",
        title="Grants and Funding | Solana",
        source="https://solana.org/grants-funding",
        source_type="html",
        content=content,
        year=2026,
    ))

    assert opportunity.program_name == "Solana Foundation Funding"
    assert opportunity.status == "OPEN_CALL"
    assert opportunity.application_url == application_url
    assert "rolling basis" in opportunity.evidence.get("status", "").lower()


def test_past_deadline_takes_precedence_over_awardee_copy():
    status = determine_status(
        None,
        None,
        "2026-05-27",
        "Climate Pitch grants. Grant recipients: 8 startups. Last Date to Apply – 27 May 2026.",
        "Climate Pitch",
    )
    assert status == "CLOSED_CALL"


def test_explicit_current_acceptance_language_takes_precedence_over_awardee_copy():
    status = determine_status(
        None,
        None,
        None,
        "Grant recipients are published separately. Applications now open for fall accelerator programs. Submit an application form.",
        "XRPL programme landing page",
    )
    assert status == "OPEN_CALL"


def test_paused_submission_text_overrides_rolling_acceptance_and_explicit_open_status():
    content = (
        "Flashbots MEV Research Proposals. Proposals are accepted on a rolling basis. "
        "FRP is currently paused, so new submissions will not be processed."
    )
    assert determine_status("OPEN_CALL", None, None, content, "MEV Research Grants") == "CLOSED_CALL"
    assert determine_status(None, None, None, content, "MEV Research Grants") == "CLOSED_CALL"


def test_past_deadline_overrides_explicit_open_status():
    past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    assert determine_status(
        "OPEN_CALL", None, past,
        "Applications are now open. Submit a funding proposal.",
        "Test Grant",
    ) == "CLOSED_CALL"


def test_explicit_funded_status_does_not_hide_verified_rolling_application():
    content = (
        "Research Grant. Applications are accepted on a rolling basis. "
        "Submit a research proposal to the programme."
    )
    assert determine_status(
        "FUNDED_PROJECT", None, None, content, "Research Grant"
    ) == "OPEN_CALL"


def test_crawler_url_normalization_preserves_trailing_slashes_but_dedupes_aliases():
    from core.crawl4ai_web import _normalize_url, _url_dedupe_key

    canonical = "https://nearn.io/devhub/"
    assert _normalize_url(canonical) == canonical
    assert _normalize_url(canonical + "#opportunities") == canonical
    assert _url_dedupe_key(canonical) == _url_dedupe_key("https://nearn.io/devhub")


def test_successful_redirect_uses_destination_status_and_final_url():
    from unittest.mock import patch
    from core.crawl4ai_web import crawl_funding_source

    requested_url = "https://nearn.io/devhub"
    final_url = "https://nearn.io/devhub/"

    def fake_fetch(url):
        assert url == requested_url
        return {
            "html": "<html><body>NEARN opportunity directory</body></html>",
            "markdown": "NEARN DevHub Opportunities. Earning opportunities.",
            "success": True,
            "error_message": None,
            "status_code": 308,
            "redirected_status_code": 200,
            "url": final_url,
            "title": "NEAR DevHub Opportunities | NEARN",
            "links": [],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch), \
         patch("core.crawl4ai_web._wait_for_host_slot", return_value=True):
        result = crawl_funding_source(
            "NEAR",
            requested_url,
            {
                "start_urls": [requested_url],
                "max_depth": 0,
                "max_pages": 1,
                "respect_robots_txt": False,
            },
            ["nearn.io"],
        )

    assert result.pages_fetched == 1
    assert result.pages_failed == 0
    assert result.errors == []
    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"


def test_final_page_url_prefers_redirect_destination_for_relative_links():
    from types import SimpleNamespace
    from urllib.parse import urljoin
    from core.crawl4ai_web import _get_final_page_url

    result = SimpleNamespace(
        url="https://earn.superteam.fun/grants/",
        redirected_url="https://superteam.fun/earn/",
    )
    page_url = _get_final_page_url(result, "https://earn.superteam.fun/grants/")
    assert page_url == "https://superteam.fun/earn/"
    assert urljoin(page_url, "opportunities/development-grants/") == (
        "https://superteam.fun/earn/opportunities/development-grants/"
    )


def test_direct_page_override_applies_only_to_primary_start_url():
    from unittest.mock import patch
    from core.crawl4ai_web import crawl_funding_source

    primary = "https://solana.org/grants-funding"
    secondary = "https://superteam.fun/earn/opportunities/development-grants/"

    def fake_fetch(url):
        if url == primary:
            title = "Grants and Funding"
            markdown = "A generic funding directory without specific application instructions."
        else:
            title = "Development Grants | Superteam Earn"
            markdown = "Find remote web3 development grants on Solana. Grants. Development."
        return {
            "html": "<html><body>Funding directory</body></html>",
            "markdown": markdown,
            "success": True,
            "error_message": None,
            "status_code": 200,
            "url": url,
            "title": title,
            "links": [],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch), \
         patch("core.crawl4ai_web._wait_for_host_slot", return_value=True):
        result = crawl_funding_source(
            "Solana",
            primary,
            {
                "start_urls": [primary, secondary],
                "max_depth": 0,
                "max_pages": 2,
                "direct_page": True,
                "respect_robots_txt": False,
            },
            ["solana.org", "superteam.fun"],
        )

    assert [item["url"] for item in result.opportunities] == [primary]


def test_chainlink_status_announcement_is_not_misclassified_as_a_funding_call():
    from core.crawl4ai_web import _classify_page_stage_b

    title = "Evolving the Chainlink Build Program"
    content = (
        "The Chainlink ecosystem is continually evolving how it supports early and mid-stage projects. "
        "Existing arrangements under the Build program are being concluded, while new commercial "
        "agreements are established on a case-by-case basis. The latest season of Build-related token "
        "rewards marked the conclusion of those rewards, with claims ending July 7, 2026. "
        "This announcement is a program-status update, not an open application call."
    )
    assert _classify_page_stage_b(
        "https://chain.link/blog/build-program-evolution", title, content
    ) is False


def test_collection_error_formatter_exposes_operation_url_and_reason():
    error = {
        "operation": "page_fetch",
        "url": "https://chain.link/community/grants",
        "error": "HTTP 404",
    }
    assert _format_collection_error(error) == (
        "page_fetch | https://chain.link/community/grants | HTTP 404"
    )
    assert _format_collection_error("robots.txt disallows this URL") == (
        "robots.txt disallows this URL"
    )
    assert len(_format_collection_error({"error": "x" * 700})) <= 600



def test_superteam_grant_directories_are_traversal_roots_not_opportunities():
    from core.crawl4ai_web import _classify_page_stage_b, _is_generic_funding_hub

    samples = [
        (
            "https://superteam.fun/earn/opportunities/development-grants/",
            "Development Grants | Superteam Earn",
            "Browse development grants and funding opportunities. Apply for a grant. "
            "View grants, deadlines, eligibility and application instructions.",
        ),
        (
            "https://earn.superteam.fun/grants/?utm_source=hs_automation",
            "Grants | Superteam Earn",
            "Browse grants and funding opportunities. Apply for grants. "
            "View individual grant listings and their eligibility requirements.",
        ),
        (
            "https://superteam.fun/earn/regions/india/",
            "Welcome to Superteam Earn India",
            "Browse opportunities and grants available to the Indian community. "
            "Apply for grants and review individual listing details.",
        ),
    ]

    for url, title, content in samples:
        assert _is_generic_funding_hub(url, title, content)
        assert not _classify_page_stage_b(url, title, content)



def test_fresh_application_closure_overrides_anytime_submission_language():
    from agents.funding_agent import determine_status

    content = (
        "Call for Innovative and Entrepreneurial Research Projects. "
        "Applications can be submitted at any time. "
        "Important: I-DAPT-Hub Foundation is currently not accepting any fresh "
        "applications for Research Projects and Post-Doctoral Chanakya Fellowships."
    )
    assert determine_status(
        None, None, None, content,
        "Call for Innovative and Entrepreneurial Research Projects",
    ) == "CLOSED_CALL"


def test_program_name_uses_specific_markdown_h1_over_generic_listing_title():
    from agents.funding_agent import _extract_program_name

    content = (
        "[Grants](https://superteam.fun/earn/grants) "
        "![grant](https://superteam.fun/assets/grant-icon.svg) Grant | Open | UK\n"
        "# Build for Breakpoint: Agentic Grants\n"
        "Applications: Sep 14 - Nov 14. Eligible applicants: United Kingdom."
    )
    assert _extract_program_name(content, "this listing") == "Build for Breakpoint: Agentic Grants"


def test_section_extraction_ignores_incidental_geography_mentions():
    from agents.funding_agent import _extract_section_value

    incidental = (
        "If none of these fit your needs by geography, sector, or vertical, "
        "some of these options might suit you."
    )
    labels = ("geographic eligibility", "geography", "eligible countries", "eligible regions")
    assert _extract_section_value(incidental, labels) is None
    assert _extract_section_value("Geography: United Kingdom", labels) == "United Kingdom"



def test_specific_open_grant_listing_badge_sets_open_status_without_guessing_dates():
    from agents.funding_agent import determine_status

    content = (
        "Grant | Open | UK. Applications: Sep 14 - Nov 14. "
        "Approved builders receive 50% upfront and 50% once the demo is live."
    )
    assert determine_status(
        None, None, None, content,
        "Build for Breakpoint: Agentic Grants",
    ) == "OPEN_CALL"

    # The same badge can appear on a card inside a directory; a generic page title
    # must not inherit an individual card's status.
    assert determine_status(None, None, None, content, "Development Grants") == "UNKNOWN"
