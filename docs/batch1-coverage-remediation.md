# PR #71 remediation notes — coverage, pagination, and reachability

## Verified diagnosis (2026-10-28)
The full Batch-1 scan at d07e90ff8237ee517ccd905a06efad41fdafe019 emitted 387 records; strict selection accepted 0. The acceptance gate reported 13 blockers.
- Climate KIC: PARTIAL; 1,568 unique coverage gaps, 1,567 relevant URLs skipped, 28 HTML fetch failures, 64 document fetch failures, pagination incomplete and discovery not exhausted.
- EU Funding & Tenders API: 26 pages, 1,188 raw records, 585 retained search records, 14 normalized valid records. Six targeted queries hit page/document caps; overall discovery remained incomplete.
- I-DAPT: FAIL because robots.txt retrieval and page retrieval timed out. Do not infer closure or empty state from the timeout.
- Several other sources have missing independent currentness verification, or anti-bot/robots/network limitations.
- All 387 records were rejected by strict qualification; reasons included 156 UNKNOWN statuses, 169 closed/expired calls, and missing applicant eligibility evidence on 34. These are quality filters, not a reason to loosen gates.

## Required remediation
1. Replace EU query-budget arithmetic with explicit fair budget allocation and make the query/page/document budgets observable per query. The current source-level limits permit only three pages per search term (and only 150 records for several queries); any cap must remain a blocker if the source cannot be exhausted.
2. Fix EU pagination accounting so duplicate overlap across queries does not confuse raw-vs-unique counts; keep the source PARTIAL if any query is not exhausted.
3. Reduce crawler false positives from external links on the Climate KIC/EIT umbrella site by restricting traversal to verified official funding/partner hosts and explicit route classes, not broad arbitrary third-party targets. Do not silently treat exclusions as verified coverage; audit exclusions and only suppress links proven non-call/navigation.
4. Keep robots restrictions fail-closed on 429/network/server failures, and honor explicit disallow. Do not bypass access controls or present inaccessible pages as verified. For I-DAPT, retry only within bounded limits and report precise failure evidence.
5. Add regression tests for budgets, per-query cap reporting, cross-query dedupe, URL frontier boundaries, timeout cases, and acceptance invariants.
6. Preserve strict eligibility, currentness, research-domain/problem evidence, accounting, and zero-actionable blockers. Never change the acceptance gate to turn incomplete coverage green.

## External references checked
- EU Funding & Tenders Portal official API documentation: https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/support/apis
- Crawl4AI download handling documentation: https://docs.crawl4ai.com/advanced/file-downloading/
- IETF RFC 9309 Robots Exclusion Protocol: https://www.rfc-editor.org/rfc/rfc9309.html

## Acceptance criteria
The PR is not production-accepted unless a fresh full configured scan at the updated head:
- passes deterministic and focused tests;
- reports all configured sources exactly once;
- reconciles source/document/output accounting;
- proves complete acquisition or honestly marks external failures PARTIAL/FAIL;
- has no unresolved relevant coverage gaps or pagination caps;
- has at least one genuinely qualified actionable call (or remains BLOCKED without claiming success);
- preserves every strict qualification gate.
