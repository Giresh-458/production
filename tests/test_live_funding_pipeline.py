from datetime import UTC, datetime, timedelta

from agents.funding_agent import _format_collection_error, determine_status, extract_call_dates
from run_pipeline import get_all_open_funding_calls


def test_eu_search_api_open_status_code_is_authoritative_for_current_topic_details():
    now = datetime.now(UTC)
    record = {
        "title": "Climate transition research call",
        "program_name": "Climate transition research call",
        "status": "OPEN_CALL",
        "deadline": (now + timedelta(days=30)).date().isoformat(),
        "source_url": (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
            "screen/opportunities/topic-details/HORIZON-CLIMATE-2027-01"
        ),
        "domain_relevance": [{
            "domain": "ESG",
            "score": 0.75,
            "raw_score": 18.0,
            "matched_terms": ["climate change mitigation", "climate"],
        }],
        "source_research_domains": ["ESG"],
        "eligibility": "The consortium must include eligible legal entities.",
        "evidence": {"source_status": "31094502"},
    }

    selected, rejected = get_all_open_funding_calls({"outputs": [record]})
    assert len(selected) == 1, rejected
    assert selected[0]["title"] == "Climate transition research call"


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



def test_filecoin_repository_link_does_not_outrank_explicit_grant_issue_template():
    from agents.funding_agent import _extract_application_url

    repository_url = "https://github.com/ipfs/devgrants"
    application_url = (
        "https://github.com/filecoin-project/devgrants/issues/new"
        "?labels=Open+Grant&template=open-grant-appolication.md"
    )
    content = (
        "Open Grants applications for funding are welcome in the following categories: "
        f"[ipfs/devgrants]({repository_url})\\n"
        "To submit an Open Grant proposal, "
        f"[create a new issue]({application_url}) using the Open Grants Proposal template."
    )

    assert _extract_application_url(content) == application_url


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
    assert opportunity.focus_area == "MEV"
    assert "rolling basis" in opportunity.evidence.get("status", "").lower()
    assert opportunity.funding_amount


def test_sbir_generic_browser_title_uses_specific_configured_topic_name():
    from agents.funding_agent import FundingDocument, analyze_document

    document = FundingDocument(
        organization="NSF SBIR Trusted Identity",
        title="our programs",
        source="https://www.sbir.gov/topics/1012406",
        source_type="html",
        content=(
            "Solicitation Status: Open. "
            "Trusted Identity and Identity Management research supports identity "
            "management and verifiable credentials. "
            "Due Date(s): November 4, 2026; March 4, 2027."
        ),
        year=2026,
    )

    opportunity = analyze_document(document)
    assert opportunity.program_name == "NSF SBIR Trusted Identity"


def test_eligibility_extractor_does_not_return_application_workflow_copy():
    from agents.funding_agent import _extract_section_value

    assert _extract_section_value(
        "Eligible applicants will then be invited to submit a Full Proposal.",
        ("eligibility", "who can apply", "eligible applicants", "eligible organizations"),
    ) is None
    assert _extract_section_value(
        "Eligibility: Pass/fail requirements related to the conditions of the call are assessed by Climate-KIC.",
        ("eligibility", "who can apply", "eligible applicants", "eligible organizations"),
    ) is None


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
        "Climate innovation grants. Grant recipients: 8 startups. Last Date to Apply – 27 May 2026.",
        "Climate-tech funding programme",
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


def test_broad_funding_catalogue_roots_are_not_individual_opportunities():
    from core.crawl4ai_web import _classify_page_stage_b, _is_generic_funding_hub

    samples = [
        (
            "https://fil.org/grants",
            "Filecoin Foundation Grants & Funding Opportunities",
            "The foundation awards grant funding. Submit a proposal, apply now, "
            "review eligibility, and check deadlines.",
        ),
        (
            "https://www.climate-kic.org/get-involved/open-calls/",
            "Climate KIC Open Calls 2027",
            "Browse open calls, grants, funding, eligibility, application instructions, "
            "and deadlines for multiple programmes.",
        ),
        (
            "https://www.sbir.gov/topics?status=Open",
            "Current SBIR topic results",
            "Open topics list funding amounts, eligibility, calls for proposals, "
            "deadlines, and application instructions across agencies.",
        ),
        (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals?isExactMatch=false&order=DESC&pageNumber=1&pageSize=50&sortBy=startDate&status=31094501%2C31094502",
            "Calls for Proposals | EU Funding & Tenders Portal",
            "Browse open and forthcoming calls for proposals, grant funding, "
            "eligibility requirements, applications, and deadlines.",
        ),
    ]

    for url, title, content in samples:
        assert _is_generic_funding_hub(url, title, content)
        assert not _classify_page_stage_b(url, title, content)


def test_filecoin_directory_is_not_forced_but_open_grants_readme_is_direct():
    from unittest.mock import patch
    from core.crawl4ai_web import crawl_funding_source

    directory = "https://fil.org/grants"
    program = (
        "https://raw.githubusercontent.com/filecoin-project/devgrants/master/"
        "Program%20Resources/Open%20Grants%20README.md"
    )

    def fake_fetch(url):
        if url == directory:
            title = "Filecoin Foundation Grants & Funding Opportunities"
            markdown = (
                "Filecoin Foundation awards grant funding for development projects. "
                "Applicants can submit a proposal, review eligibility, apply now, and check deadlines."
            )
        else:
            title = "Open Grants"
            markdown = "Programme evidence text."
        return {
            "html": "<html><body>Funding page</body></html>",
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
            "Filecoin Grants and Funding",
            directory,
            {
                "start_urls": [directory, program],
                "max_depth": 0,
                "max_pages": 2,
                "direct_page_urls": [program],
                "respect_robots_txt": False,
            },
            ["fil.org", "raw.githubusercontent.com"],
        )

    assert [item["url"] for item in result.opportunities] == [program]


def test_ecosystem_status_announcement_is_not_misclassified_as_a_funding_call():
    from core.crawl4ai_web import _classify_page_stage_b

    title = "Evolving an Ecosystem Builder Program"
    content = (
        "The blockchain ecosystem is continually evolving how it supports early and mid-stage projects. "
        "Existing arrangements under the builder programme are being concluded, while new commercial "
        "agreements are established on a case-by-case basis. The latest season of programme-specific token "
        "rewards marked the conclusion of those rewards, with claims ending July 7, 2026. "
        "This announcement is a program-status update, not an open application call."
    )
    assert _classify_page_stage_b(
        "https://funding.example.org/program-status", title, content
    ) is False


def test_collection_error_formatter_exposes_operation_url_and_reason():
    error = {
        "operation": "page_fetch",
        "url": "https://funding.example.org/previously-configured-route",
        "error": "HTTP 404",
    }
    assert _format_collection_error(error) == (
        "page_fetch | https://funding.example.org/previously-configured-route | HTTP 404"
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



def test_paused_applications_override_rolling_acceptance_language():
    from agents.funding_agent import determine_status

    content = (
        "Grant fellows are accepted on a rolling basis. "
        "Applications Paused."
    )
    assert determine_status(
        None, None, None, content, "Agentic Engineering Grants"
    ) == "CLOSED_CALL"


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


def test_superteam_directory_waits_for_client_rendered_grant_cards():
    from core.crawl4ai_web import _superteam_directory_wait_options

    options = _superteam_directory_wait_options(
        "https://superteam.fun/earn/opportunities/development-grants/"
    )
    assert "/earn/grants/" in options["wait_for"]
    assert "scrollTo" in options["js_code_before_wait"]
    assert options["delay_before_return_html"] == 1.0


def test_superteam_detail_page_does_not_wait_for_directory_cards():
    from core.crawl4ai_web import _superteam_directory_wait_options

    assert _superteam_directory_wait_options(
        "https://superteam.fun/earn/grants/agentic-engineering"
    ) == {}


def test_non_superteam_page_does_not_use_superteam_wait_options():
    from core.crawl4ai_web import _superteam_directory_wait_options

    assert _superteam_directory_wait_options(
        "https://solana.org/grants-funding"
    ) == {}


def test_specific_superteam_h1_precedes_about_section_and_open_badge_is_open():
    from agents.funding_agent import FundingDocument, analyze_document

    content = (
        "# Startup Accelerator Grant\n"
        "Grant | Open | Global\n"
        "## About DD.xyz by Webacy\n"
        "Get up to $10,000 in DD.xyz API credits.\n"
        "Apply for the grant now: "
        "[Apply](https://83chy35k9ea.typeform.com/to/Mgl4epqX)\n"
    )
    opportunity = analyze_document(FundingDocument(
        organization="Solana Grants and Funding",
        title="potentially dangerous token approvals",
        source="https://superteam.fun/earn/grants/startup-accelerator-grant",
        source_type="html",
        content=content,
        year=2026,
    ))

    assert opportunity.program_name == "Startup Accelerator Grant"
    assert opportunity.status == "OPEN_CALL"
    assert opportunity.application_url == "https://83chy35k9ea.typeform.com/to/Mgl4epqX"


def test_expired_climate_grant_deadline_is_not_a_funded_project():
    from agents.funding_agent import FundingDocument, analyze_document, extract_call_dates

    content = (
        "Climate innovation grant programme for early-stage startups in climate technology.\n"
        "Apply Here. _Last Date to Apply –_ _27 May 2026_.\n"
        "Climate innovation showcase. Grant Support: Up to Rs 20 Lakhs.\n"
        "Grant recipients: 8 startups.\n"
        "Previous programme awardees.\n"
    )
    _, deadline = extract_call_dates(content)
    assert deadline == "2026-05-27"

    opportunity = analyze_document(FundingDocument(
        organization="Example Climate Funding Programme",
        title="Climate-tech funding programme",
        source="https://funding.example.org/climate-call",
        source_type="html",
        content=content,
        year=2026,
    ))
    assert opportunity.status == "CLOSED_CALL"
    assert opportunity.opportunity_type == "FUNDING_CALL"


def test_flashbots_markdown_h1_wins_over_url_fallback_title():
    from agents.funding_agent import _extract_program_name

    content = (
        "# MEV Fellowship Grants\n"
        "The MEV fellowship is a grants program to support research.\n"
        "## About Flashbots Research\n"
        "Fellows are accepted on a rolling basis.\n"
    )
    assert _extract_program_name(
        content,
        "Extracted from https://raw.githubusercontent.com/flashbots/mev-research/main/grants.md",
    ) == "MEV Fellowship Grants"


def test_repeated_due_date_schedule_selects_nearest_future_round():
    from agents.funding_agent import extract_call_dates

    today = datetime.now(UTC).date()
    opening_date = today - timedelta(days=30)
    past_deadline = today - timedelta(days=10)
    next_deadline = today + timedelta(days=20)
    later_deadline = today + timedelta(days=80)

    def display_date(value):
        return f"{value.strftime('%B')} {value.day}, {value.year}"

    content = (
        f"Open Date: {display_date(opening_date)}. "
        f"3. Due Date(s) {display_date(past_deadline)}; "
        f"{display_date(next_deadline)}; {display_date(later_deadline)}. "
        f"4. Close Date: {display_date(later_deadline)}. "
        "Solicitation Status: Open."
    )
    opening, deadline = extract_call_dates(content)

    assert opening == opening_date.isoformat()
    assert deadline == next_deadline.isoformat()
    assert determine_status(None, opening, deadline, content) == "OPEN_CALL"


def test_explicit_deadline_beats_later_notification_milestone():
    from agents.funding_agent import extract_call_dates

    today = datetime.now(UTC).date()
    past_deadline = today - timedelta(days=4)
    future_notification = today + timedelta(days=14)
    content = (
        f"OPEN CALL\\nDEADLINE {past_deadline.isoformat()} 23:59 CET\\n"
        "Planned Date | Milestone\\n"
        f"{future_notification.strftime('%b %d, %Y')} | Notification of selected applicants"
    )

    opening, deadline = extract_call_dates(content)
    assert deadline == past_deadline.isoformat()
    assert determine_status(None, opening, deadline, content, "Climate resilience call") == "CLOSED_CALL"


def test_generic_sbir_apply_page_is_not_used_as_call_application_url():
    from agents.funding_agent import _extract_application_url

    content = (
        "[Apply](https://www.sbir.gov/apply) "
        "Solicitation Status: Open. "
        "[View Official Solicitation](https://www.nsf.gov/funding/opportunities/"
        "small-business-innovation-research-small-business-technology/nsf26-510/solicitation)"
    )
    assert _extract_application_url(content) is None


def test_specific_open_sbir_topic_uses_its_topic_page_not_generic_apply_guidance():
    from agents.funding_agent import _resolve_application_url

    source_url = "https://www.sbir.gov/topics/1012406"
    content = (
        "Solicitation Status: Open. "
        "[Apply](https://www.sbir.gov/apply) "
        "[View Official Solicitation](https://www.nsf.gov/funding/opportunities/"
        "small-business-innovation-research-small-business-technology/nsf26-510/solicitation)"
    )
    assert _resolve_application_url(content, source_url, "OPEN_CALL") == source_url


def test_eligibility_heading_is_not_misread_as_a_requirement_value():
    from agents.funding_agent import _extract_section_value

    content = (
        "## Eligibility and Final Selection\n"
        "This call has a one-stage assessment process.\n"
        "Eligibility: Organisations must be legally established in the eligible region."
    )
    assert _extract_section_value(content, ("eligibility", "who can apply")) == (
        "Organisations must be legally established in the eligible region."
    )


def test_sbir_generic_browser_title_is_replaced_with_the_topic_heading():
    from agents.funding_agent import FundingSource
    from agents.funding_collector import _normalize_raw_to_doc

    source = FundingSource(
        name="NSF SBIR Trusted Identity",
        url="https://www.sbir.gov/topics/1012406",
        tier="tier_3",
        direct_page=True,
    )
    document = _normalize_raw_to_doc(
        source,
        {
            "type": "html",
            "url": source.url,
            "title": "Our Programs",
            "content": (
                "# Topic\n"
                "## Trusted Identity and Identity Management\n"
                "Solicitation Status: Open"
            ),
        },
    )
    assert document.title == "Trusted Identity and Identity Management"


def test_unpunctuated_eligible_applicants_prose_is_still_extracted():
    from agents.funding_agent import _extract_section_value

    content = "Eligible applicants must be legally established in the United States."
    assert _extract_section_value(content, ("eligible applicants",)) == (
        "must be legally established in the United States."
    )
