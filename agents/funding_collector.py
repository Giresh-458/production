"""Funding collection orchestrator.

Routes each configured funding source to its appropriate acquisition method
(API or web) and ensures all collected records pass through the same
normalization pipeline:

    raw source record → FundingDocument → analyze_document → (doc, opportunity)

Every CollectionResult contains only normalized (FundingDocument, FundingOpportunity)
tuples, never raw crawler dicts.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, List, Optional, Iterator
from urllib.parse import urlparse

from core.schemas import CollectionResult
from agents.utils import cleaned_text
from core.structured_sources import (
    grantsgov_paginated_search,
    grantsgov_fetch_opportunity,
    github_paginated_issues,
    eu_funding_tenders_paginated_search,
    eu_funding_tenders_fetch_document,
    eu_funding_tenders_fetch_topic_details,
    eu_funding_tenders_fetch_reference_pdf_text,
    EU_TOPIC_DETAILS_URL_TEMPLATE,
    _EU_TOPIC_IDENTIFIER_RE,
)
from core.crawl4ai_web import crawl_funding_source, _reports_no_active_funding
from agents.funding_agent import (
    FundingSource,
    FundingDocument,
    FundingOpportunity,
    analyze_document,
    current_year,
    parse_date_string,
)

LOGGER = logging.getLogger("rif.funding_collector")


def _first_grantsgov_value(
    details: Any,
    hit: dict[str, Any],
    keys: tuple[str, ...],
) -> Any:
    """Return the first populated structured value, preferring the detail response."""
    for mapping in (details, hit):
        if not isinstance(mapping, dict):
            continue
        # The public fetchOpportunity response nests award and eligibility
        # metadata inside its "synopsis" object. Prefer that structured detail
        # object, but keep root-level and search-hit fallbacks for API variations.
        synopsis = mapping.get("synopsis")
        candidates = [synopsis, mapping] if isinstance(synopsis, dict) else [mapping]
        for candidate in candidates:
            for key in keys:
                value = candidate.get(key)
                if value is None:
                    continue
                if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "unknown"}:
                    continue
                return value
    return None


def _clean_grantsgov_text(value: Any) -> str:
    """Convert a Grants.gov text/HTML field to readable plain text."""
    if value is None:
        return ""
    text = unescape(str(value))
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _grantsgov_money_number(value: Any) -> str | None:
    """Normalize a numeric Grants.gov award field while retaining its magnitude."""
    text = _clean_grantsgov_text(value)
    text = re.sub(r"^(?:USD\s*)?\$?\s*", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?", text):
        return None
    return text


def _apply_grantsgov_structured_metadata(
    analysis: FundingOpportunity,
    details: Any,
    hit: dict[str, Any],
) -> None:
    """Prefer source-labelled Grants.gov award/eligibility fields over ambiguous prose."""
    floor_raw = _first_grantsgov_value(
        details, hit, ("awardFloorFormatted", "awardFloor")
    )
    ceiling_raw = _first_grantsgov_value(
        details, hit, ("awardCeilingFormatted", "awardCeiling")
    )
    estimated_raw = _first_grantsgov_value(
        details, hit, ("estimatedFundingFormatted", "estimatedFunding")
    )

    floor = _grantsgov_money_number(floor_raw)
    ceiling = _grantsgov_money_number(ceiling_raw)
    estimated = _grantsgov_money_number(estimated_raw)

    evidence = dict(analysis.evidence or {})

    # Preserve the official Grants.gov opportunity status separately from the
    # language-based status classifier. In search2, "posted" means the FOA is
    # announced and accepting applications; "forecasted" is not yet an FOA.
    source_status_raw = _first_grantsgov_value(
        details, hit, ("oppStatus", "opportunityStatus")
    )
    source_status = _clean_grantsgov_text(source_status_raw).lower()
    if source_status:
        evidence["source_status"] = source_status
        evidence["source_status_provenance"] = "Grants.gov API oppStatus field"
        status_map = {
            "posted": "OPEN_CALL",
            "open": "OPEN_CALL",
            "forecasted": "UPCOMING_CALL",
            "upcoming": "UPCOMING_CALL",
            "closed": "CLOSED_CALL",
            "archived": "CLOSED_CALL",
        }
        if source_status in status_map:
            analysis.status = status_map[source_status]

    if floor and ceiling:
        analysis.funding_amount = (
            f"${floor}–${ceiling} per award "
            "(Grants.gov award floor–ceiling; verify designation-specific limits in the solicitation)"
        )
        evidence["funding_amount"] = (
            f"Grants.gov structured metadata: award floor ${floor}; award ceiling ${ceiling}. "
            "These bounds may not replace designation-specific limits in the solicitation."
        )
    elif ceiling:
        analysis.funding_amount = (
            f"Up to ${ceiling} per award "
            "(Grants.gov award ceiling; verify the solicitation for category-specific limits)"
        )
        evidence["funding_amount"] = f"Grants.gov structured metadata: award ceiling ${ceiling}."

    if estimated:
        evidence["estimated_program_funding"] = (
            f"Grants.gov estimated program funding: ${estimated} "
            "(aggregate program metadata, not an individual-award amount)"
        )

    eligibility_raw = _first_grantsgov_value(
        details,
        hit,
        ("applicantEligibilityDesc", "applicantEligibilityDescription"),
    )
    eligibility = _clean_grantsgov_text(eligibility_raw)

    # Grants.gov exposes applicant categories as structured objects even when
    # no free-text eligibility description is supplied.
    if not eligibility:
        applicant_types = _first_grantsgov_value(
            details, hit, ("applicantTypes", "eligibleApplicantTypes")
        )
        if isinstance(applicant_types, list):
            descriptions = []
            for applicant_type in applicant_types:
                if isinstance(applicant_type, dict):
                    description = _clean_grantsgov_text(
                        applicant_type.get("description") or applicant_type.get("name")
                    )
                    if description and description not in descriptions:
                        descriptions.append(description)
                elif isinstance(applicant_type, str):
                    description = _clean_grantsgov_text(applicant_type)
                    if description and description not in descriptions:
                        descriptions.append(description)
            if descriptions:
                eligibility = "Eligible applicant types (Grants.gov): " + "; ".join(descriptions)
        elif isinstance(applicant_types, str):
            eligibility = _clean_grantsgov_text(applicant_types)

    # The prose extractor can accidentally label a fragment of Python's
    # serialized detail-response dictionary as eligibility (for example
    # "Desc': 'N/A ...', 'estSynopsisPostingDate': ..."). Never expose that
    # serialization as a real eligibility condition or evidence.
    def looks_serialized(value: Any) -> bool:
        candidate = str(value or "")
        return bool(
            re.search(r"(?i)\bDesc['\"]\s*:", candidate)
            or re.search(
                r"(?i)['\"](?:estSynopsisPostingDate|createTimeStamp|applicantTypes)['\"]\s*:",
                candidate,
            )
        )

    if eligibility and not looks_serialized(eligibility):
        analysis.eligibility = eligibility
        evidence["eligibility"] = eligibility
    elif looks_serialized(analysis.eligibility) or looks_serialized(evidence.get("eligibility")):
        analysis.eligibility = None
        evidence.pop("eligibility", None)

    # Always write back the copied evidence, even when cleanup removes its
    # final entry; otherwise the original malformed dict remains attached.
    analysis.evidence = evidence


def collect_auto_sources(
    sources: List[FundingSource],
    area: Optional[str] = None,
    warnings: Optional[List[str]] = None,
    is_test_mode: bool = False,
) -> Iterator[CollectionResult]:
    """Collect funding opportunities from all configured sources sequentially yielding results."""
    test_limits = None
    if is_test_mode:
        LOGGER.info("TEST MODE ENABLED. Filtering sources and applying caps.")
        test_limits = {"max_pages": 5, "max_detail_pages": 10, "max_documents": 5}
        test_source_names = [
            "Filecoin Grants and Funding",
            "Climate KIC Open Calls",
            "Ethereum ESP RFPs",
        ]
        sources = [s for s in sources if s.name in test_source_names]
        LOGGER.info("Test mode limited to %d sources", len(sources))

    for source in sources:
        LOGGER.info("Collecting funding opportunities from %s", source.name)
        coll = source.collection
        mode = coll.get("mode", "web")
        
        # Keep production source limits separate from test-only limits.
        source_limits = dict(coll.get("limits", {}))
        # For web discovery block backwards compatibility
        discovery = coll.get("discovery", {})
        if "max_pages" in discovery and "max_pages" not in source_limits:
            source_limits["max_pages"] = discovery["max_pages"]

        active_test_limits = None
        production_source_limits = source_limits or None
        if is_test_mode:
            # Preserve the existing test-mode precedence/diagnostic behavior.
            active_test_limits = dict(test_limits or {})
            if "max_pages" in source_limits:
                active_test_limits["max_pages"] = source_limits["max_pages"]
            if "max_documents" in source_limits:
                active_test_limits["max_documents"] = source_limits["max_documents"]
            production_source_limits = None
            
        try:
            if mode == "api":
                yield _collect_api_source(
                    source, area, warnings,
                    active_test_limits, production_source_limits
                )
            elif mode == "url_validation":
                result = CollectionResult(
                    source_id=source.name,
                    method="url_validation",
                    final_status="FAIL",
                    failed_urls=[source.url],
                    errors=["url_validation mode requires manual check"]
                )
                if warnings is not None:
                    warnings.append(f"{source.name} requires manual url_validation check")
                yield result
            else:
                yield _collect_web_source(
                    source, area, warnings,
                    active_test_limits, production_source_limits
                )
        except Exception as e:
            LOGGER.exception("Collection failed for source %s", source.name)
            result = CollectionResult(
                source_id=source.name,
                method=mode,
                final_status="FAIL",
            )
            result.errors.append({
                "url": source.url,
                "operation": "source_collection",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if warnings is not None:
                warnings.append(f"Collection failed for {source.name}: {e}")
            yield result


def _trim_page_navigation_and_footer(content: str) -> str:
    """Remove common Markdown-rendered site chrome before extracting call evidence.

    Crawl4AI can include the entire header/navigation/footer in its Markdown output.
    Domain scoring over that text may classify an unrelated opportunity by the
    publisher's own mission statement (for example, a deep-tech call hosted by a
    climate organisation). Preserve the call body and its links, but start at the
    last skip-to-content link and discard a clearly labelled copyright/footer tail.
    """
    text = str(content or "")
    if not text.strip():
        return text

    skip_links = list(re.finditer(
        r"(?i)\[skip to (?:main )?content\]\([^)]+\)\s*",
        text,
    ))
    if skip_links:
        text = text[skip_links[-1].start():]
        text = re.sub(
            r"(?i)^\[skip to (?:main )?content\]\([^)]+\)\s*",
            "",
            text,
            count=1,
        )

    footer_patterns = (
        r"(?im)^\s*(?:©|copyright)\s*[^\n]{0,180}\b(?:19|20)\d{2}\b",
        r"(?im)^\s*#{1,4}\s*(?:quick links|quicks links|footer navigation|newsletter|Europe[’']s leading\b.*\bclimate innovation agency\b.*\bcommunity)\s*$",
    )
    footer_starts = [
        match.start()
        for pattern in footer_patterns
        for match in re.finditer(pattern, text)
    ]
    if footer_starts:
        text = text[:min(footer_starts)]

    return text.strip()


def _normalize_raw_to_doc(
    source: FundingSource,
    raw: dict,
) -> FundingDocument:
    """Convert a raw crawler result dict into a FundingDocument."""
    raw_type = raw.get("type", "html")
    url = raw.get("url", source.url)
    title = str(raw.get("title", "") or "").strip()
    content = str(raw.get("content", "") or "")
    if raw_type != "pdf":
        content = _trim_page_navigation_and_footer(content)

    # Some topic publishers expose site chrome ("Our Programs") as the browser
    # title. Prefer a concrete content heading before naming an output record.
    generic_titles = {
        "our programs", "programs", "topic", "home", "welcome", "funding",
        "grants", "grant", "overview", "about", "details", "description",
        "all cookies under each consent category below", "cookie settings",
        "privacy preferences", "consent preferences", "your privacy choices",
        "manage cookie preferences", "manage consent preferences",
    }
    if title.casefold().rstrip(" .:-") in generic_titles:
        for heading_match in re.finditer(r"(?m)^\s{0,4}#{1,4}\s+(.{3,180}?)\s*$", content):
            candidate = cleaned_text(heading_match.group(1)).rstrip(" .:-")
            candidate = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", candidate)
            if candidate.casefold().rstrip(" .:-") in generic_titles:
                continue
            if candidate.casefold().startswith(("solicitation status", "funding agency")):
                continue
            title = candidate
            break
        if title.casefold().rstrip(" .:-") in generic_titles and source.direct_page:
            title = source.name

    if raw_type == "pdf":
        source_type = "PDF Document"
        if not title:
            title = urlparse(url).path.split("/")[-1].replace("-", " ").replace("_", " ").strip()
    else:
        source_type = "Web Traversal"
        if not title:
            title = f"Extracted from {url}"

    return FundingDocument(
        organization=source.name,
        title=title,
        source=url,
        source_type=source_type,
        content=content[:200000],  # bounded, consistent with document readers
        year=current_year(),
    )


def _collect_api_source(
    source: FundingSource,
    area: Optional[str],
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect from an API source (Grants.gov or GitHub)."""
    provider = source.collection.get("provider", "")
    result = CollectionResult(source_id=source.name, method="api")

    if provider == "grants_gov":
        return _collect_grants_gov(
            source, area, result, warnings, test_limits, source_limits
        )
    elif provider == "github":
        return _collect_github(
            source, result, warnings, test_limits, source_limits
        )
    elif provider == "eu_funding_tenders":
        return _collect_eu_funding_tenders(
            source, result, warnings, test_limits, source_limits
        )
    else:
        result.final_status = "FAIL"
        result.errors.append({
            "url": source.url,
            "operation": "api_provider_lookup",
            "error": f"Unknown API provider: {provider}",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return result


def _collect_grants_gov(
    source: FundingSource,
    area: Optional[str],
    result: CollectionResult,
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect from Grants.gov API with genuine pagination exhaustion."""
    query = "research cyber physical blockchain identity infrastructure"
    api_result = grantsgov_paginated_search(
        query, test_limits=test_limits, source_limits=source_limits
    )

    hits = api_result["hits"]
    if test_limits and "max_documents" in test_limits:
        hits = hits[:test_limits["max_documents"]]
    if source_limits and "max_documents" in source_limits:
        hits = hits[:source_limits["max_documents"]]
        
    result.api_pages_fetched = api_result["pages_fetched"]
    result.api_expected_records = api_result["total_expected"]
    result.api_records_received = len(hits)
    result.pagination_complete = api_result["pagination_complete"]
    result.discovery_exhausted = bool(api_result["pagination_complete"])
    result.pagination_detected = True
    result.records_discovered = len(hits)

    for err in api_result.get("errors", []):
        result.errors.append({
            "url": "https://api.grants.gov/v1/api/search2",
            "operation": "paginated_search",
            "error": err,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        if "TEST_TRUNCATED" in err:
            result.truncated = True
            result.final_status = "TEST_TRUNCATED"
        elif "SOURCE_LIMIT_REACHED" in err:
            result.truncated = True
        if warnings is not None:
            warnings.append(f"Grants.gov: {err}")

    if not result.pagination_complete:
        result.truncated = True

    # Normalize each hit into FundingDocument → analyze_document
    seen_ids: set[str] = set()
    for hit in hits:
        opp_id = str(hit.get("id", ""))
        if not opp_id:
            continue
        if opp_id in seen_ids:
            result.duplicates_removed += 1
            continue
        seen_ids.add(opp_id)

        # Fetch detail if configured; keep the structured payload for field-specific normalization.
        detail_content = ""
        details: Any = None
        if source.collection.get("detail_fetch", False) and opp_id:
            details = grantsgov_fetch_opportunity(opp_id)
            if details:
                result.detail_pages_fetched += 1
                detail_content = str(details)
            else:
                result.detail_pages_failed += 1

        # Build content from API data
        title = hit.get("title", "")
        agency = hit.get("agencyName", "")
        number = hit.get("number", "")
        open_date = hit.get("openDate", "")
        close_date = hit.get("closeDate", "")
        opp_status = hit.get("oppStatus", "")
        description = detail_content or str(hit)

        content = (
            f"Title: {title}\n"
            f"Agency: {agency}\n"
            f"Number: {number}\n"
            f"Open Date: {open_date}\n"
            f"Close Date: {close_date}\n"
            f"Status: {opp_status}\n"
            f"Details: {description}\n"
        )

        doc = FundingDocument(
            organization=source.name,
            title=title or f"Grants.gov Opportunity {opp_id}",
            source=f"https://www.grants.gov/search-results-detail/{opp_id}",
            source_type="Official API",
            content=content[:50000],
            year=current_year(),
        )

        try:
            analysis = analyze_document(doc)
            _apply_grantsgov_structured_metadata(analysis, details, hit)
            result.opportunities.append((doc, analysis))
            result.records_valid += 1
        except Exception as e:
            result.records_failed += 1
            result.errors.append({
                "url": doc.source,
                "operation": "analyze_document",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if warnings is not None:
                warnings.append(f"Grants.gov analysis failed for {opp_id}: {e}")

    # Final status
    if result.errors and not result.opportunities:
        result.final_status = "FAIL"
    elif result.truncated:
        if result.final_status != "TEST_TRUNCATED":
            result.final_status = "PARTIAL"
    elif result.opportunities:
        result.final_status = "VERIFIED"
    else:
        result.final_status = "NO_ACTIVE_OPPORTUNITIES"

    if test_limits:
        print("\nGrants.gov API diagnostic")
        print("-------------------------")
        # Collection-limit warnings are not API-envelope failures. Only API
        # transport/shape errors should affect the envelope diagnostic.
        api_shape_errors = [
            err["error"] for err in result.errors
            if any(marker in err["error"] for marker in (
                "API request failed", "Grants.gov returned",
                "non-JSON/non-dict", "malformed", "Pagination stalled",
                "repeated page content",
            ))
        ]
        app_err = api_shape_errors[0] if api_shape_errors else "none"
        request_failed = any("API request failed" in err["error"] for err in result.errors)

        if request_failed:
            http_status = "unknown (request failed; exact status not retained)"
            json_response = "unknown"
            envelope = "unknown"
            dtype = "unknown"
        else:
            http_status = "successful (exact HTTP code not retained)"
            json_response = (
                "unknown"
                if any("non-JSON/non-dict response" in err for err in api_shape_errors)
                else "yes"
            )
            envelope = "invalid" if api_shape_errors else "valid"
            dtype = "unknown" if api_shape_errors else "dict"

        print(f"HTTP status: {http_status}")
        print(f"JSON response: {json_response}")
        print(f"Envelope: {envelope}")
        print(f"data type: {dtype}")
        print(f"API/application error: {app_err}")
        print(f"records retrieved: {result.records_discovered}")
        print(f"pagination_complete: {result.pagination_complete}")
        print(f"truncated: {result.truncated}")
        print(f"final_status: {result.final_status}\n")

    return result


def _eu_metadata_value(metadata: dict, *keys: str) -> str | None:
    """Return the first meaningful scalar value in an EU API metadata record."""
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, list):
            value = next((item for item in value if item not in (None, "")), None)
        if isinstance(value, dict):
            value = value.get("label") or value.get("name") or value.get("title") or value.get("value")
        if value is None:
            continue
        cleaned = _clean_grantsgov_text(value)
        if cleaned and cleaned.casefold() not in {"null", "none", "unknown", "n/a"}:
            return cleaned
    return None


def _eu_application_url(metadata: dict, detail_url: str) -> tuple[str | None, str | None]:
    """Resolve an EU call's application entry point without inventing external URLs.

    Prefer an explicit publisher-supplied submission URL. When the Commission API
    omits one, its own topic-details page is the official landing page from which
    applicants can open the submission system.
    """
    explicit = _eu_metadata_value(
        metadata,
        "applicationUrl",
        "applicationURL",
        "submissionUrl",
        "submissionURL",
        "applyUrl",
        "applicationLink",
        "submissionLink",
        "applicationPortalUrl",
    )
    if explicit:
        parsed_explicit = urlparse(explicit)
        if parsed_explicit.scheme.lower() == "https" and parsed_explicit.netloc:
            return explicit, "EU Search API application/submission URL metadata"

    parsed = urlparse(detail_url)
    if (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() in {"ec.europa.eu", "www.ec.europa.eu"}
        and parsed.path.startswith(
            "/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
        )
    ):
        landing_page = parsed._replace(
            scheme="https",
            netloc=parsed.netloc.lower(),
            fragment="",
        ).geturl()
        return landing_page, "Official EU Funding & Tenders topic-details submission landing page"

    return None, None


def _extract_eu_eligibility_from_html(value: Any) -> str | None:
    """Extract explicit applicant criteria from the portal's rich HTML fields.

    A beneficiary-administration field can contain general process guidance rather
    than eligibility. Return only paragraphs that state applicant types or conditions.
    """
    if value is None:
        return None
    if isinstance(value, list):
        raw = "\n".join(str(part) for part in value if part not in (None, ""))
    else:
        raw = str(value)
    if not raw.strip():
        return None

    raw = unescape(raw)
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(
        r"(?i)</(?:p|li|div|h[1-6]|tr|section|article|br)\s*>|<br\s*/?>",
        "\n",
        raw,
    )
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    paragraphs = [
        re.sub(r"\s+", " ", part).strip(" \t\r\n:;-")
        for part in raw.splitlines()
    ]
    paragraphs = [part for part in paragraphs if part]
    if not paragraphs:
        cleaned = _clean_grantsgov_text(raw)
        paragraphs = [cleaned] if cleaned else []

    explicit_patterns = (
        r"\beligible applicants?\b.{0,180}\b(?:must be|must include|may apply|can apply|are eligible to|are eligible for|are limited to)\b",
        r"\beligible applicants?\s+(?:are|include)\s+(?:legal entities?|natural persons?|SMEs?|small and medium[- ]sized enterprises|public\s+research organisations?|public\s+research organizations?|nuclear research facilities|research facilities?|research institutions?|research organisations?|research organizations?|universit(?:y|ies)|public bodies|non[- ]profit organisations?|non[- ]profit organizations?|start[- ]ups?|students?|individual developers?|research teams?|companies|businesses|organisations|organizations)\b.{0,180}",
        r"\beligible applicants?\s*[:\-]\s*.{15,}",
        r"\bapplicants?\s+(?:(?:must|are required to)\s+(?:be|include|have|hold|establish|register|reside|operate|form|belong|qualify|maintain|apply)|may\s+(?:apply|be eligible|submit)|can\s+apply)\b",
        r"\bapplications?\s+(?:are|is)\s+open\s+to\b.{15,}",
        r"\bwho can apply\b.{20,}",
        r"\bonly\b.{0,140}\b(?:may apply|are eligible|can apply)\b",
        r"\b(?:SMEs?|small and medium[- ]sized enterprises|research organisations?|research organizations?|universit(?:y|ies)|public bodies|non[- ]profit organisations?|non[- ]profit organizations?|start[- ]ups?|students?)\b.{0,180}\b(?:eligible|may apply|can apply|may submit|can submit|submit grant applications?|must|provided that|support(?:ed)?|are required)\b",
        r"\b(?:legal entities|natural persons|public entities|private entities)\b.{0,140}\b(?:eligible|may apply|can apply|established|registered)\b",
        r"\bconsortiums?\s+must\s+include\b.{20,}",
        r"\b(?:at least|minimum of)\s+\d+\s+(?:independent\s+)?(?:legal entities|partners|beneficiaries|organisations|organizations)\b.{15,}",
        r"\b(?:applicants|beneficiaries|partners)\s+must\s+be\s+(?:established|registered|located|based|resident|legally established)\b.{10,}",
        r"\bopen to all\s+(?:legal entities|organisations|organizations|public bodies|SMEs|research organisations)\b.{5,}",
    )
    selected: list[str] = []
    for paragraph in paragraphs:
        if any(re.search(pattern, paragraph, flags=re.IGNORECASE) for pattern in explicit_patterns):
            if not re.search(
                r"\beligibility criteria (?:specified|set out|described) in the call\b",
                paragraph,
                flags=re.IGNORECASE,
            ):
                selected.append(paragraph[:1200])
        if len(selected) >= 4:
            break

    if not selected:
        return None
    return " ".join(dict.fromkeys(selected))[:4000]


def _eu_html_to_text(value: Any) -> str:
    raw = unescape(str(value or ""))
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(?:p|li|div|h[1-6]|tr|section|article)\s*>|<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _eu_epoch_ms_to_date(value: Any) -> date | None:
    try:
        millis = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _eu_is_portal_topic(item: dict, metadata: dict, url: str) -> bool:
    """True for Horizon-style topics that have an official topicDetails document."""
    record_type = _eu_metadata_value(metadata, "type") or _eu_metadata_value(item, "type")
    lowered = str(url or "").casefold()
    return record_type == "1" or "/topic-details/" in lowered or "/topicdetails/" in lowered


def _eu_topic_details_facts(
    details: dict, identifier: str, *, today: date | None = None
) -> dict[str, Any]:
    """Interpret the official topicDetails document without inventing missing values."""
    reference_day = today or datetime.now(UTC).date()
    actions = [a for a in (details.get("actions") or []) if isinstance(a, dict)]
    status_labels: list[str] = []
    action_types: list[str] = []
    opening_dates: list[date] = []
    deadline_dates: list[date] = []
    for action in actions:
        status = action.get("status") if isinstance(action.get("status"), dict) else {}
        label = str(status.get("abbreviation") or status.get("description") or "").strip()
        if label:
            status_labels.append(label)
        for action_type in action.get("types") or []:
            if isinstance(action_type, dict) and action_type.get("typeOfAction"):
                action_types.append(str(action_type["typeOfAction"]).strip())
        opening = _eu_epoch_ms_to_date(action.get("plannedOpeningDate"))
        if opening:
            opening_dates.append(opening)
        for raw_deadline in action.get("deadlineDates") or []:
            parsed = _eu_epoch_ms_to_date(raw_deadline)
            if parsed:
                deadline_dates.append(parsed)

    folded = [label.casefold() for label in status_labels]
    if any(label.startswith("open") for label in folded):
        status_label = "Open"
    elif any("forthcoming" in label for label in folded):
        status_label = "Forthcoming"
    elif any("closed" in label for label in folded):
        status_label = "Closed"
    else:
        status_label = None
    deadline_models = set()
    budget_entries = ((details.get("budgetOverviewJSONItem") or {}).get("budgetTopicActionMap") or {})
    if isinstance(budget_entries, dict):
        for entries in budget_entries.values():
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and str(entry.get("action") or "").casefold().startswith(
                    f"{identifier.casefold()} "
                ):
                    if entry.get("deadlineModel"):
                        deadline_models.add(str(entry["deadlineModel"]).strip().casefold())
    if "two-stage" in deadline_models or identifier.casefold().endswith("-two-stage"):
        # Only the first-stage deadline is open to new applicants; the second
        # stage is by invitation. A passed stage-1 deadline means the call is
        # closed to new proposals even though a later date exists.
        next_deadline = min(deadline_dates) if deadline_dates else None
    else:
        # Single-stage topics with several cut-offs: the actionable deadline is
        # the next unexpired cut-off; if all have passed, report the latest.
        future_deadlines = sorted(d for d in deadline_dates if d >= reference_day)
        next_deadline = future_deadlines[0] if future_deadlines else (max(deadline_dates) if deadline_dates else None)

    submission_links = []
    for link in details.get("links") or []:
        if not isinstance(link, dict):
            continue
        link_url = str(link.get("url") or "").strip()
        parsed = urlparse(link_url)
        if parsed.scheme == "https" and (parsed.hostname or "").casefold().endswith("ec.europa.eu"):
            submission_links.append(link_url)

    budget_parts: list[str] = []
    budget_map = ((details.get("budgetOverviewJSONItem") or {}).get("budgetTopicActionMap") or {})
    prefix = f"{identifier.casefold()} "
    if isinstance(budget_map, dict):
        for entries in budget_map.values():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                if not str(entry.get("action") or "").casefold().startswith(prefix):
                    continue
                year_budget = sum(
                    float(v) for v in (entry.get("budgetYearMap") or {}).values()
                    if re.fullmatch(r"\d+(?:\.\d+)?", str(v))
                )
                min_c, max_c = entry.get("minContribution"), entry.get("maxContribution")
                part = []
                if year_budget:
                    part.append(f"topic budget EUR {year_budget:,.0f}")
                if min_c and max_c and str(min_c) == str(max_c):
                    part.append(f"EU contribution per project EUR {float(min_c):,.0f}")
                elif min_c or max_c:
                    part.append(
                        f"EU contribution per project EUR {float(min_c or 0):,.0f}-{float(max_c or 0):,.0f}"
                    )
                if entry.get("expectedGrants"):
                    part.append(f"expected grants {entry['expectedGrants']}")
                if part:
                    budget_parts.append("; ".join(part))

    return {
        "status_label": status_label,
        "status_labels": status_labels,
        "action_types": list(dict.fromkeys(action_types)),
        "opening_date": min(opening_dates).isoformat() if opening_dates else None,
        "deadline": next_deadline.isoformat() if next_deadline else None,
        "deadline_model": ", ".join(sorted(deadline_models)) or None,
        "description": _eu_html_to_text(details.get("description")),
        "conditions_html": str(details.get("conditions") or ""),
        "submission_links": list(dict.fromkeys(submission_links)),
        "budget": " | ".join(dict.fromkeys(budget_parts)) or None,
        "document_url": str(details.get("_document_url") or ""),
    }


def _eu_action_type_research_intent(action_types: list[str]) -> tuple[str | None, str | None]:
    """Classify research intent from the publisher's official Type of Action.

    Horizon Europe assigns every topic an action type. Research and Innovation
    Actions (RIA), ERC frontier-research grants and MSCA research/training actions
    fund research; Coordination and Support Actions (networking/coordination),
    Innovation Actions (close-to-market), Pre-Commercial Procurement / PPI (public
    buyers procuring R&D) and co-fund actions do not fund applicants to carry out
    research. The official classification outranks keyword matches in the
    description, which previously labelled a PCP topic that states it "does not
    provide direct funding ... to perform R&D" as research. Returns (None, None)
    when no action type is published.
    """
    cleaned = [re.sub(r"\s+", " ", str(value)).strip() for value in action_types if str(value).strip()]
    if not cleaned:
        return None, None
    research = [
        value for value in cleaned
        if re.search(r"(?:^|[-\s])RIA\b|research and innovation actions?", value, flags=re.IGNORECASE)
        or (re.search(r"(?:^|-)ERC\b", value) and not re.search(r"\bPOC\b|proof of concept", value, re.IGNORECASE))
        or re.search(r"(?:^|-)(?:TMA-)?MSCA\b", value)
    ]
    if research:
        return "research", f"Official EU type of action: {research[0]}"
    return "non_research", (
        f"Official EU type of action: {'; '.join(cleaned)} (not a research action type)"
    )


_EU_ANNEX_ELIGIBILITY_PATTERNS = (
    # Countries whose legal entities are eligible for funding: Member States,
    # OCTs and Associated Countries (the list ends before the note on countries
    # negotiating association). Fallback: the Member State sentence alone.
    r"To be eligible for funding,\s*applicants must be established in[^:]{0,200}:"
    r".{0,2500}?(?=\s*Other third countries may become associated)",
    r"To be eligible for funding,\s*applicants must be established in[^:]{0,200}:"
    r".{0,900}?Member States of the European Union[^.]{0,400}\.",
    # Minimum consortium composition for collaborative actions. PDF text
    # extraction can split words ("i n"), so match whitespace loosely.
    r"Unless otherwise provided for in the specific call(?:/topic)? conditions,\s*only legal entities "
    r"forming a consortium are eligible to participate.{0,40}?provided that the consortium includes"
    r".{0,500}?(?:Member States or Associated Countries|Associated Countries)\.",
)


_EU_CONDITION_BOILERPLATE = (
    r"described in Annex [A-G]\b[^.]*\.?",
    r"A number of non-EU/non-Associated Countries that are not automatically eligible for funding "
    r"have made specific provisions[^.]*\.",
    r"See the information in the Horizon Europe Programme Guide\s*\.?",
)


def _eu_topic_specific_eligibility(conditions_html: str) -> str | None:
    """Return topic-specific eligibility text from the official conditions, verbatim.

    Horizon topic conditions list "2. Eligible countries" and "3. Other
    eligibility conditions". Most of that text only points to Annex B, but some
    topics add binding criteria (observed live on HORIZON-NATURE-2027-02-02:
    "The following additional eligibility criteria apply: The proposals must
    apply the multi-actor approach."). Those criteria must travel with the call;
    standard cross-references and guidance sentences are dropped.
    """
    flat = re.sub(r"\s+", " ", _eu_html_to_text(conditions_html or "")).strip()
    sections = []
    for match in re.finditer(
        r"\b[23]\.\s*(?:Eligible countries|Other Eligib\w* Conditions?)\s*:?\s*(.*?)(?=\s\d[a-z]?\.\s+[A-Z]|$)",
        flat,
        flags=re.IGNORECASE,
    ):
        body = match.group(1)
        for pattern in _EU_CONDITION_BOILERPLATE:
            body = re.sub(pattern, " ", body, flags=re.IGNORECASE)
        body = re.sub(r"\s+", " ", body).strip(" .;:-")
        if len(body.split()) >= 4:
            sections.append(body.rstrip(".") + ".")
    if not sections:
        return None
    return "Topic-specific eligibility conditions: " + " ".join(dict.fromkeys(sections))


def _eu_resolve_annex_eligibility(conditions_html: str) -> tuple[str | None, str | None, str | None]:
    """Resolve eligibility that a topic's official conditions delegate to Annex B.

    Horizon Europe topic pages state "Eligible Countries: described in Annex B of
    the Work Programme General Annexes" and link the official annex PDF. The
    applicant criteria therefore exist in a publisher-referenced document. This
    resolves that exact link and returns the verbatim Annex B passages, or
    ``(None, url, error)`` when the reference cannot be verified. Nothing is
    inferred when the topic does not cite Annex B for eligibility.
    """
    conditions_text = _eu_html_to_text(conditions_html)
    if not re.search(r"(?is)eligib.{0,240}?Annex\s+B", conditions_text):
        return None, None, None
    annex_url = None
    for match in re.finditer(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', conditions_html or ""):
        anchor = re.sub(r"<[^>]+>", " ", match.group(2))
        if re.search(r"\bAnnex\s+B\b", unescape(anchor)):
            annex_url = unescape(match.group(1)).strip()
            break
    if not annex_url:
        return None, None, "topic conditions cite Annex B for eligibility without an annex link"
    try:
        annex_text = eu_funding_tenders_fetch_reference_pdf_text(annex_url)
    except Exception as exc:  # recorded by the caller; eligibility stays unknown
        return None, annex_url, str(exc)
    flat = re.sub(r"\s+", " ", annex_text.replace("�", "•"))
    passages = []
    country_rule_found = False
    for index, pattern in enumerate(_EU_ANNEX_ELIGIBILITY_PATTERNS):
        if index == 1 and country_rule_found:
            continue  # fallback country pattern only when the full list was not found
        match = re.search(pattern, flat, flags=re.IGNORECASE)
        if match:
            passages.append(match.group(0).strip())
            if index in {0, 1}:
                country_rule_found = True
    if not passages:
        return None, annex_url, "Annex B eligibility passages were not found in the cited annex"
    return " ".join(passages)[:2400], annex_url, None


def _record_eu_filter(result: CollectionResult, reason: str, title: str, url: str) -> None:
    """Record a deliberate data-quality filter separately from an acquisition failure."""
    result.records_filtered += 1
    result.records_filtered_by_reason[reason] = (
        result.records_filtered_by_reason.get(reason, 0) + 1
    )
    LOGGER.info("Filtered EU funding record %r (%s): %s", title, reason, url)


def _eu_status_to_internal(raw_status: str | None) -> str | None:
    status = re.sub(r"[^a-z0-9]+", "", (raw_status or "").casefold())
    if not status:
        return None
    if "forthcoming" in status or "upcoming" in status or status == "31094501":
        return "UPCOMING_CALL"
    if "openforsubmission" in status or status in {"open", "31094502"} or status.startswith("open"):
        return "OPEN_CALL"
    if "closed" in status or status == "31094503":
        return "CLOSED_CALL"
    return None


def _eu_effective_status(
    raw_status: str | None,
    deadline_date: str | None,
    *,
    today: date | None = None,
) -> tuple[str | None, str | None]:
    """Apply a conservative expiry guard to potentially stale portal status metadata.

    The source status is retained separately as provenance. If the API still marks
    a record open/forthcoming after its explicit deadline has passed, use
    CLOSED_CALL as the effective status and expose the inconsistency in evidence.
    Missing or unparseable deadlines do not override the publisher's status.
    """
    status = _eu_status_to_internal(raw_status)
    if status not in {"OPEN_CALL", "UPCOMING_CALL"} or not deadline_date:
        return status, None

    try:
        deadline = date.fromisoformat(deadline_date)
    except (TypeError, ValueError):
        return status, None

    reference_day = today or datetime.now(UTC).date()
    if deadline < reference_day:
        warning = (
            f"EU Search API reports source status {raw_status!r}, but the explicit "
            f"deadline {deadline.isoformat()} is before {reference_day.isoformat()}; "
            "effective status conservatively set to CLOSED_CALL pending source review."
        )
        return "CLOSED_CALL", warning
    return status, None


def _eu_query_budget(
    *,
    search_texts: list[str],
    page_size: int,
    max_pages: int,
    max_documents: int,
) -> dict[str, int]:
    """Allocate bounded EU API budgets fairly across all configured search terms.

    Every configured query receives the same floor allocation. Remainders are
    reported (rather than used to drop query terms), and callers must keep a
    source PARTIAL when any query's API results are not exhausted.
    """
    query_count = len(search_texts)
    if query_count <= 0:
        raise ValueError("At least one EU Funding & Tenders search term is required")
    safe_page_size = max(1, min(int(page_size), 100))
    safe_max_pages = max(query_count, int(max_pages))
    safe_max_documents = max(query_count, int(max_documents))
    pages_per_query = safe_max_pages // query_count
    documents_per_query = min(
        safe_page_size * pages_per_query,
        safe_max_documents // query_count,
    )
    return {
        "query_count": query_count,
        "pages_per_query": pages_per_query,
        "documents_per_query": max(1, documents_per_query),
        "unallocated_pages": safe_max_pages - pages_per_query * query_count,
        "unallocated_documents": safe_max_documents
        - max(1, documents_per_query) * query_count,
    }


def _collect_eu_funding_tenders(
    source: FundingSource,
    result: CollectionResult,
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect only open/forthcoming EU calls from the Commission Search API."""
    api_config = source.collection.get("api", {}) or {}
    configured_limits = source.collection.get("limits", {}) or {}
    query_values = api_config.get("search_texts") or ["*"]
    configured_search_texts = list(dict.fromkeys(
        str(value).strip() for value in query_values if str(value).strip()
    )) or ["*"]

    def _effective_limit(name: str, default: int) -> int:
        values = [
            limits.get(name)
            for limits in (configured_limits, source_limits or {}, test_limits or {})
            if isinstance(limits, dict) and limits.get(name) is not None
        ]
        return max(1, min(int(value) for value in values)) if values else default

    max_pages_budget = _effective_limit("max_pages", 5)
    max_documents_budget = _effective_limit("max_documents", 250)
    page_size = max(1, min(int(api_config.get("page_size", 50)), 100))

    # Test budgets may intentionally be smaller than the number of production
    # terms. Only truncate the query list in explicitly bounded test mode;
    # production must attempt every configured search term.
    if test_limits and max_pages_budget < len(configured_search_texts):
        search_texts = configured_search_texts[:max_pages_budget]
        omitted = configured_search_texts[max_pages_budget:]
        warnings_text = (
            f"TEST_TRUNCATED: Test query budget is {max_pages_budget}; "
            f"skipped {len(omitted)} configured EU domain query/queries"
        )
    else:
        search_texts = configured_search_texts
        warnings_text = None

    query_budget = _eu_query_budget(
        search_texts=search_texts,
        page_size=page_size,
        max_pages=max_pages_budget,
        max_documents=max_documents_budget,
    )
    pages_per_query = query_budget["pages_per_query"]
    documents_per_query = query_budget["documents_per_query"]

    aggregated_records: list[dict] = []
    aggregated_errors: list[str] = []
    query_totals: list[int] = []
    pages_fetched = 0
    raw_records_received = 0
    all_queries_complete = warnings_text is None
    seen_search_refs: set[str] = set()
    seen_search_urls: set[str] = set()

    for search_text in search_texts:
        remaining_budget = max_documents_budget - len(aggregated_records)
        if remaining_budget <= 0:
            all_queries_complete = False
            aggregated_errors.append(
                f"{'TEST_TRUNCATED' if test_limits else 'SOURCE_LIMIT_REACHED'}: "
                f"Global max documents reached ({max_documents_budget}) before query {search_text!r}"
            )
            break

        query_document_cap = min(documents_per_query, remaining_budget)
        query_page_cap = min(pages_per_query, max(1, (query_document_cap + page_size - 1) // page_size))
        query_limits = {
            "max_pages": query_page_cap,
            "max_documents": query_document_cap,
        }
        query_result = eu_funding_tenders_paginated_search(
            types=[str(value) for value in api_config.get("types", ["1", "2", "8"])],
            statuses=[str(value) for value in api_config.get("statuses", ["31094501", "31094502"])],
            search_text=search_text,
            page_size=min(page_size, query_document_cap),
            test_limits=query_limits if test_limits else None,
            source_limits=None if test_limits else query_limits,
        )
        pages_fetched += int(query_result.get("pages_fetched", 0) or 0)
        raw_records_received += int(
            query_result.get("raw_records_received", len(query_result.get("results", []))) or 0
        )
        if query_result.get("total_expected") is not None:
            try:
                query_totals.append(int(query_result["total_expected"]))
            except (TypeError, ValueError):
                pass
        if not query_result.get("pagination_complete"):
            all_queries_complete = False

        for error in query_result.get("errors", []):
            aggregated_errors.append(f"query={search_text!r}: {error}")

        for item in query_result.get("results", []):
            if not isinstance(item, dict):
                continue
            reference = str(item.get("reference") or "").strip()
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            raw_url = item.get("url") or metadata.get("url") or ""
            if isinstance(raw_url, list):
                raw_url = next((value for value in raw_url if value), "")
            canonical_url = ""
            if str(raw_url).strip().startswith("https://"):
                parsed = urlparse(str(raw_url).strip())
                canonical_url = parsed._replace(
                    scheme=parsed.scheme.lower(),
                    netloc=parsed.netloc.lower(),
                    path=parsed.path.rstrip("/") or "/",
                    fragment="",
                ).geturl()
            if (reference and reference in seen_search_refs) or (
                canonical_url and canonical_url in seen_search_urls
            ):
                continue
            if reference:
                seen_search_refs.add(reference)
            if canonical_url:
                seen_search_urls.add(canonical_url)
            aggregated_records.append(item)

    if warnings_text:
        aggregated_errors.append(warnings_text)
    if len(aggregated_records) >= max_documents_budget and not all_queries_complete:
        limit_marker = "TEST_TRUNCATED" if test_limits else "SOURCE_LIMIT_REACHED"
        aggregated_errors.append(
            f"{limit_marker}: Global max documents reached ({max_documents_budget})"
        )

    api_result = {
        "results": aggregated_records,
        "pages_fetched": pages_fetched,
        # Raw counts may include duplicates across query texts; records_discovered
        # remains the unique retained opportunity count.
        "raw_records_received": raw_records_received,
        # Totals across different text searches overlap, so do not present their
        # sum as a count of unique opportunities.
        "total_expected": query_totals[0] if len(search_texts) == 1 and query_totals else None,
        "pagination_complete": all_queries_complete and not aggregated_errors,
        "errors": aggregated_errors,
    }

    records = api_result.get("results", [])
    result.pages_fetched = int(api_result.get("pages_fetched", 0) or 0)
    result.api_pages_fetched = result.pages_fetched
    result.api_expected_records = api_result.get("total_expected")
    result.api_records_received = int(api_result.get("raw_records_received", len(records)) or 0)
    result.pagination_detected = True
    result.pagination_complete = bool(api_result.get("pagination_complete"))
    result.discovery_exhausted = bool(api_result.get("pagination_complete"))
    result.records_discovered = len(records)
    # Count duplicates removed both within a query's pages and across query terms.
    result.duplicates_removed += max(0, raw_records_received - len(records))

    for error in api_result.get("errors", []):
        result.errors.append({
            "url": "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
            "operation": "paginated_search",
            "error": error,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        if "TEST_TRUNCATED" in error or "SOURCE_LIMIT_REACHED" in error:
            result.truncated = True
            if "TEST_TRUNCATED" in error:
                result.final_status = "TEST_TRUNCATED"
        if warnings is not None:
            warnings.append(f"EU Funding & Tenders API: {error}")
    if not result.pagination_complete:
        result.truncated = True

    seen_detail_urls: set[str] = set()
    # Report an unreachable shared eligibility annex once, not once per topic.
    reported_annex_failures: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            result.records_failed += 1
            continue
        raw_metadata = item.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        # Search hits can omit richer publisher fields. Fetch the documented
        # EU document record only when the hit has no eligibility/content payload.
        detail_reference = str(item.get("reference") or metadata.get("reference") or "").strip()
        # EU Search hits often omit eligibility-specific keys even when the
        # official document's description contains an explicit "who can apply"
        # section. Scan publisher-owned rich-text fields before fetching details.
        detail_keys = (
            "eligibility", "eligibilityConditions", "eligibleApplicants", "applicantEligibility",
            "admissibilityConditions", "eligibilityCriteria", "topicConditions",
            "eligibilityCriteriaHtml", "admissibilityCriteria", "beneficiaryAdministration",
            "description", "descriptionEn", "descriptionByte", "topicDescription",
            "scope", "summary",
        )
        metadata_eligibility_present = bool(
            _extract_eu_eligibility_from_html(
                _eu_metadata_value(metadata, "eligibility", "eligibleApplicants", "applicantEligibility")
            )
        ) or any(
            _extract_eu_eligibility_from_html(metadata.get(key))
            for key in detail_keys
        )
        title = _eu_metadata_value(metadata, "title", "topicTitle", "callTitle", "name")
        if not title:
            title = _eu_metadata_value(item, "title", "topicTitle", "callTitle", "name")
        url_value = item.get("url") or metadata.get("url")
        if isinstance(url_value, list):
            url_value = next((value for value in url_value if value), "")
        url = str(url_value or "").strip()
        if not url.startswith("https://") or not title:
            result.records_failed += 1
            result.errors.append({
                "url": url or "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
                "operation": "normalize_search_result",
                "error": "EU API result is missing a concrete title or HTTPS detail URL",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        # Distinct search references can still point at the same detail page.
        # Keep source records unique by their canonical URL before analysis.
        parsed_detail_url = urlparse(url)
        canonical_detail_url = parsed_detail_url._replace(
            scheme=parsed_detail_url.scheme.lower(),
            netloc=parsed_detail_url.netloc.lower(),
            path=parsed_detail_url.path.rstrip("/") or "/",
            fragment="",
        ).geturl()
        if canonical_detail_url in seen_detail_urls:
            result.duplicates_removed += 1
            continue
        seen_detail_urls.add(canonical_detail_url)

        raw_status = _eu_metadata_value(metadata, "status", "submissionStatus", "callStatus")
        if not raw_status:
            raw_status = _eu_metadata_value(item, "status", "submissionStatus", "callStatus")
        opening_raw = _eu_metadata_value(
            metadata, "openingDate", "startDate", "openingDateTime", "publicationDate"
        )
        deadline_raw = _eu_metadata_value(
            metadata, "deadlineDate", "deadline", "deadlineDateTime", "submissionDeadline", "endDate"
        )
        opening_dt = parse_date_string(opening_raw) if opening_raw else None
        deadline_dt = parse_date_string(deadline_raw) if deadline_raw else None
        opening_date = opening_dt.date().isoformat() if opening_dt else None
        deadline = deadline_dt.date().isoformat() if deadline_dt else None
        internal_status, status_consistency_warning = _eu_effective_status(
            raw_status, deadline
        )
        if status_consistency_warning:
            _record_eu_filter(result, "expired_deadline", title, url)
            continue
        if internal_status not in {"OPEN_CALL", "UPCOMING_CALL"}:
            _record_eu_filter(result, "unrecognized_or_noncurrent_status", title, url)
            continue

        # A call (e.g. HORIZON-CL5-2026-11) groups many separately evaluated
        # topics (HORIZON-CL5-2026-11-D3-06). The topic code is the individual
        # opportunity's identity; using the shared call code would merge sibling
        # topics into one record downstream. Fall back to the call code only when
        # the publisher supplies no topic code.
        topic_identifier = _eu_metadata_value(
            metadata, "identifier", "topicIdentifier", "topicAbbreviation"
        )
        call_identifier = _eu_metadata_value(metadata, "callIdentifier")
        identifier = topic_identifier or call_identifier
        has_description_metadata = bool(_eu_metadata_value(
            metadata, "description", "descriptionEn", "descriptionByte",
            "topicDescription", "scope", "summary",
        ))
        topic_facts: dict[str, Any] | None = None
        # EuropeAid references (e.g. "EuropeAid/167264/DD/ACT/Multi") are listed
        # with topic-style URLs but have no topicDetails document; keep them on
        # the original Search API path rather than recording a spurious failure.
        is_portal_topic = bool(
            topic_identifier
            and _EU_TOPIC_IDENTIFIER_RE.fullmatch(str(topic_identifier))
            and _eu_is_portal_topic(item, metadata, url)
        )
        # Horizon-style topics: the Search API hit carries only index metadata.
        # Read the official topicDetails document (description, conditions,
        # per-action status, deadlines, budget, submission link) instead of
        # re-querying the same Search API, which returns no additional fields.
        if (
            api_config.get("detail_fetch_missing_metadata", True)
            and topic_identifier
            and is_portal_topic
            and not (metadata_eligibility_present and has_description_metadata)
        ):
            result.detail_pages_discovered += 1
            try:
                topic_details = eu_funding_tenders_fetch_topic_details(str(topic_identifier))
                topic_facts = _eu_topic_details_facts(topic_details, str(topic_identifier))
                result.detail_pages_fetched += 1
            except Exception as exc:
                result.detail_pages_failed += 1
                result.errors.append({
                    "url": EU_TOPIC_DETAILS_URL_TEMPLATE.format(
                        identifier=str(topic_identifier).casefold()
                    ),
                    "operation": "fetch_detail_metadata",
                    "error": f"EU topicDetails fetch failed: {exc}",
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                LOGGER.warning(
                    "EU topicDetails fetch failed for %s: %s", topic_identifier, exc,
                )
        # SEDIA Topic Details lookup is keyed by the publisher's call/topic
        # identifier, not the opaque internal search-result reference.
        elif (
            api_config.get("detail_fetch_missing_metadata", True)
            and identifier
            and not metadata_eligibility_present
            and not is_portal_topic
        ):
            result.detail_pages_discovered += 1
            try:
                detail = eu_funding_tenders_fetch_document(str(identifier))
                detail_metadata = detail.get("metadata", {}) if isinstance(detail, dict) else {}
                if isinstance(detail_metadata, dict):
                    for key, value in detail_metadata.items():
                        if metadata.get(key) in (None, "", []):
                            metadata[key] = value
                result.detail_pages_fetched += 1
            except Exception as exc:
                result.detail_pages_failed += 1
                result.errors.append({
                    "url": (
                        "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
                        f"?apiKey=SEDIA&text={identifier}"
                    ),
                    "operation": "fetch_detail_metadata",
                    "error": str(exc),
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                LOGGER.warning(
                    "EU topic detail fetch failed for identifier %s: %s",
                    identifier, exc,
                )
        status_cross_check = None
        if topic_facts is not None:
            topic_status_label = topic_facts.get("status_label")
            if topic_facts.get("deadline"):
                deadline = topic_facts["deadline"]
                deadline_raw = f"{deadline} (official topicDetails next deadline)"
            if topic_facts.get("opening_date") and not opening_date:
                opening_date = topic_facts["opening_date"]
                opening_raw = f"{opening_date} (official topicDetails planned opening)"
            if not topic_status_label:
                _record_eu_filter(result, "topic_details_status_missing", title, url)
                continue
            topic_status, topic_warning = _eu_effective_status(topic_status_label, deadline)
            status_cross_check = (
                f"Search API status {raw_status!r}; official topicDetails action status "
                f"{', '.join(topic_facts.get('status_labels') or [])!r}"
            )
            if topic_status != internal_status:
                status_cross_check += (
                    f" — disagreement resolved in favour of the per-topic record ({topic_status})."
                )
            internal_status = topic_status
            raw_status = topic_status_label
            if topic_warning:
                status_consistency_warning = topic_warning
                _record_eu_filter(result, "expired_deadline", title, url)
                continue
            if internal_status not in {"OPEN_CALL", "UPCOMING_CALL"}:
                _record_eu_filter(result, "topic_details_status_not_current", title, url)
                continue
        programme = _eu_metadata_value(
            metadata, "programme", "programmeName", "fundingProgramme", "frameworkProgramme"
        )
        # The portal sometimes exposes an internal numeric programme ID in a
        # display-name field. Do not publish that ID as the funding-body name.
        if programme and str(programme).strip().isdigit():
            programme = None
        amount = _eu_metadata_value(
            metadata, "maximumGrantAmount", "maxGrantAmount", "fundingAmount", "budget"
        )
        scalar_eligibility = _eu_metadata_value(
            metadata, "eligibility", "eligibleApplicants", "applicantEligibility"
        )
        eligibility = _extract_eu_eligibility_from_html(scalar_eligibility)
        eligibility_source_field = "eligibility"
        if not eligibility:
            for key in detail_keys:
                extracted = _extract_eu_eligibility_from_html(metadata.get(key))
                if extracted:
                    eligibility = extracted
                    eligibility_source_field = key
                    break
        eligibility_provenance = "European Commission Funding & Tenders public Search API metadata"
        if not eligibility and topic_facts is not None:
            topic_document_url = topic_facts.get("document_url") or EU_TOPIC_DETAILS_URL_TEMPLATE.format(
                identifier=str(topic_identifier).casefold()
            )
            topic_specific = _extract_eu_eligibility_from_html(topic_facts.get("conditions_html"))
            annex_text, annex_url, annex_error = _eu_resolve_annex_eligibility(
                topic_facts.get("conditions_html") or ""
            )
            if annex_error and annex_url and annex_url not in reported_annex_failures:
                reported_annex_failures.add(annex_url)
                result.documents_failed += 1
                result.errors.append({
                    "url": annex_url,
                    "operation": "pdf_fetch",
                    "error": f"Eligibility annex cited by EU topic conditions could not be verified: {annex_error}",
                    "timestamp": datetime.now(UTC).isoformat(),
                })
            parts = []
            if topic_specific:
                parts.append(topic_specific)
            topic_conditions = _eu_topic_specific_eligibility(topic_facts.get("conditions_html") or "")
            if topic_conditions and (annex_text or topic_specific):
                # Binding topic-specific criteria travel with the generic rules;
                # they never stand in for missing applicant eligibility alone.
                parts.append(topic_conditions)
            if annex_text:
                parts.append(
                    "Annex B of the Work Programme General Annexes (cited for eligibility by the "
                    f"topic conditions): {annex_text}"
                )
            if parts:
                eligibility = " ".join(parts)
                eligibility_source_field = (
                    "topicDetails.conditions" + (" + cited Annex B" if annex_text else "")
                )
                eligibility_provenance = (
                    f"Official EU topicDetails document {topic_document_url}"
                    + (f"; eligibility annex {annex_url}" if annex_text else "")
                )
        description = _eu_metadata_value(
            metadata, "description", "descriptionEn", "descriptionByte", "objective",
            "topicDescription", "scope", "summary", "content", "beneficiaryAdministration"
        )
        if not description and topic_facts is not None and topic_facts.get("description"):
            description = topic_facts["description"]
        if topic_facts is not None and not amount and topic_facts.get("budget"):
            amount = topic_facts["budget"]

        fields = [
            f"Title: {title}",
            f"Official detail URL: {url}",
            f"Status: {raw_status or 'Not supplied in result metadata'}",
        ]
        if topic_identifier:
            fields.append(f"Topic identifier: {topic_identifier}")
        if call_identifier:
            fields.append(f"Call identifier: {call_identifier}")
        if topic_facts is not None and topic_facts.get("action_types"):
            action_types = "; ".join(
                re.sub(r"\s+", " ", value) for value in topic_facts["action_types"]
            )
            fields.append(f"Type of action: {action_types}")
        if programme:
            fields.append(f"Programme: {programme}")
        if opening_raw:
            fields.append(f"Opening Date: {opening_raw}")
        if deadline_raw:
            fields.append(f"Deadline: {deadline_raw}")
        if amount:
            fields.append(f"Funding amount or budget: {amount}")
        if eligibility:
            fields.append(f"Eligibility: {eligibility}")
        if description:
            fields.append(f"Description: {description}")

        require_eligibility = bool(api_config.get("require_eligibility_evidence", True))
        if require_eligibility and not eligibility:
            _record_eu_filter(result, "missing_eligibility_evidence", title, url)
            continue

        doc = FundingDocument(
            organization=programme or source.name,
            title=title,
            source=url,
            source_type="Official EU Funding & Tenders Search API",
            content="\n".join(fields)[:50000],
            year=current_year(),
        )
        try:
            analysis = analyze_document(doc)
            require_domain_match = bool(api_config.get("require_domain_match", True))
            if require_domain_match and (
                analysis.focus_area == "General"
                or analysis.focus_area not in set(source.research_domains or [])
            ):
                _record_eu_filter(result, "off_domain_or_weak_domain_evidence", title, url)
                continue
            analysis.program_name = title
            analysis.source_title = title
            if topic_facts is not None:
                official_intent, official_evidence = _eu_action_type_research_intent(
                    topic_facts.get("action_types") or []
                )
                if official_intent:
                    research_context = dict(analysis.research_context or {})
                    keyword_evidence = [
                        str(item) for item in research_context.get("research_intent_evidence") or []
                        if str(item) != official_evidence
                    ]
                    research_context["research_intent"] = official_intent
                    research_context["research_intent_evidence"] = (
                        [official_evidence] + keyword_evidence[:3]
                        if official_intent == "research"
                        else [official_evidence]
                    )
                    analysis.research_context = research_context
                    analysis.evidence["research_intent_provenance"] = (
                        f"Official EU topicDetails type of action: {official_evidence}"
                    )
            application_url, application_url_provenance = _eu_application_url(
                metadata, canonical_detail_url
            )
            if topic_facts is not None and topic_facts.get("submission_links"):
                application_url = topic_facts["submission_links"][0]
                application_url_provenance = (
                    "Official EU topicDetails submission-service link"
                )
            if application_url:
                analysis.application_url = application_url
                analysis.evidence["application_url"] = (
                    f"{application_url_provenance}: {application_url}"
                )
            if identifier:
                analysis.call_id = identifier
                # Keep one identity per record: the research context must carry
                # the same official topic/call code. Otherwise the context builder
                # refuses to rebind contexts that have no evidence refs (e.g.
                # non-research CSA/IA/PCP topics) and run_pipeline.main crashed.
                if isinstance(analysis.research_context, dict):
                    research_context = dict(analysis.research_context)
                    research_context["funding_call_id"] = identifier
                    analysis.research_context = research_context
            if call_identifier and call_identifier != identifier:
                analysis.evidence["parent_call_identifier"] = call_identifier
            source_label = (
                "official EU topicDetails" if topic_facts is not None else "EU Search API"
            )
            if internal_status:
                analysis.status = internal_status
                analysis.evidence["source_status"] = raw_status or internal_status
                analysis.evidence["source_status_provenance"] = (
                    "Official EU Funding & Tenders topicDetails document (per-action status), "
                    "cross-checked against the public Search API"
                    if topic_facts is not None
                    else "European Commission Funding & Tenders public Search API metadata"
                )
                if status_cross_check:
                    analysis.evidence["status_cross_check"] = status_cross_check
                if status_consistency_warning:
                    analysis.evidence["status_consistency_warning"] = status_consistency_warning
                    analysis.evidence["effective_status_provenance"] = (
                        "Conservative deadline guard applied because the source status "
                        "conflicts with its past deadline."
                    )
            if opening_date:
                analysis.opening_date = opening_date
                analysis.evidence["opening_date"] = f"{source_label} opening date: {opening_raw}"
            if deadline:
                analysis.deadline = deadline
                analysis.evidence["deadline"] = f"{source_label} deadline: {deadline_raw}"
            if eligibility:
                analysis.eligibility = eligibility
                eligibility_origin = (
                    "EU Search API metadata field"
                    if eligibility_source_field in detail_keys or eligibility_source_field == "eligibility"
                    else "Official EU field"
                )
                analysis.evidence["eligibility"] = (
                    f"{eligibility_origin} {eligibility_source_field}: {eligibility}"
                )
                analysis.evidence["eligibility_provenance"] = eligibility_provenance
            if amount:
                analysis.funding_amount = amount
                analysis.evidence["funding_amount"] = f"{source_label} metadata: {amount}"
            result.opportunities.append((doc, analysis))
            result.records_valid += 1
        except Exception as exc:
            result.records_failed += 1
            result.errors.append({
                "url": url,
                "operation": "analyze_search_result",
                "error": str(exc),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if warnings is not None:
                warnings.append(f"EU funding record analysis failed for {url}: {exc}")

    if result.truncated:
        if result.final_status != "TEST_TRUNCATED":
            result.final_status = "PARTIAL"
    elif result.errors:
        result.final_status = "PARTIAL" if result.opportunities else "FAIL"
    elif result.opportunities:
        result.final_status = "VERIFIED"
    elif result.records_filtered:
        result.final_status = "NO_QUALIFIED_OPPORTUNITIES"
    else:
        result.final_status = "NO_ACTIVE_OPPORTUNITIES"
    return result


def _collect_github(
    source: FundingSource,
    result: CollectionResult,
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect from GitHub Issues API with genuine pagination exhaustion."""
    repo = source.collection.get("repo", "")
    if not repo or "/" not in repo:
        result.final_status = "FAIL"
        result.errors.append({
            "url": source.url,
            "operation": "github_repo_parse",
            "error": f"Invalid repo specification: {repo}",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return result

    owner, repo_name = repo.split("/", 1)
    api_result = github_paginated_issues(
        owner, repo_name, test_limits=test_limits, source_limits=source_limits
    )

    issues = api_result["issues"]
    if test_limits and "max_documents" in test_limits:
        issues = issues[:test_limits["max_documents"]]
    if source_limits and "max_documents" in source_limits:
        issues = issues[:source_limits["max_documents"]]
        
    result.api_pages_fetched = api_result["pages_fetched"]
    result.api_records_received = len(issues)
    result.pagination_complete = api_result["pagination_complete"]
    result.discovery_exhausted = bool(api_result["pagination_complete"])
    result.pagination_detected = True
    result.records_discovered = len(issues)

    if api_result.get("rate_limited"):
        result.truncated = True

    for err in api_result.get("errors", []):
        result.errors.append({
            "url": f"https://api.github.com/repos/{repo}/issues",
            "operation": "github_paginated_issues",
            "error": err,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        if "TEST_TRUNCATED" in err:
            result.truncated = True
            result.final_status = "TEST_TRUNCATED"
        elif "SOURCE_LIMIT_REACHED" in err:
            result.truncated = True
        if warnings is not None:
            warnings.append(f"GitHub API: {err}")

    if not result.pagination_complete:
        result.truncated = True

    # Classify and normalize each issue
    seen_ids: set[int] = set()
    for issue in issues:
        issue_id = issue.get("id", 0)
        if issue_id in seen_ids:
            result.duplicates_removed += 1
            continue
        seen_ids.add(issue_id)

        # Skip pull requests
        if "pull_request" in issue:
            result.urls_skipped += 1
            continue

        labels = [l.get("name", "").lower() for l in issue.get("labels", [])]
        state = issue.get("state", "open").lower()
        title_lower = issue.get("title", "").lower()

        # Classify issue type
        is_grant = "grant" in title_lower or "rfp" in title_lower or any(
            "grant" in l or "rfp" in l or "open grant" in l for l in labels
        )
        is_approved = any("approved" in l or "funded" in l for l in labels)

        # Build content
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        html_url = issue.get("html_url", "")
        updated = issue.get("updated_at", "")
        created = issue.get("created_at", "")

        if is_grant:
            if is_approved:
                status_label = "Approved Grant"
            elif state == "open":
                status_label = "Open Grant Request"
            else:
                status_label = "Closed/Rejected Request"
        else:
            status_label = "Issue"

        content = (
            f"Title: {title}\n"
            f"Type: {status_label}\n"
            f"State: {state}\n"
            f"Labels: {', '.join(labels)}\n"
            f"Created: {created}\n"
            f"Updated: {updated}\n"
            f"URL: {html_url}\n"
            f"Body:\n{body[:10000]}\n"
        )

        doc = FundingDocument(
            organization=source.name,
            title=title,
            source=html_url,
            source_type="Official API",
            content=content,
            year=current_year(),
        )

        try:
            analysis = analyze_document(doc)
            result.opportunities.append((doc, analysis))
            result.records_valid += 1
        except Exception as e:
            result.records_failed += 1
            result.errors.append({
                "url": html_url,
                "operation": "analyze_document",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if warnings is not None:
                warnings.append(f"GitHub analysis failed for {html_url}: {e}")

    # Final status
    if result.errors and not result.opportunities:
        result.final_status = "FAIL"
    elif result.truncated:
        if result.final_status != "TEST_TRUNCATED":
            result.final_status = "PARTIAL"
    elif result.opportunities:
        result.final_status = "VERIFIED"
    else:
        result.final_status = "NO_ACTIVE_OPPORTUNITIES"

    return result


def _fetch_status_verification_context(
    source: FundingSource,
    warnings: Optional[List[str]] = None,
    document: Optional[FundingDocument] = None,
) -> str:
    """Fetch publisher-owned closure evidence for the exact mapped document route.

    Legacy status_verification_urls remain source-wide for existing sources.
    Sources with status_verification_url_map opt into exact document-to-status
    routing so a closure notice for one programme cannot contaminate another.
    """
    collection = source.collection or {}
    route_map = collection.get("status_verification_url_map")
    document_url = urlparse(str(getattr(document, "source", "") or ""))
    route_specific = isinstance(route_map, dict)
    if route_specific:
        if document is None:
            return ""
        urls = []
        for document_route, candidates in route_map.items():
            parsed_route = urlparse(str(document_route or ""))
            if (
                (parsed_route.hostname or "").casefold() == (document_url.hostname or "").casefold()
                and parsed_route.path.rstrip("/") == document_url.path.rstrip("/")
            ):
                if isinstance(candidates, list):
                    urls.extend(candidates)
                elif isinstance(candidates, str):
                    urls.append(candidates)
    else:
        urls = collection.get("status_verification_urls", [])
    if not isinstance(urls, list) or not urls:
        return ""

    from core.http_client import fetch_text

    evidence: list[str] = []
    closure_pattern = re.compile(
        r"[^.!?\n]{0,180}\b(?:paused|suspended|closed for applications|no longer accepting|"
        r"not accepting (?:any fresh |new )?applications|not accepting new submissions|"
        r"submissions? will not be processed)\b[^.!?\n]{0,180}[.!?]?",
        flags=re.IGNORECASE,
    )
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        expected = urlparse(url)
        try:
            body, final_url = fetch_text(url, timeout=(8.0, 20.0), retries=1)
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"{source.name}: status verification could not be fetched from {url}: {exc}")
            LOGGER.warning("Status verification fetch failed for %s (%s): %s", source.name, url, exc)
            continue
        if route_specific:
            final = urlparse(str(final_url or url))
            if (
                (final.hostname or "").casefold() != (expected.hostname or "").casefold()
                or final.path.rstrip("/") != expected.path.rstrip("/")
            ):
                continue
        if not body:
            continue
        matches = [cleaned_text(match.group(0)) for match in closure_pattern.finditer(str(body))]
        for match in matches[:5]:
            evidence.append(f"Official status page ({final_url or url}): {match}")
    return "\n".join(dict.fromkeys(evidence))



def _fetch_currentness_verification_context(
    source: FundingSource,
    document: FundingDocument,
    warnings: Optional[List[str]] = None,
) -> str:
    """Verify explicitly configured publisher-owned application/currentness evidence."""
    discovery = source.collection.get("discovery", {}) or {}
    urls = source.collection.get(
        "currentness_verification_urls",
        discovery.get("currentness_verification_urls", []),
    )
    if not isinstance(urls, list) or not urls:
        return ""
    document_url = urlparse(str(document.source or ""))
    configured_urls = []
    for candidate in urls:
        if not isinstance(candidate, str) or not candidate.startswith("https://"):
            continue
        parsed = urlparse(candidate)
        if (
            (parsed.hostname or "").casefold() == (document_url.hostname or "").casefold()
            and parsed.path.rstrip("/") == document_url.path.rstrip("/")
        ):
            configured_urls.append(candidate)
    # Do not leak a source-level status cue into unrelated opportunity documents.
    if not configured_urls:
        return ""

    from core.http_client import fetch_text

    patterns = (
        r"apply to (?:ubri )?(?:digital asset xcelerator|udax)(?:\s+\(udax\))?\s+2027",
        r"apply for protocol rewards",
        r"apply to horizon",
        r"applying is as simple as creating a github issue",
        r"applications? (?:can be submitted at any time|are accepted on a rolling basis)",
        r"submit your proposal anytime",
        r"applications? are evaluated regular intervals?",
        r"applications? (?:are )?reviewed on a rolling basis",
        r"proposals are accepted on a rolling basis",
        r"open grants applications? for funding up to",
    )
    verified = []
    for url in configured_urls:
        expected = urlparse(url)
        try:
            body, final_url = fetch_text(url, timeout=(8.0, 20.0), retries=1)
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"{source.name}: currentness verification could not be fetched from {url}: {exc}")
            LOGGER.warning("Currentness verification fetch failed for %s (%s): %s", source.name, url, exc)
            continue
        final_url = str(final_url or url)
        final = urlparse(final_url)
        if final.scheme.casefold() != "https" or (final.hostname or "").casefold() != (expected.hostname or "").casefold():
            continue
        body = str(body or "")
        for pattern in patterns:
            match = re.search(pattern, body, flags=re.IGNORECASE)
            if match:
                excerpt = cleaned_text(body[max(0, match.start() - 180):min(len(body), match.end() + 240)])
                verified.append(
                    f"Official currentness verification: CURRENT APPLICATION EVIDENCE CONFIRMED\n"
                    f"Official currentness URL: {final_url}\n{excerpt[:700]}"
                )
                break
    return "\n".join(dict.fromkeys(verified))



def _verify_configured_empty_state(
    source: FundingSource,
    warnings: Optional[List[str]] = None,
) -> bool:
    """Independently verify an explicitly configured publisher empty-state route."""
    discovery = source.collection.get("discovery", {}) or {}
    urls = discovery.get("empty_state_verification_urls", []) or []
    if not isinstance(urls, list):
        return False

    from core.http_client import fetch_text

    for url in urls:
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        expected = urlparse(url)
        try:
            body, final_url = fetch_text(url, timeout=(8.0, 20.0), retries=1)
        except Exception as exc:
            if warnings is not None:
                warnings.append(
                    f"{source.name}: explicit empty-state verification failed for {url}: {exc}"
                )
            LOGGER.warning(
                "Empty-state verification fetch failed for %s (%s): %s",
                source.name, url, exc,
            )
            continue

        final = urlparse(str(final_url or url))
        if (
            final.scheme.casefold() != "https"
            or (final.hostname or "").casefold() != (expected.hostname or "").casefold()
            or final.path.rstrip("/") != expected.path.rstrip("/")
        ):
            continue
        if _reports_no_active_funding(url, source.name, str(body or "")):
            return True
    return False


def _is_generic_sbir_topic_title(value: str | None) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned_text(str(value or "")).casefold()).strip()
    return normalized in {
        "", "topic", "topic sbir", "topic funding opportunities", "funding opportunities",
        "back to funding opportunities search", "home", "menu", "official solicitation",
        "program solicitation", "solicitation", "nsf sbir", "open sbir funding topics",
    }


def _fetch_linked_official_sbir_context(
    document: FundingDocument,
    warnings: Optional[List[str]] = None,
    cache: Optional[dict[str, str]] = None,
) -> str:
    """Verify an SBIR mirror record against the official solicitation it links to.

    SBIR.gov warns that topic pages may lag the issuing agency. For topic pages,
    follow only the page's explicitly labelled official-solicitation link, require
    an HTTPS .gov destination, and verify that the solicitation identifier matches
    before trusting its current/open status. A mirror's own disclaimer is never
    treated as proof that a call is stale or current.
    """
    parsed_source = urlparse(str(document.source or ""))
    if (
        (parsed_source.hostname or "").casefold() not in {"sbir.gov", "www.sbir.gov"}
        or not re.fullmatch(r"/topics/\d+/?", parsed_source.path)
        or not re.search(
            r"not necessarily the latest and most up-to-date",
            str(document.content or ""),
            flags=re.IGNORECASE,
        )
    ):
        return ""

    link = re.search(
        r"\[\s*View Official Solicitation\s*\]\((https://[^)\s]+)\)",
        str(document.content or ""),
        flags=re.IGNORECASE,
    )
    if not link:
        return ""
    candidate_url = link.group(1).strip()
    candidate = urlparse(candidate_url)
    if (
        candidate.scheme.casefold() != "https"
        or not candidate.hostname
        or not candidate.hostname.casefold().endswith(".gov")
        or candidate.port not in (None, 443)
        or candidate.username
        or candidate.password
    ):
        return ""

    cache = cache if cache is not None else {}
    if candidate_url in cache:
        return cache[candidate_url]

    from core.http_client import fetch_text

    try:
        official_text, final_url = fetch_text(candidate_url, timeout=(8.0, 20.0), retries=1)
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                f"SBIR official solicitation verification failed for {candidate_url}: {exc}"
            )
        cache[candidate_url] = ""
        return ""

    final_url = str(final_url or candidate_url)
    final = urlparse(final_url)
    if (
        final.scheme.casefold() != "https"
        or not final.hostname
        or not final.hostname.casefold().endswith(".gov")
    ):
        cache[candidate_url] = ""
        return ""

    source_text = str(document.content or "")
    official_text = str(official_text or "")
    identifier_pattern = re.compile(
        r"\b(?:[A-Z]{2,6}\s+\d{2}-\d{3}|[A-Z]{2,6}-\d{2}-\d{2,3})\b",
        flags=re.IGNORECASE,
    )
    source_ids = {
        re.sub(r"\s+", "", match.group(0)).upper()
        for match in identifier_pattern.finditer(source_text)
    }
    official_ids = {
        re.sub(r"\s+", "", match.group(0)).upper()
        for match in identifier_pattern.finditer(official_text)
    }
    matched_ids = sorted(source_ids & official_ids)
    normalized_official = re.sub(r"\s+", " ", official_text).casefold()
    official_current = bool(
        re.search(
            r"\bactive funding opportunity\b|"
            r"\bthis document is the current version\b|"
            r"\b(?:solicitation status|opportunity status)\s*:?\s*open\b|"
            r"\bopen opportunity\b",
            normalized_official,
            flags=re.IGNORECASE,
        )
    )

    # A current umbrella solicitation is not proof that a mirror's individual
    # topic is current. Require the official agency text to identify the same
    # topic as well as the solicitation number and current status.
    topic_title = cleaned_text(str(document.title or ""))
    if _is_generic_sbir_topic_title(topic_title):
        headings = re.findall(r"(?im)^\s*#{1,6}\s+([^\n]+?)\s*$", source_text)
        for heading in headings:
            candidate_title = cleaned_text(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading))
            if _is_generic_sbir_topic_title(candidate_title):
                continue
            topic_title = candidate_title
            break
    normalized_topic = re.sub(r"[^a-z0-9]+", " ", topic_title.casefold()).strip()
    normalized_official_identity = re.sub(r"[^a-z0-9]+", " ", normalized_official).strip()
    topic_confirmed = bool(
        normalized_topic
        and not _is_generic_sbir_topic_title(topic_title)
        and normalized_topic in normalized_official_identity
    )
    # NSF 26-510/26-511 are umbrella solicitations covering many topic areas;
    # matching that number alone cannot verify an individual SBIR.gov topic.
    # A unique opportunity identifier such as PA-27-100 can verify the record
    # when the official opportunity page repeats that identifier and open status.
    generic_umbrella_ids = {"NSF26-510", "NSF26-511"}
    requires_topic_identity = any(identifier in generic_umbrella_ids for identifier in matched_ids)
    if not matched_ids or not official_current or (requires_topic_identity and not topic_confirmed):
        if warnings is not None and matched_ids and official_current and requires_topic_identity and not topic_confirmed:
            warnings.append(
                f"NSF umbrella solicitation {matched_ids[0]} is current, but official evidence does not "
                f"confirm mirror topic '{topic_title or document.source}'; keeping topic status unknown."
            )
        cache[candidate_url] = ""
        return ""

    relevant_lines = [
        cleaned_text(line)
        for line in official_text.splitlines()
        if re.search(
            r"active funding opportunity|current version|program solicitation|"
            r"solicitation number|opportunity status|solicitation status|"
            r"full proposal deadline|due date|close date|application deadline|open date",
            line,
            flags=re.IGNORECASE,
        )
    ]
    excerpt = "\n".join(dict.fromkeys(line for line in relevant_lines if line))[:5000]
    context = (
        "\n\n## Official Solicitation Verification\n"
        "Official solicitation verification: CURRENT VERSION CONFIRMED\n"
        f"Official solicitation URL: {final_url}\n"
        f"Matched solicitation identifier: {matched_ids[0]}\n"
        f"{excerpt}"
    )
    cache[candidate_url] = context
    return context


def _is_explicit_no_active_placeholder(document: FundingDocument, source_name: str) -> bool:
    """Identify explicit publisher empty-state rows without mistaking open RFPs for placeholders."""
    content = re.sub(r"\s+", " ", cleaned_text(str(document.content or "")).casefold())
    title = cleaned_text(str(document.title or "")).casefold()
    source_url = str(document.source or "").casefold()
    if (
        "if this is the only rfp that is visible" in content
        and "do not apply for this" in content
    ):
        return True

    parsed = urlparse(source_url)
    known_solana_rfp_view = (
        source_name == "Solana Grants and Funding"
        and (parsed.hostname or "").casefold() in {"airtable.com", "www.airtable.com"}
        and parsed.path.casefold().startswith((
            "/apppdmk2pin9wx8jv/shrr0umku4n57tgw7",
            "/apppdmk2pin9wx8jv/shrdbfj1wktq7pb6f",
        ))
    )
    return bool(
        known_solana_rfp_view
        and re.search(r"\brfp context\b", content)
        and re.search(r"\bdo not apply for this\b", content)
        and re.search(
            r"no other active rfp(?:s)?(?: are available| at this time| available)?",
            content,
        )
    )


def _collect_web_source(
    source: FundingSource,
    area: Optional[str],
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect from a web source using Crawl4AI with BFS traversal."""
    allowed_domains = source.collection.get("allowed_domains", [])
    config = dict(source.collection.get("discovery", {}) or {})
    if source.direct_page:
        config["direct_page"] = True

    # crawl_funding_source returns raw opportunities as dicts
    result = crawl_funding_source(
        source.name, source.url, config, allowed_domains, test_limits, source_limits
    )
    # Some application managers render their empty-state text client-side.
    # Re-fetch only explicitly configured exact routes so an inaccessible or
    # changed page can never be silently treated as an empty programme.
    if not result.no_active_opportunities_confirmed:
        result.no_active_opportunities_confirmed = _verify_configured_empty_state(
            source, warnings
        )

    # Normalize: convert raw dicts +' FundingDocument +' analyze_document
    raw_opps = list(result.opportunities)
    if test_limits and "max_documents" in test_limits:
        raw_opps = raw_opps[:test_limits["max_documents"]]
    if source_limits and "max_documents" in source_limits:
        raw_opps = raw_opps[:source_limits["max_documents"]]
        
    result.opportunities = []
    result.records_discovered = len(raw_opps)

    # Resolve status evidence per document below to prevent cross-route leakage.
    # Cache linked agency solicitations within this source run so a shared
    # solicitation (e.g. NSF 26-510) is fetched once, not once per topic.
    official_verification_cache: dict[str, str] = {}
    seen_urls: set[str] = set()
    explicit_empty_state_seen = False
    for raw in raw_opps:
        raw_url = raw.get("url", "")
        if raw_url in seen_urls:
            result.duplicates_removed += 1
            continue
        seen_urls.add(raw_url)

        doc = _normalize_raw_to_doc(source, raw)
        verification_context = _fetch_status_verification_context(source, warnings, document=doc)
        currentness_context = _fetch_currentness_verification_context(source, doc, warnings)
        linked_official_context = _fetch_linked_official_sbir_context(
            doc, warnings, official_verification_cache
        )
        if linked_official_context:
            doc.content = (doc.content.rstrip() + linked_official_context)[:200000]

        # A topic page whose own publisher explicitly warns that its copied
        # solicitation may be stale is not a valid funding-call record unless
        # the linked official source independently confirms that topic identity
        # and currentness. Preserve the fetched record in acquisition/filter
        # telemetry, but do not normalize it into an opportunity or allow it
        # to poison source-level currentness for other independently verified calls.
        parsed_doc_url = urlparse(str(doc.source or ""))
        is_sbir_topic_mirror = (
            (parsed_doc_url.hostname or "").casefold() in {"sbir.gov", "www.sbir.gov"}
            and re.fullmatch(r"/topics/\d+/?", parsed_doc_url.path) is not None
            and re.search(
                r"not necessarily the latest and most up-to-date",
                str(doc.content or ""),
                flags=re.IGNORECASE,
            ) is not None
        )
        if is_sbir_topic_mirror and not linked_official_context:
            result.records_filtered += 1
            reason = "unverified_sbir_mirror_topic"
            result.records_filtered_by_reason[reason] = (
                result.records_filtered_by_reason.get(reason, 0) + 1
            )
            if warnings is not None:
                warnings.append(
                    f"Skipped unverified SBIR mirror topic as a funding-call record: {doc.title!r} "
                    f"({doc.source}); linked official source did not confirm this topic's currentness."
                )
            continue

        if _is_explicit_no_active_placeholder(doc, source.name):
            explicit_empty_state_seen = True
            result.records_filtered += 1
            reason = "explicit_no_active_rfp_placeholder"
            result.records_filtered_by_reason[reason] = result.records_filtered_by_reason.get(reason, 0) + 1
            continue

        if verification_context:
            doc.content = (
                doc.content.rstrip()
                + "\n\n## Official Status Verification\n"
                + verification_context
            )[:200000]

        if currentness_context:
            doc.content = (
                doc.content.rstrip()
                + "\n\n## Official Currentness Verification\n"
                + currentness_context
            )[:200000]

        try:
            analysis = analyze_document(doc)
            result.opportunities.append((doc, analysis))
            result.records_valid += 1
        except Exception as e:
            result.records_failed += 1
            result.errors.append({
                "url": raw_url,
                "operation": "analyze_document",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            if warnings is not None:
                warnings.append(f"Web analysis failed for {raw_url}: {e}")

    # An explicit empty-state marker is meaningful only if no actual call records
    # survived filtering. A placeholder must never override a genuine call.
    if explicit_empty_state_seen and result.records_valid == 0:
        result.no_active_opportunities_confirmed = True
    elif result.records_valid > 0:
        result.no_active_opportunities_confirmed = False

    # Keep source status conservative after normalization. The crawl may have
    # collected some evidence while another page or record failed; that is
    # partial coverage, not a verified or empty source.
    if result.records_failed > 0:
        if result.final_status != "TEST_TRUNCATED":
            result.final_status = "PARTIAL" if result.records_valid > 0 else "FAIL"
    elif result.errors or result.pages_failed > 0 or result.documents_failed > 0:
        if result.pages_fetched > 0 or result.documents_fetched > 0 or result.records_valid > 0:
            if result.final_status != "TEST_TRUNCATED":
                result.final_status = "PARTIAL"
        elif result.final_status not in {"PARTIAL", "TEST_TRUNCATED"}:
            result.final_status = "FAIL"
    elif result.records_valid == 0 and result.records_filtered > 0 and result.no_active_opportunities_confirmed:
        if (
            result.pages_fetched > 0
            and not result.truncated
            and result.discovery_exhausted
            and not result.coverage_gaps
            and result.relevant_urls_skipped == 0
            and (not result.pagination_detected or result.pagination_complete)
        ):
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"
        elif result.pages_fetched > 0 and not result.truncated:
            result.final_status = "PARTIAL"
    elif result.records_valid == 0 and result.records_discovered == 0:
        if (
            result.pages_fetched > 0
            and not result.truncated
            and result.discovery_exhausted
            and result.no_active_opportunities_confirmed
            and not result.coverage_gaps
            and result.relevant_urls_skipped == 0
            and (not result.pagination_detected or result.pagination_complete)
        ):
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"
        elif result.pages_fetched > 0 and not result.truncated:
            # Do not overwrite the crawler's PARTIAL status when it found
            # relevant routes it could not inspect (for example, external
            # application forms linked from Solana/XRPL programme pages).
            result.final_status = "PARTIAL"

    return result
