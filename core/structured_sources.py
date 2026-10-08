from __future__ import annotations

import os, re, time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.http_client import fetch_json, post_json, get_response

@dataclass(slots=True)
class StructuredResult:
    text: str
    url: str
    title: str
    metadata: dict[str, Any]


def _q(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _github(url: str, limit: int = 20) -> StructuredResult | None:
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    if not m: return None
    owner, repo = m.group(1), m.group(2).removesuffix('.git')
    headers = {"Accept":"application/vnd.github+json", "User-Agent":"RIF-Structured-Source/1.0", "X-GitHub-Api-Version":"2026-03-10"}
    token = os.getenv("GITHUB_TOKEN")
    if token: headers["Authorization"] = f"Bearer {token}"
    base=f"https://api.github.com/repos/{owner}/{repo}"
    repo_data,_=fetch_json(base,headers=headers,timeout=(8,20),retries=1)
    
    from core.http_client import get_response
    issues = []
    try:
        r = get_response(base+"/issues", params={"state":"all","per_page":min(limit,100)}, headers=headers, timeout=(8,20), retries=1)
        issues = r.json()
        if not isinstance(issues, list): issues = []
    except Exception:
        pass
        
    releases = []
    try:
        r = get_response(base+"/releases", params={"per_page":min(10,limit)}, headers=headers, timeout=(8,20), retries=1)
        releases = r.json()
        if not isinstance(releases, list): releases = []
    except Exception:
        pass
    text=[f"GitHub repository: {repo_data.get('full_name', owner+'/'+repo)}",f"Description: {repo_data.get('description') or ''}",f"Stars: {repo_data.get('stargazers_count',0)}",f"Forks: {repo_data.get('forks_count',0)}",f"Open issues: {repo_data.get('open_issues_count',0)}"]
    # Filecoin Semantics & general Github semantics:
    # Do not treat every issue as a grant opportunity.
    # We classify them based on labels and state.
    for x in issues[:limit]:
        if 'pull_request' in x: continue
        labels = [l.get('name', '').lower() for l in x.get('labels', [])]
        state = x.get('state', 'open').lower()
        title = x.get('title', '').lower()
        
        is_grant_request = "grant" in title or "rfp" in title or "open grant" in labels
        is_informational = "question" in labels or "discussion" in labels
        is_approved = "approved" in labels or "funded" in labels
        is_rejected = "rejected" in labels or "closed" in state and not is_approved
        
        status_label = "Open Grant Request" if (is_grant_request and state == "open") else \
                       "Approved Grant" if is_approved else \
                       "Closed/Rejected Request" if (is_grant_request and state == "closed") else \
                       "Informational Issue" if is_informational else \
                       "Issue"
                       
        text.append(f"{status_label}: {x.get('title','')} | {x.get('html_url','')} | updated {x.get('updated_at','')}")
    for x in releases[:10]: text.append(f"Release: {x.get('name') or x.get('tag_name','')} | {x.get('published_at','')} | {x.get('html_url','')}")
    return StructuredResult(_q('\n'.join(text)),base,f"{owner}/{repo}",{"api":"github","authenticated":bool(token)})


def _zenodo(url: str, limit: int = 10) -> StructuredResult | None:
    if 'zenodo.org' not in urlparse(url).netloc: return None
    query = ''
    m=re.search(r'/records/(\d+)',url)
    endpoint='https://zenodo.org/api/records'
    params={'size':limit}
    if m: endpoint=f"https://zenodo.org/api/records/{m.group(1)}"; params={}
    else:
        parsed=urlparse(url); query=parsed.query
        params['q']=''
    data,_=fetch_json(endpoint,params=params,headers={'User-Agent':'RIF-Structured-Source/1.0'},timeout=(8,20),retries=1)
    records=data.get('hits',{}).get('hits',[]) if isinstance(data,dict) else []
    if isinstance(data,dict) and data.get('metadata'):
        records=[data]
    lines=[]
    for r in records[:limit]:
        md=r.get('metadata',{})
        lines.append(f"Zenodo record: {md.get('title','')} | {r.get('links',{}).get('self',url)} | published {md.get('publication_date','')} | description {md.get('description','')}")
        for f in md.get('related_identifiers',[])[:10]: lines.append(f"Related: {f.get('identifier','')} ({f.get('relation','')})")
    return StructuredResult(_q('\n'.join(lines)),endpoint,'Zenodo API',{'api':'zenodo','records':len(records)})


def _openaq(url: str, area: str | None) -> StructuredResult | None:
    if 'openaq.org' not in urlparse(url).netloc: return None
    key=os.getenv('OPENAQ_API_KEY')
    if not key: return None
    headers={'X-API-Key':key,'User-Agent':'RIF-Structured-Source/1.0'}
    data,_=fetch_json('https://api.openaq.org/v3/locations',params={'limit':100,'page':1},headers=headers,timeout=(8,20),retries=1)
    results=data.get('results',[]) if isinstance(data,dict) else []
    lines=[f"OpenAQ locations returned: {len(results)}"]
    for x in results[:50]:
        lines.append(f"Location: {x.get('name','')} | country {x.get('country',{}).get('code','')} | locality {x.get('locality','')} | id {x.get('id','')}")
    return StructuredResult(_q('\n'.join(lines)),'https://api.openaq.org/v3/locations','OpenAQ API',{'api':'openaq','authenticated':True})


def _etherscan(url: str) -> StructuredResult | None:
    if 'etherscan.io' not in urlparse(url).netloc: return None
    key=os.getenv('ETHERSCAN_API_KEY')
    if not key: return None
    params={'chainid':'1','module':'stats','action':'ethsupply','apikey':key}
    data,_=fetch_json('https://api.etherscan.io/v2/api',params=params,headers={'User-Agent':'RIF-Structured-Source/1.0'},timeout=(8,20),retries=1)
    return StructuredResult(_q(f"Etherscan Ethereum API result: status={data.get('status')} message={data.get('message')} result={data.get('result')}"),'https://api.etherscan.io/v2/api','Etherscan API',{'api':'etherscan','authenticated':True})


def _sec(url: str) -> StructuredResult | None:
    if 'sec.gov' not in urlparse(url).netloc: return None
    headers={'User-Agent':os.getenv('SEC_USER_AGENT','RIF research contact@example.com'),'Accept-Encoding':'gzip, deflate'}
    # Company tickers are the safest general structured entry point; no key required.
    data,_=fetch_json('https://www.sec.gov/files/company_tickers.json',headers=headers,timeout=(8,20),retries=1)
    items=list(data.values()) if isinstance(data,dict) else []
    lines=[f"SEC company ticker directory records: {len(items)}"]
    for x in items[:50]: lines.append(f"Company: {x.get('title','')} | ticker {x.get('ticker','')} | CIK {x.get('cik_str','')}")
    return StructuredResult(_q('\n'.join(lines)),'https://www.sec.gov/files/company_tickers.json','SEC EDGAR API',{'api':'sec','authenticated':False})



def grantsgov_paginated_search(
    keywords: str,
    limit_per_page: int = 50,
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> dict:
    """Paginate through Grants.gov API until all results are consumed.

    Returns a dict with:
        hits: list[dict]           — all retrieved opportunity summaries
        pagination_complete: bool  — True only when retrieved count matches API total
        total_expected: int | None — API-reported hitCount
        pages_fetched: int
        errors: list[str]
    """
    all_hits: list[dict] = []
    start_record = 0
    pages_fetched = 0
    errors: list[str] = []
    total_expected: int | None = None
    pagination_complete = False
    previous_start = -1  # loop-detection state
    seen_ids: set[Any] = set()
    source_max_pages = source_limits.get("max_pages") if source_limits else None

    while True:
        # Infinite-loop safeguard: detect non-advancing pagination
        if start_record == previous_start:
            errors.append(f"Pagination stalled at startRecordNum={start_record}")
            break
        previous_start = start_record

        if source_max_pages is not None and pages_fetched >= source_max_pages:
            errors.append(f"SOURCE_LIMIT_REACHED: Max pages reached ({source_max_pages})")
            break

        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx and ctx.check_limits(source_max_pages=20):  # Cap API pages to a reasonable 20
            errors.append("Crawl context deadline or limit reached")
            break

        payload = {
            "rows": limit_per_page,
            "startRecordNum": start_record,
            "keyword": keywords,
            "oppStatuses": "open|forecasted|posted",
        }
        try:
            data, _ = post_json(
                "https://api.grants.gov/v1/api/search2",
                json_body=payload,
                headers={"Content-Type": "application/json", "User-Agent": "RIF-Structured-Source/1.0"},
                timeout=(8, 25),
                retries=2,
            )
        except Exception as e:
            errors.append(f"API request failed at page {pages_fetched + 1}: {e}")
            break

        if not isinstance(data, dict):
            errors.append(f"Grants.gov API returned non-JSON/non-dict response: type {type(data).__name__}")
            break

        data_block = data.get("data")
        if not isinstance(data_block, dict):
            # Do NOT truncate string previews, but be mindful of huge dumps. Safe to print ~100 chars
            err_str = str(data_block)[:200]
            errors.append(f"Grants.gov returned non-dict data block: {err_str}")
            break
            
        raw_hit_count = data_block.get("hitCount", 0)
        try:
            hit_count = int(raw_hit_count)
        except (ValueError, TypeError):
            errors.append(f"Grants.gov returned malformed hitCount: {raw_hit_count}")
            break

        hits = data_block.get("oppHits")
        if hits is None:
            hits = []
        if not isinstance(hits, list):
            errors.append(f"Grants.gov returned malformed oppHits: type {type(hits).__name__}")
            break

        if total_expected is None and hit_count:
            total_expected = hit_count

        if not hits:
            # API returned no more results — pagination is done
            pagination_complete = True
            break

        # Repeated page-content detection
        current_ids = {h.get("id") or h.get("number") or h.get("title") for h in hits}
        if current_ids and current_ids.issubset(seen_ids):
            errors.append(f"Grants.gov returned repeated page content at page {pages_fetched + 1}")
            break
        seen_ids.update(current_ids)

        all_hits.extend(hits)
        pages_fetched += 1
        start_record += len(hits)

        # Check if we've consumed all expected records
        if total_expected is not None and start_record >= total_expected:
            pagination_complete = True
            break
            
        if test_limits and pages_fetched >= test_limits.get("max_pages", float("inf")):
            errors.append(f"TEST_TRUNCATED: Max pages reached ({test_limits['max_pages']})")
            break

        # Additional safeguard: if we got fewer hits than requested, we're at the end
        if len(hits) < limit_per_page:
            pagination_complete = True
            break

    return {
        "hits": all_hits,
        "pagination_complete": pagination_complete,
        "total_expected": total_expected,
        "pages_fetched": pages_fetched,
        "errors": errors,
    }

def grantsgov_fetch_opportunity(opp_id: str) -> dict | None:
    """Fetch detail for a single Grants.gov opportunity."""
    try:
        opportunity_id = int(str(opp_id).strip())
    except (TypeError, ValueError):
        import logging
        logging.getLogger("rif.structured_sources").warning(
            "Invalid Grants.gov opportunity ID: %r", opp_id
        )
        return None

    try:
        data, _ = post_json(
            "https://api.grants.gov/v1/api/fetchOpportunity",
            json_body={"opportunityId": opportunity_id},
            headers={
                "Content-Type": "application/json",
                "User-Agent": "RIF-Structured-Source/1.0",
            },
            timeout=(8, 20),
            retries=2,
        )
        return data.get("data", {}) if isinstance(data, dict) else None
    except Exception as e:
        import logging
        logging.getLogger("rif.structured_sources").warning(
            "Grants.gov detail fetch failed for %s: %s", opp_id, e
        )
        return None

def github_paginated_issues(
    owner: str,
    repo: str,
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> dict:
    """Paginate through GitHub Issues API until no more records.

    Returns a dict with:
        issues: list[dict]
        pagination_complete: bool
        pages_fetched: int
        errors: list[str]
        rate_limited: bool
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RIF-Structured-Source/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    all_issues: list[dict] = []
    errors: list[str] = []
    page = 1
    pages_fetched = 0
    pagination_complete = False
    rate_limited = False
    previous_ids: set[int] = set()
    source_max_pages = source_limits.get("max_pages") if source_limits else None

    while True:
        if source_max_pages is not None and pages_fetched >= source_max_pages:
            errors.append(f"SOURCE_LIMIT_REACHED: Max pages reached ({source_max_pages})")
            break

        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx and ctx.check_limits(source_max_pages=10):
            errors.append("Crawl context deadline or limit reached")
            break

        url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        params = {"state": "all", "per_page": 100, "page": page}

        try:
            r = get_response(url, params=params, headers=headers, timeout=(8, 25), retries=2)
        except Exception as e:
            errors.append(f"GitHub API request failed at page {page}: {e}")
            break

        # Rate limit detection
        remaining = r.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) <= 0:
            rate_limited = True
            errors.append(f"GitHub rate limit exhausted at page {page}")
            break

        if r.status_code == 403:
            rate_limited = True
            errors.append(f"GitHub API returned 403 at page {page} (likely rate limited)")
            break

        try:
            issues = r.json()
        except Exception as e:
            errors.append(f"GitHub JSON parse failed at page {page}: {e}")
            break

        if not isinstance(issues, list) or not issues:
            # No more results
            pagination_complete = True
            break

        # Repeated page detection
        current_ids = {i.get("id", 0) for i in issues}
        if current_ids and current_ids.issubset(previous_ids):
            errors.append(f"GitHub returned repeated page at page {page}")
            break
        previous_ids.update(current_ids)

        all_issues.extend(issues)
        pages_fetched += 1
        
        if test_limits and pages_fetched >= test_limits.get("max_pages", float("inf")):
            errors.append(f"TEST_TRUNCATED: Max pages reached ({test_limits['max_pages']})")
            break

        # Check Link header for next page
        link_header = str(r.headers.get("Link", ""))
        if link_header:
            if 'rel="next"' not in link_header:
                pagination_complete = True
                break
        elif len(issues) < 100:
            pagination_complete = True
            break

        page += 1

    return {
        "issues": all_issues,
        "pagination_complete": pagination_complete,
        "pages_fetched": pages_fetched,
        "errors": errors,
        "rate_limited": rate_limited,
    }

def collect_structured_source(url: str, source_type: str = '', area: str | None = None) -> StructuredResult | None:
    """Return a structured/API acquisition when the configured source has an official endpoint.
    Returns None when no safe API is available or credentials are absent, preserving scraper fallback.
    """
    lower=url.lower()
    
    if "api.grants.gov" in lower or "search2" in lower:
        return None # Grants.gov is now handled natively via paginated API outside this fallback pipeline

    for fn in (_github,_zenodo):
        try:
            result=fn(url)
            if result and result.text: return result
        except Exception as e:
            import logging
            logging.getLogger("rif.structured_sources").warning("Structured source %s failed for %s: %s", fn.__name__, url, e)
    for fn in (_openaq,_etherscan,_sec):
        try:
            if fn is _openaq:
                result=fn(url,area)
            else:
                result=fn(url)
            if result and result.text: return result
        except Exception as e:
            import logging
            logging.getLogger("rif.structured_sources").warning("Structured source %s failed for %s: %s", fn.__name__, url, e)
    return None
