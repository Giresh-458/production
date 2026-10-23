# Funding Agent Batch 1 Go/No-Go Checklist

**Decision: NO-GO — Batch 1 is not accepted; do not start Batch 2 yet.**

Audit baseline reviewed: GitHub Actions run [#126](https://github.com/Giresh-458/production/actions/runs/39902870149), commit `227a2456bf64ca68cbd8a4e046998558adc9aeff`, artifact `batch1-full-live-audit` (2026-10-23). This checklist is the release gate, not a claim that the current branch passes.

## How to use this checklist

- Check an item only after the evidence is present in the code, tests, and latest full live audit artifact.
- A green CI run alone is not acceptance.
- A source may be accepted as having no active opportunities only when the configured discovery boundary was exhausted and the evidence/currentness checks support that conclusion.
- Any exception must name the source, show the official evidence, explain the limitation, and be explicitly approved. Do not silently convert missing data into a pass.
- Batch 2 may start only when every **required** item below is checked and the final audit reports `batch_acceptance.status = ACCEPTED`.

## 0. Audit and reproducibility

- [x] Reviewed the latest run status, commit SHA, workflow steps, and uploaded full-scan artifact.
- [x] Reviewed the latest `summary.json`, `records.json`, `selected.json`, and representative source Markdown.
- [x] Recorded the current gate result: 75 raw records, 20 current-status candidates, 1 selected actionable call, 1 selected manual-review record, 0 selected quality flags, 0 coverage/status invariant violations, and 16 acceptance blockers.
- [ ] Re-run the full audit after the fixes below and attach a new artifact.
- [ ] Reconcile source-stage counts with the final record count and explain all filtering/deduplication. Current source-level `records_valid` totals 82 while the final raw record list contains 75; the pipeline must report the stage and reason for the seven-record difference clearly.

## 1. Branch integration and known regressions

- [ ] Ensure the exact commit used for the final audit includes the fixes from [PR #69](https://github.com/Giresh-458/production/pull/69) as well as the coverage/acceptance work from [PR #70](https://github.com/Giresh-458/production/pull/70). They are currently separate open branches; PR #70's artifact does **not** include the #69 fixes.
- [ ] I-DAPT proposal-template DOCX is fetched/counted as supporting material but is not emitted as a standalone funding opportunity.
- [ ] All seven Chanakya fellowship DOCX records have distinct, specific titles derived from the document content.
- [ ] I-DAPT parent call's status reflects the explicit current closure notice; do not infer open status from a conflicting generic “applications any time” phrase.
- [ ] Add/run regressions on the integrated branch and verify the full audit no longer contains the known template false positive or duplicate generic fellowship titles.

## 2. Record identity, deduplication, and status integrity

- [ ] Every record has a stable identity that does not rely on title alone; source URL, publisher ID, and source-specific identity are used consistently.
- [ ] Investigate the two EU topics sharing `call_id=HORIZON-INFRA-2027-01`; confirm whether the shared publisher call identifier is legitimate and ensure the records remain uniquely addressable.
- [ ] No duplicate generic titles remain unless the source explicitly gives the same title to genuinely distinct calls and the record identity distinguishes them.
- [ ] Every `OPEN_CALL` / `UPCOMING_CALL` has source evidence for its status and dates; expired or contradictory calls are not treated as open.
- [ ] Unknown, closed, paused, future, and open statuses remain distinct; unknown status is not silently promoted to open.

## 3. Applicant-facing record quality

- [ ] Every selected actionable call has an official source URL and a verified direct application/submission URL, or an explicit evidence-backed reason why no separate URL exists.
- [ ] Selected calls have source-supported deadline and substantive eligibility, or an explicit “not published/not applicable” value that is distinguishable from extraction failure.
- [ ] Funding amount, geography, research priorities, and other optional fields distinguish “not published” from “not extracted”; never guess missing values.
- [ ] Selected calls have no unresolved quality or manual-review flags.
- [ ] Verify the final selected call(s) against their official source pages and inspect the actual output Markdown, not just scores.
- [ ] The selector's high score cannot override missing direct-application evidence or unknown research context.

## 4. Evidence and research-context semantics

- [ ] `evidence_verified` and `currentness_verified` are set only by explicit verification logic and are persisted into the audit. They must not remain permanently false because only schema defaults are set.
- [ ] Every configured source has evidence/currentness verification semantics appropriate to its source type; “verified” means the check was actually performed.
- [ ] `problem` and `problem_signal` have distinct documented meanings. Do not copy the entire source document into both fields and label it a problem signal.
- [ ] Research-context claims carry traceable evidence references/provenance where required; no unsupported challenge or priority is invented.
- [ ] Confidence and `research_intent` accurately reflect the source content. Unknown confidence and empty evidence references are surfaced as unresolved rather than hidden by a relevance score.
- [ ] Add regression tests for raw-content leakage into problem fields, missing evidence refs, unknown research context, and domain-fit false positives.

## 5. Source coverage and acquisition completeness

- [ ] Audit inventory exactly matches the configured Batch 1 source inventory; missing source summaries are blockers, even when some other sources have summaries.
- [ ] Every source ends in a justified state: fully verified, verified no-active-opportunities, or explicitly blocked/approved partial. “No records” alone is not proof of no opportunities.
- [ ] Resolve relevant skipped URLs, external forms/tables, fetch failures, truncation, and incomplete pagination—or document a specific, evidence-backed exception for each.
- [ ] Verify that source status, `discovery_exhausted`, pagination flags, coverage gaps, and error counts agree.
- [ ] Define and test what `UNKNOWN` source status means in the acceptance gate; unknown status must not pass as complete.
- [ ] Acceptance tests prove that a missing configured source, a source with no verification telemetry, an unknown source status, a partial source, and a truncated source all block acceptance.

### Source-specific unresolved work from the latest artifact

- [ ] Ethereum ESP RFPs and Open Rounds: confirm “no active opportunities” with explicit evidence/currentness verification.
- [ ] Solana Grants: process or explicitly disposition the four relevant skipped URLs (including form/table links).
- [ ] Superteam Development Grants: resolve 27 skipped relevant URLs and the 60-page truncation; verify each retained listing's current status, eligibility, and actual grant terms.
- [ ] NEAR Ecosystem Funding: resolve six coverage gaps and incomplete pagination; validate the RPGF record against the actual official call.
- [ ] Ripple UBRI: resolve or explicitly disposition the skipped application form.
- [ ] XRPL Grants: inspect/disposition the skipped Brinc and Tenity destinations and the associated coverage gaps.
- [ ] Filecoin Grants: resolve the skipped GitHub devgrants link, reported fetch error/429, and truncation.
- [ ] Flashbots MEV Research Grants: add explicit evidence/currentness verification despite the source's `VERIFIED` status.
- [ ] Climate KIC: resolve 57 relevant skipped URLs and the 60-page truncation; verify that retained records are current calls rather than closed or general programs.
- [ ] NSF SBIR Trusted Identity and Climate Change Adaptation: confirm configured topic pages are actually represented in the final records and have current official evidence.
- [ ] Open SBIR Funding Topics: fix or explicitly disposition generic `Topic | SBIR` title skips, two page-fetch failures, seven relevant skipped URLs, and incomplete pagination.
- [ ] EU Funding & Tenders: resolve the 10-query pagination/document-limit warnings, five reported errors, and incomplete discovery; record query-level totals and filtering reasons without implying exhaustive coverage.
- [ ] I-DAPT: integrate the #69 DOCX fixes and verify the parent-call/document-role behavior on the final integrated branch.

## 6. EU Funding & Tenders retrieval

- [ ] The ten configured search queries are all executed; no query is silently omitted due to budget.
- [ ] Pagination is exhausted for each query, or the source remains explicitly blocked with per-query unsearched totals.
- [ ] Limits are coherent: the configured `max_documents: 1500` must not be misrepresented as full coverage when the per-query/page allocation caps retrieval earlier.
- [ ] Query overlaps and duplicate results are counted separately from valid records, and per-query totals are not summed as unique opportunities.
- [ ] Eligibility, status, deadline, and domain filters have regression tests using realistic official-source payloads.
- [ ] Relevant RWA, ESG, ZK-IoV, DID, DePIN, MEV, and Stablecoins calls are tested for both recall and false-positive rejection.

## 7. CI and final release gate

- [ ] Complete deterministic test suite passes on the integrated final branch.
- [ ] Focused regression suite passes on the integrated final branch.
- [ ] Bounded live audit passes its invariants and reports any limits accurately.
- [ ] Full configured live audit completes and its artifact is inspected.
- [ ] Latest artifact has zero unresolved selected-record quality/manual-review flags.
- [ ] Latest artifact has no unresolved source-coverage, evidence, currentness, pagination, truncation, or error blockers; any approved exception is explicitly recorded.
- [ ] Audit counts and source-stage metrics reconcile or explain their differences.
- [ ] Latest `summary.json` reports `batch_acceptance.status = ACCEPTED`, `accepted = true`, and `blocker_count = 0`.
- [ ] Final reviewer confirms the above evidence. Only then mark Batch 1 good to go and begin Batch 2.

## Current review findings (not yet fixed)

- The latest #70 artifact has 75 records; status distribution is 45 `CLOSED_CALL`, 11 `UPCOMING_CALL`, 9 `OPEN_CALL`, and 10 `UNKNOWN`.
- 47/75 records lack an application URL; 38/75 lack a deadline; 51/75 lack eligibility; 48/75 lack a funding amount; 61/75 lack research priorities. Missing values must be classified, not blindly filled.
- All 75 records have identical `problem` and `problem_signal` values; all 75 lack research-context `evidence_refs`; 53 have `confidence=Unknown` and `research_intent=non_research`.
- The latest source summary has 15 configured sources: 9 `PARTIAL`, 4 `VERIFIED`, and 2 `NO_ACTIVE_OPPORTUNITIES`. All 15 have `evidence_verified=false` and `currentness_verified=false`.
- EU retrieval reports 587 records discovered, 14 valid, 573 filtered, incomplete pagination/truncation, and five errors. Two retained EU records share `call_id=HORIZON-INFRA-2027-01` and require identity review.
- The latest selected EU call has three manual-review flags: missing direct application URL, unknown research-context confidence, and missing research-problem signal.
- Latest CI run #126 passed, but its audit correctly reports `batch_acceptance.status=BLOCKED` with 16 blockers. This is **not** a Batch 1 release approval.
