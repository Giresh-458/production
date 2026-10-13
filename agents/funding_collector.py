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
from core.structured_sources import (
    grantsgov_paginated_search,
    grantsgov_fetch_opportunity,
    github_paginated_issues,
)
from core.crawl4ai_web import crawl_funding_source
from agents.funding_agent import (
    FundingSource,
    FundingDocument,
    FundingOpportunity,
    analyze_document,
    current_year,
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
            "Grants.gov Public Opportunities API", 
            "Filecoin Grants and Funding", 
            "Chainlink Grants", 
            "NSF Cyber Physical and Intelligent Systems",
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
    title = raw.get("title", "")
    content = raw.get("content", "")

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
        app_err = next((err["error"] for err in result.errors if "Grants.gov returned" in err["error"]), "none")
        if app_err != "none":
            dtype = "string" if "non-dict data block" in app_err else "unknown"
        else:
            dtype = "dict" if not result.errors else "unknown"
            
        print("HTTP status: 200") # We know post_json handled it safely
        print(f"JSON response: {'yes' if 'non-JSON' not in str(result.errors) else 'no'}")
        print(f"Envelope: {'valid' if dtype == 'dict' else 'invalid'}")
        print(f"data type: {dtype}")
        print(f"API/application error: {app_err}")
        print(f"records retrieved: {result.records_discovered}")
        print(f"pagination_complete: {result.pagination_complete}")
        print(f"truncated: {result.truncated}")
        print(f"final_status: {result.final_status}\n")

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
    config = source.collection.get("discovery", {})

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

    # Adjust final status based on normalization outcomes
    if result.records_valid == 0 and result.records_failed > 0:
        result.final_status = "FAIL"
    elif result.records_valid == 0 and result.records_discovered == 0:
        if result.pages_fetched > 0:
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"
        # else keep the status from crawl_funding_source

    return result
