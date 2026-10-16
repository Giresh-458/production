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
from datetime import UTC, datetime
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
)
from core.crawl4ai_web import crawl_funding_source
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
            "NSF SBIR Trusted Identity",
            "NSF SBIR Climate Change Adaptation and Mitigation",
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


def _normalize_raw_to_doc(
    source: FundingSource,
    raw: dict,
) -> FundingDocument:
    """Convert a raw crawler result dict into a FundingDocument."""
    raw_type = raw.get("type", "html")
    url = raw.get("url", source.url)
    title = str(raw.get("title", "") or "").strip()
    content = str(raw.get("content", "") or "")

    # Some topic publishers expose site chrome ("Our Programs") as the browser
    # title. Prefer a concrete content heading before naming an output record.
    generic_titles = {
        "our programs", "programs", "topic", "home", "welcome", "funding",
        "grants", "grant", "overview", "about", "details", "description",
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
        content=content[:50000],  # protect against extremely large pages
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


def _collect_eu_funding_tenders(
    source: FundingSource,
    result: CollectionResult,
    warnings: Optional[List[str]],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Collect only open/forthcoming EU calls from the Commission Search API."""
    api_config = source.collection.get("api", {}) or {}
    api_result = eu_funding_tenders_paginated_search(
        types=[str(value) for value in api_config.get("types", ["1", "2", "8"])],
        statuses=[str(value) for value in api_config.get("statuses", ["31094501", "31094502"])],
        page_size=int(api_config.get("page_size", 50)),
        test_limits=test_limits,
        source_limits=source_limits,
    )

    records = api_result.get("results", [])
    result.pages_fetched = int(api_result.get("pages_fetched", 0) or 0)
    result.api_pages_fetched = result.pages_fetched
    result.api_expected_records = api_result.get("total_expected")
    result.api_records_received = len(records)
    result.pagination_detected = True
    result.pagination_complete = bool(api_result.get("pagination_complete"))
    result.records_discovered = len(records)

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

    for item in records:
        if not isinstance(item, dict):
            result.records_failed += 1
            continue
        raw_metadata = item.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
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

        raw_status = _eu_metadata_value(metadata, "status", "submissionStatus", "callStatus")
        if not raw_status:
            raw_status = _eu_metadata_value(item, "status", "submissionStatus", "callStatus")
        internal_status = _eu_status_to_internal(raw_status)
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

        identifier = _eu_metadata_value(
            metadata, "callIdentifier", "topicAbbreviation", "identifier", "topicIdentifier"
        )
        programme = _eu_metadata_value(
            metadata, "programme", "programmeName", "fundingProgramme", "frameworkProgramme"
        )
        amount = _eu_metadata_value(
            metadata, "maximumGrantAmount", "maxGrantAmount", "fundingAmount", "budget"
        )
        eligibility = _eu_metadata_value(
            metadata, "eligibility", "eligibilityConditions", "eligibleApplicants", "applicantEligibility"
        )
        description = _eu_metadata_value(
            metadata, "description", "descriptionEn", "objective", "topicDescription", "scope", "summary", "content"
        )

        fields = [
            f"Title: {title}",
            f"Official detail URL: {url}",
            f"Status: {raw_status or 'Not supplied in result metadata'}",
        ]
        if identifier:
            fields.append(f"Call identifier: {identifier}")
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
            analysis.program_name = title
            analysis.source_title = title
            if identifier:
                analysis.call_id = identifier
            if internal_status:
                analysis.status = internal_status
                analysis.evidence["source_status"] = raw_status or internal_status
                analysis.evidence["source_status_provenance"] = (
                    "European Commission Funding & Tenders public Search API metadata"
                )
            if opening_date:
                analysis.opening_date = opening_date
                analysis.evidence["opening_date"] = f"EU Search API opening date: {opening_raw}"
            if deadline:
                analysis.deadline = deadline
                analysis.evidence["deadline"] = f"EU Search API deadline: {deadline_raw}"
            if eligibility:
                analysis.eligibility = eligibility
                analysis.evidence["eligibility"] = f"EU Search API metadata: {eligibility}"
            if amount:
                analysis.funding_amount = amount
                analysis.evidence["funding_amount"] = f"EU Search API metadata: {amount}"
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

    if result.errors and not result.opportunities and not result.truncated:
        result.final_status = "FAIL"
    elif result.truncated:
        if result.final_status != "TEST_TRUNCATED":
            result.final_status = "PARTIAL"
    elif result.opportunities:
        result.final_status = "VERIFIED"
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

    # Normalize: convert raw dicts +' FundingDocument +' analyze_document
    raw_opps = list(result.opportunities)
    if test_limits and "max_documents" in test_limits:
        raw_opps = raw_opps[:test_limits["max_documents"]]
    if source_limits and "max_documents" in source_limits:
        raw_opps = raw_opps[:source_limits["max_documents"]]
        
    result.opportunities = []
    result.records_discovered = len(raw_opps)

    seen_urls: set[str] = set()
    for raw in raw_opps:
        raw_url = raw.get("url", "")
        if raw_url in seen_urls:
            result.duplicates_removed += 1
            continue
        seen_urls.add(raw_url)

        doc = _normalize_raw_to_doc(source, raw)
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
    elif result.records_valid == 0 and result.records_discovered == 0:
        if result.pages_fetched > 0 and not result.truncated:
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"

    return result
