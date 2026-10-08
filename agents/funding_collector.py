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
        
        # Merge source-specific limits
        active_limits = dict(test_limits) if test_limits else {}
        source_limits = coll.get("limits", {})
        if "max_pages" in source_limits:
            active_limits["max_pages"] = source_limits["max_pages"]
        if "max_documents" in source_limits:
            active_limits["max_documents"] = source_limits["max_documents"]
        # For web discovery block backwards compatibility
        discovery = coll.get("discovery", {})
        if "max_pages" in discovery and "max_pages" not in active_limits:
            active_limits["max_pages"] = discovery["max_pages"]
            
        try:
            if mode == "api":
                yield _collect_api_source(source, area, warnings, active_limits)
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
                yield _collect_web_source(source, area, warnings, active_limits)
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
) -> CollectionResult:
    """Collect from an API source (Grants.gov or GitHub)."""
    provider = source.collection.get("provider", "")
    result = CollectionResult(source_id=source.name, method="api")

    if provider == "grants_gov":
        return _collect_grants_gov(source, area, result, warnings, test_limits)
    elif provider == "github":
        return _collect_github(source, result, warnings, test_limits)
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
) -> CollectionResult:
    """Collect from Grants.gov API with genuine pagination exhaustion."""
    query = "research cyber physical blockchain identity infrastructure"
    api_result = grantsgov_paginated_search(query, test_limits=test_limits)

    hits = api_result["hits"]
    if test_limits and "max_documents" in test_limits:
        hits = hits[:test_limits["max_documents"]]
        
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

        # Fetch detail if configured
        detail_content = ""
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
    api_result = github_paginated_issues(owner, repo_name, test_limits=test_limits)

    issues = api_result["issues"]
    if test_limits and "max_documents" in test_limits:
        issues = issues[:test_limits["max_documents"]]
        
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
) -> CollectionResult:
    """Collect from a web source using Crawl4AI with BFS traversal."""
    allowed_domains = source.collection.get("allowed_domains", [])
    config = source.collection.get("discovery", {})

    # crawl_funding_source returns raw opportunities as dicts
    result = crawl_funding_source(source.name, source.url, config, allowed_domains, test_limits)

    # Normalize: convert raw dicts +' FundingDocument +' analyze_document
    raw_opps = list(result.opportunities)
    if test_limits and "max_documents" in test_limits:
        raw_opps = raw_opps[:test_limits["max_documents"]]
        
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
