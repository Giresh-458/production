# Batch-1 funding remediation log

## Baseline observed before this cycle

- PR: [#71](https://github.com/Giresh-458/production/pull/71), branch `fix/batch1-funding-quality-gates`.
- Audited head: `53486ae1537c6dabefc43b921951b29f068e01f2`.
- Full configured scan: [workflow run 40651192363](https://github.com/Giresh-458/production/actions/runs/40651192363).
- Audit artifact: `batch1-full-live-audit`, artifact ID `12801331249`.
- Audit outcome: `BLOCKED`; 283 emitted records, 0 selected actionable calls, 13 blockers.
- Record accounting reconciled: 1,113 source records discovered; 284 source records valid; 825 source records filtered; 3,286 source duplicates removed; 283 records emitted.
- Deterministic suite, EU query-budget regressions, focused regressions, bounded live qualification audit, and 11-collector contract workflow passed on the audited head. The full configured scan / strict selector failed its acceptance gate.

## Root causes evidenced by the audit

1. **EU Funding & Tenders pagination budget was insufficient.** Two configured queries reached the 10-page per-query ceiling: `sustainability data` returned 500/577 expected rows and `internet of vehicles` returned 500/807. The source correctly remained `PARTIAL`; this is a configuration budget issue, not evidence that the source was exhausted.
2. **Climate KIC coverage remains incomplete.** The audit reported three relevant skipped URLs, three coverage gaps, 14 page failures, 55 document failures, 72 reported errors, and incomplete pagination/discovery. Some observed errors are external access/anti-bot or unavailable destinations; these must remain visible unless individually proven historical or irrelevant.
3. **I-DAPT acquisition remains partial.** The source reported a failed page/document acquisition, so its source-wide currentness and evidence state cannot be treated as verified.
4. **Other source summaries remain unverified or partial.** The audit identified verification gaps for Solana, Superteam, NEAR, Ripple UBRI, XRPL, Filecoin, and NSF/SBIR sources. A green deterministic suite does not resolve these live-source findings.
5. **No record passed all mandatory selection gates.** The audit's 283 candidates were rejected for evidence-grounded reasons including expired/closed or unknown status, missing substantive applicant eligibility, upcoming status, non-research intent, missing research-context evidence, and insufficient canonical-domain evidence. The zero-selection blocker is not being suppressed.

## Changes in this cycle

- Increased the EU API source budget from 100 pages / 5,000 documents to 200 pages / 10,000 documents. With ten configured search terms and page size 50, this permits up to 20 pages / 1,000 documents per query while remaining bounded.
- Updated the query-budget and source-configuration regression assertions to verify the new limits.
- Any query that still reaches a configured limit must continue to report incomplete pagination / `PARTIAL`; the limit increase does not make source verification optimistic.


## Latest full configured scan result (2026-10-28, head `91639c174c68e45deeea41a196d6f61593147ba9`)

- Workflow: [run 40660564214](https://github.com/Giresh-458/production/actions/runs/40660564214).
- Artifact: `batch1-full-live-audit`, artifact ID `12804943159`.
- Deterministic suite, EU query-budget regressions, focused regressions, and bounded live qualification audit all passed.
- Full configured scan and strict selector still failed acceptance: **286 emitted records; 56 raw current-status candidates; 0 selected actionable calls; 12 blockers**.
- The EU API pagination fix is confirmed in the live result: EU Funding & Tenders is `VERIFIED`, with 58 API pages fetched, 2,623 API records received, 774/774 detail pages fetched, 897 records discovered, pagination complete, discovery exhausted, zero skipped relevant URLs, and zero errors. It yielded 19 valid records after filtering out 878 for missing eligibility evidence, weak/off-domain evidence, or expired deadlines. This resolves the previous EU query-cap blocker without weakening the selector.
- Remaining acceptance blockers are: no actionable calls selected; evidence/currentness verification gaps for Solana, Superteam, NEAR, Ripple UBRI, XRPL, Filecoin, Climate KIC, NSF SBIR Trusted Identity, NSF SBIR Climate Change Adaptation and Mitigation, Open SBIR Funding Topics, and I-DAPT.
- Current source acquisition problems remain material: Climate KIC is `PARTIAL` with 5 coverage gaps, 5 relevant URLs skipped, 12 page failures, 55 document failures, incomplete pagination/discovery and 72 reported errors; Filecoin is `PARTIAL` with one relevant URL skipped and discovery not exhausted; I-DAPT is `PARTIAL` with one failed page and one reported error.
- Acceptance remains **`BLOCKED`**. Do not merge or start Batch 2 until remaining source verification and zero-actionable-call causes are addressed with evidence. The new run did not show that EU pagination itself remains a blocker.

## Verification correction found during this cycle

The first CI run after raising the configured budget stopped in the deterministic suite before the live scan because the helper-level budget test still passed the old literal inputs (`max_pages=100`, `max_documents=5000`) while asserting the new 20-page allocation. This was a stale test fixture, not a production-code failure. The fixture now passes 200 / 10,000, and the CI run on the new head must confirm the correction before live results can be assessed.

## Acceptance decision

**`BLOCKED` pending a new full configured scan and resolution of the remaining blockers.** This log does not claim that the budget change fixed the live audit. The next run must be inspected at its exact commit, including `summary.json`, `records.json`, `selected.json`, source summaries, and failure logs. Do not merge or start Batch 2 based on deterministic tests alone.

## Remediation cycle 2026-11-19 (local live verification)

### Baseline on `main` @ `02275ce` (after PR #72)

- Deterministic suite: 1 failure (`tests/test_batch4_normalization.py::test_case_10_and_11_missing_provenance`, a Windows-only file lock in Batch-2 normalization, unrelated to Batch-1).
- Full configured scan (the CI script, run locally): every source `VERIFIED`/`NO_ACTIVE_OPPORTUNITIES` and acceptance `ACCEPTED` — but only because `allow_empty_actionable_result=True`. **0 actionable calls were selected; nothing reached Batch-2.** The EU source discovered 896 records and kept 14; 882 were filtered, mostly `missing_eligibility_evidence`.

### Root causes (traced to code, confirmed against live responses)

1. **EU Horizon topics were never read from their official record.** For type-1 topics the Search API hit contains only index metadata (no description, conditions or submission link). The "detail fetch" re-ran the same Search API query and added zero fields. The portal itself renders each topic from `/opportunities/data/topicDetails/<identifier lowercased>.json` (the hit's own `esST_URL`), which carries per-action status, deadlines, budget, description, conditions and the submission-service link. Eligibility in those conditions is delegated to *Annex B of the Work Programme General Annexes*, linked from the topic.
2. **Topic identity collapsed to the call.** The collector preferred `callIdentifier` (`HORIZON-CL5-2026-11`) over the topic `identifier` (`HORIZON-CL5-2026-11-D3-06`), so sibling topics shared one `call_id` and the selector's call-id dedup could drop them as duplicates.
3. **Truncated JSON bodies were not retried.** `post_multipart_json`/`fetch_json` raised immediately on an undecodable HTTP 200 body (observed live: EU Search API page cut off at ~380 KB), aborting a whole EU query and marking the source `PARTIAL`.
4. **Crawl4AI navigation-timeout retry was dead code.** Crawl4AI 0.9.5 returns `CrawlResult(success=False, error_message="…Page.goto: Timeout…")` instead of raising; the bounded commit-wait retry only handled the raised form (observed live: sbir.gov listing pages 19–20).
5. **EU research intent ignored the official Type of Action.** Keyword matching labelled a Pre-Commercial Procurement topic (`HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03`) and a Coordination and Support Action (`HORIZON-NATURE-2027-02-03`) as research.
6. **Negated sentences counted as research evidence.** "This topic does not provide direct funding to … research organisations to perform R&D" passed because the negation filter did not allow an intervening word.
7. **Task statements and list fragments became problem signals.** "The network should integrate coordinators…" and a semicolon list item starting "or lack of resources…" were selected as research problems.

### Fixes

- `core/structured_sources.py`: `eu_funding_tenders_fetch_topic_details` (official topicDetails document, identifier-checked) and `eu_funding_tenders_fetch_reference_pdf_text` (only Commission-hosted `/opportunities/docs/*.pdf`, cached per run).
- `agents/funding_collector.py`: for portal topics with missing metadata, read topicDetails instead of re-querying search; independent per-topic status cross-check (topicDetails wins on disagreement and both are recorded); next unexpired deadline; topic-specific budget; official submission link; verbatim Annex B eligibility (country rule and consortium composition) only when the topic conditions cite Annex B, with provenance to both documents; topic code as identity with `parent_call_identifier` evidence; research intent from the official type of action (RIA, ERC frontier research and MSCA count as research; CSA, IA, PCP, PPI, COFUND and ERC-PoC do not); EuropeAid references keep the original path; one error per unreachable annex.
- `core/http_client.py`: undecodable 200 bodies use the existing bounded retry budget; persistent failures still raise `Invalid JSON`.
- `core/crawl4ai_web.py`: returned navigation-timeout results trigger the same single bounded retry; other returned failures are not retried.
- `agents/funding_agent.py`: sentences that negate funding/R&D are never research-intent evidence.
- `core/problem_extraction.py`: requirements placed on networks/consortia/selected projects are task statements; conjunction-led list fragments are not problem candidates.

No gate threshold, domain weight, source inventory or acceptance rule was relaxed.

### Technical basis

- Official topic document format and Annex B wording: observed directly from `topicDetails/horizon-cl5-2026-11-d3-06.json` and `wp-15-general-annexes_horizon-2026-2027_en.pdf` ("To be eligible for funding, applicants must be established in one of the following countries…"; "…the consortium includes, as beneficiaries, three legal entities independent from each other…").
- Type-of-action semantics: Horizon Europe Programme Guide (`programme-guide_horizon_en.pdf`: CSAs "support coordination and networking activities"; PCP/PPI co-finance procurers buying R&D) and Regulation (EU) 2021/695 Art. 2 definitions. EUR-Lex returned an anti-bot challenge (HTTP 202) to automated requests, so the Regulation text was not re-fetched in this cycle.
- SBIR.gov official solicitations API (`api.www.sbir.gov/public/api/solicitations`) returns HTTP 403 and the publisher states it is under maintenance; SBIR therefore remains web-crawled.

### Transient-network root cause (found in runs 3–4)

8. **One transient network fault could fail a whole source.** robots.txt was fetched with `retries=0`, and a single DNS failure (`getaddrinfo failed` for `www.sbir.gov`) was cached as a fail-closed policy for the entire source (SBIR `FAIL`, 0 pages). Browser navigation retried only timeouts, not Chromium network-stack faults (`ERR_NAME_NOT_RESOLVED`, `ERR_ADDRESS_UNREACHABLE`, `ERR_NETWORK_CHANGED`, `ERR_CONNECTION_ABORTED`). Fix: `core/robots_policy.py` retries a robots.txt *connection* failure once after 2 s (HTTP statuses are never retried; a second failure still fails closed per RFC 9309 §2.3.1.4); `core/crawl4ai_web.py` gives those network errors the same single bounded retry after a 2 s pause (certificate, HTTP and robots errors are not retried).

### Verification results (local, Windows 11, Python 3.11)

- Deterministic suite: `1193 passed, 7 skipped, 1 failed`. The single failure is the pre-existing Windows-only Batch-2 test noted in the baseline. 69 new Batch-1 regression tests (58 in three new files, 11 added to existing files): `tests/test_eu_topic_details_enrichment.py`, `tests/test_research_intent_negation.py`, `tests/test_robots_network_retry.py`, plus additions to `tests/test_http_policy_extra.py` and `tests/test_playwright_deadline.py`.
- Live full configured scan (the CI script, run locally), final run `ci-batch1-20261119T163714201142Z`:
  - `run_status=success`; **acceptance `ACCEPTED`, 0 blockers**; all 13 configured sources `VERIFIED` or `NO_ACTIVE_OPPORTUNITIES` with 0 errors, evidence and currentness verified, discovery exhausted; record accounting reconciled; 0 priority-source, coverage-invariant, quality, problem-quality or problem-semantic violations.
  - 142 records, 115 current-status candidates; **2 actionable calls selected; 2/2 contexts pass `validate_batch1_context`**. EU source: 102 valid records (previously 14).
  - `HORIZON-NEB-2026-01-BUSINESS-02`: Research and Innovation Action, Open, deadline 2026-12-01, EUR 3.5 M per project.
  - `HORIZON-NATURE-2027-02-02`: Research and Innovation Action, Open, deadline 2027-01-14, EUR 10 M per project.
  - Both re-verified independently against their official topicDetails documents after the scan (status Open, RIA, same deadlines, conditions cite Annex B for eligibility). The same two calls were selected in three consecutive independent scans.
- Batch-2 handoff: `run_batch2.py --profile smoke` (CI arguments) accepted the real `funding_contexts.json` from the final run (matching Batch-1 run ID, envelope validated, contexts independently revalidated), ran collection (10/11 collectors healthy, threshold 6) and normalized 29 records. Its own mission-relevance gate accepted 0 of 29 for the New European Bauhaus call (collected evidence was blockchain-oriented), so Batch-2 ended `incomplete_no_handoff`. That is a downstream Batch-2 evidence outcome; the Batch-1 to Batch-2 contract was satisfied.

## Acceptance decision (2026-11-19)

**Batch-1: GOOD TO GO.** A full live configured scan is `ACCEPTED` with zero blockers, every source verified, and two independently re-verified, currently open, research-eligible calls handed to Batch-2 with valid `FundingCallContext`s that Batch-2 accepted.

Open items outside Batch-1's acceptance scope:
- Domain fit: both selected calls qualify as ESG through existing strong terms in `core/schemas.py` (`new european bauhaus`, `bioeconomy`, `carbon`, `climate adaptation`). Whether those belong in RIF's ESG scope is a product decision; Batch-2 found no relevant evidence for the NEB call.
- `tests/test_batch4_normalization.py::test_case_10_and_11_missing_provenance` fails on Windows only (Batch-2 normalization file lock).
- External sources stay volatile: the acceptance holds for this run; CI should keep re-checking on every change.

## Double-check and Batch-2 cycle (2026-11-20)

### Additional defects found by the double-check (all fixed, with regression tests)

9. **Topic-specific eligibility was dropped (Batch-1).** `HORIZON-NATURE-2027-02-02` conditions add "The proposals must apply the multi-actor approach."; the handoff only carried generic Annex B text. `_eu_topic_specific_eligibility` now carries non-boilerplate text of the topic's own "Eligible countries"/"Other eligibility conditions" verbatim (never as a stand-in for missing applicant eligibility).
10. **Two-stage deadline (Batch-1).** For `deadlineModel: two-stage` topics the applicant deadline is now always the stage-1 date, so a passed stage 1 cannot look open via the stage-2 date.
11. **Brand-only ESG match (Batch-1, owner decision 2026-11-20).** "new european bauhaus" still scores toward ESG but satisfies the ESG anchor group only together with another ESG term (`DOMAIN_CORROBORATION_REQUIRED_ANCHORS`). `HORIZON-NEB-2026-01-BUSINESS-02` matched only the brand and produced 0 relevant records in Batch-2 under both the smoke and the full refresh profile; the owner's NEB-plus-sustainability test still passes.
12. **Batch-2 retrieval ignored the call (Batch-2).** For Medium/High-confidence contexts `generate_funding_queries` emitted only domain-template queries ("What challenges ... for ESG, Carbon and MRV?"). Queries now lead with the call title and the source-grounded research problem (only when evidence refs exist). Result: `HORIZON-NATURE-2027-02-02` went from 0 to 3–4 accepted records.
13. **Page dumps promoted to problem statements (Batch-2).** Normalization fell back to the first 280 raw characters, and accepted collector "snippets"/previews that were whole serialized pages (`Page title: … Source URL: …`, `[Depth N] … | url`). These now yield the collectors' existing placeholder "No explicit problem statement found."; the text stays in evidence/context.
14. **Windows-only test failure.** `test_case_10_and_11_missing_provenance` left its own SQLite handle open before re-normalizing; Windows cannot replace an open file. The test now closes it; production code already closed every connection.

### Final verification (local, Windows 11, Python 3.11)

- Deterministic suite: **1207 passed, 0 failed**, 7 skipped.
- Batch-1 live full configured scan `ci-batch1-20261120T025459190262Z`: **ACCEPTED, 0 blockers**; all 13 sources `VERIFIED`/`NO_ACTIVE_OPPORTUNITIES` with 0 errors; record accounting reconciled; 0 quality/problem/semantic/priority/invariant/review violations; 1 call selected, 1/1 context ready: `HORIZON-NATURE-2027-02-02` (RIA, Open, deadline 2027-01-14, EUR 10 M per project; eligibility includes Annex B and the multi-actor condition). NEB filtered at the collector as `off_domain_or_weak_domain_evidence`.
- Batch-2 (`run_batch2.py --profile smoke`, CI arguments) on that run's own `funding_contexts.json`: **`ready_for_batch3`, exit 0**; Batch-1 run ID matched; 10/11 collectors healthy (threshold 6); 44 records normalized, 4 accepted by the mission gate, no page-dump problem statements.
- External access limits recorded, not hidden: the hackathon collector persisted nothing because every source was blocked (Devpost robots.txt, DoraHacks HTTP 405, Devfolio/Chainlink anti-bot, Solana robots HTTP 429, two deadline overruns); other collectors are `partial_success` for 403/404/429/anti-bot/robots/page-cap reasons. SBIR.gov's official API remains HTTP 403.

## Acceptance decision (2026-11-20)

**Batch-1: GOOD TO GO. Batch-2: GOOD TO GO** for the verified handoff: a fresh full Batch-1 scan is `ACCEPTED` with a ready context, and Batch-2 consumed that exact handoff and reached `ready_for_batch3`. Live sources change daily, so the CI quality workflow must keep re-checking both on every push.

## Final chain verification and Batch-3 (2026-11-20)

- Batch-1 + Batch-2 re-verified on the final code (run `ci-batch1-20261120T042459997869Z` → Batch-2 `20261120T044310013067Z`): Batch-1 `ACCEPTED`, 0 blockers, all sources clean, 1/1 context ready; Batch-2 `ready_for_batch3` (run IDs matched, 10/11 collectors healthy, 4 records accepted).
- Batch-3 defects found on that real output and fixed:
  15. Cluster signal types were assigned by source layer alone, so a literature record with "No explicit problem statement found." became `research_problem`. Problem-asserting labels now require an extracted problem (`core/intelligence.py`).
  16. Batch-3 artifacts dropped `run_id`; tags, cluster members, clusters (`run_ids`) and trends (`run_ids`) now carry it.
  17. No Batch-3 output contract: stages returning `partial_success` with zero processed records passed. `core/batch3.py` validates coverage, identity, trend counts and handoff immutability; `run_pipeline.py` uses it; `scripts/pipeline_tools/run_batch3.py` runs it standalone; CI runs it after the Batch-2 smoke. See `docs/BATCH3_PROCESSING.md`.
- Batch-3 on the real Batch-2 output: **`ready_for_batch4`, exit 0** — tagging 4/4 records, 4 clusters covering all records once, trends counts match, Batch-2 handoff byte-identical, run IDs preserved.
- Deterministic suite: **1229 passed, 0 failed**, 7 skipped.

**Batch-1, Batch-2 and Batch-3: GOOD TO GO** on this verified chain. Not run here: GitHub Actions (Linux) and the post-Batch-3 LLM stages (synthesis/idea/proposal).

## Deep check (2026-11-20, commit 5efe992, fresh clones)

Method: fresh clone of the committed code; full deterministic suite; the committed CI workflow steps 2–7 executed live in order; the production entry point `run_pipeline.py --max-open-calls 1` executed end to end (Ollama `qwen3.5:4b`); every Batch-1 claim for the selected call re-checked against the official EU topic JSON and Annex B PDF.

Defects found by the deep check and fixed (each with a regression test that fails on the old code):

18. `run_pipeline.py` crashed (`ValueError: Research-context funding_call_id differs from the selected call`) building contexts for non-research EU topics: the collector set the topic code on `call_id` but not on `research_context.funding_call_id`. The CI script never hit this because it builds contexts only after the strict selector.
19. In `run_pipeline.py`, the first Batch-3 agent re-normalized the Batch-2 handoff: the refresh manifest included Batch-1 `<run>/intermediate/funding` artifacts that the pipeline and Batch-2 never normalize. Call-scoped normalization now skips that folder (refresh roots and the existing contract are unchanged). Detected by the new Batch-3 contract.
20. A regulation problem statement ran on into a second crawled page (`[Depth 1] … | https://…`, 2,085 chars); problem text is now cut at any serialized page header.
21. Adaptive research used the Python repr of OpenAlex "concepts" as evidence text when a work had no abstract; such works are no longer evidence.

Results on the fresh clone:
- Suite: **1234 passed, 0 failed**, 7 skipped.
- CI chain: Batch-1 `ci-batch1-20261120T074311855366Z` **ACCEPTED, 0 blockers**, all sources clean, 1/1 context ready → Batch-2 **ready_for_batch3** (4 accepted) → Batch-3 **ready_for_batch4**, 0 issues; run IDs match at every hand-off.
- `run_pipeline.py`: exit 0; Batch-3 `ready_for_batch4` without modifying the handoff; synthesis and ideas succeeded; proposals 0 because every idea failed the external-novelty gate → `completed_no_proposal`.
- Grounding: status, deadline, RIA type, research-problem quote, submission link, multi-actor condition and both Annex B passages match the official sources verbatim.

## Remaining-defects cycle (2026-11-20, commit 3a926d7, fresh clone)

Each fix was chosen by testing candidate designs against real run data first; rejected designs are noted.

Batch-1
- Opportunity-specific code removed. UDAX literals (7 sites, incl. status cues) → one general dated "Apply to <Programme> <year>" CTA rule (`_named_application_cta`). Live: title "UBRI Digital Asset Xcelerator (UDAX) 2027", OPEN, real application form link.
- `PA-27-102` hard-coded title removed: it was not the official title. Mirror pages with a generic title now get title/status/close date from an exact-number Grants.gov lookup (`grantsgov_title_for_number`); no match → nothing invented. NIH/Grants.gov guide pages are Cloudflare-blocked (HTTP 403); the Grants.gov API is not.
- Dead Solana heading pattern and two named SBIR topics removed; NSF umbrella IDs and the I-DAPT Chanakya scoped notice moved to `sources/funding_sources.yaml`. CI script: EIT companion check generalized; Climate KIC `download/72240` special case removed.
- Measured, no change: of 4 open in-domain research topics rejected only for a missing problem, 3 have no problem statement in the official text; 1 is borderline.

Batch-2
- Mission gate: a pass now needs a call-specific phrase, a matched mission phrase, or a domain anchor term (required-anchor group, else strong terms; brand-only terms excluded). Rejected design: call-title word overlap (kept Protocol Labs, dropped ESG papers). Records carry `relevance_tier`.
- Problem statement: one bounded source sentence passing the problem-signal test, excluding listings, dates, instructions, code, URLs and page dumps. Rejected designs: joining snippets (up to 12 KB), reusing the extractor as-is (selected page dumps), "first clean sentence" (selected non-problems). Collector previews were measured as the main junk source (16 long "Selected Problem" values), so they are filtered too. Evidence identity uses source text to avoid placeholder collisions.

Batch-3
- Tagging/clustering text excludes serialized page dumps (Protocol Labs: 7 noise tags → 0; other records unchanged).
- Cache manifest is content-addressed (relative paths, no mtimes; schema_version 8); copied/moved runs reuse their cache.

Verification (fresh clone of 3a926d7): suite **1247 passed, 0 failed**; CI steps 2–7 all real work; Batch-1 **ACCEPTED** (`ci-batch1-20261120T090818848578Z`, 0 blockers, all sources clean); Batch-2 **ready_for_batch3** (3 records, tiers recorded, max problem 131 chars, 0 dumps); Batch-3 **ready_for_batch4**; `run_pipeline.py` exit 0, `completed_no_proposal` (novelty gate).

Residual, documented: the keyword problem-signal test still accepts sentences that merely mention "risk" (e.g. CAD Trust "guarding against double-counting risk"); changing it would alter verified Batch-1 extraction. Batch-1 #2 (ESG scope vs blockchain focus) awaits an owner decision.

## Deep-check defects cycle (2026-11-20)

The deep groundedness audit found these defects; each fix has a regression test in `tests/test_deep_check_fixes.py` (11 of its 13 original cases failed on the old code).

Batch-1
- Cascade calls (`competitive-calls-cs/<id>`) were labelled with the parent topic's code (live: 45656490 shown as `HORIZON-CL6-2024-CLIMATE-01-5`, closed 2024). They now get `EU-CS-<id>`; the parent topic is kept as `parent_topic_identifier` evidence.
- Two of six portal topics skipped the official topicDetails status check because their search metadata looked complete. Every current portal topic is now checked (live: 102/102 portal topics cross-checked).
- `zero-emission` and `low-emission transport` are ESG strong terms but were missing from the ESG anchor group; they have been added.
- Merges from companion FAQ pages (live: Women TechEU) added appeal steps, submission timing and "perks via a Slack community" to eligibility. Only sentences that pass the substantive-eligibility test and are not procedural are now kept.

Batch-2
- The lexical gate rejected on-topic evidence that used different wording (e.g. living-lab restoration papers). It now has an additive `semantic_call_match` tier, which uses real sentence-transformer cosine ≥ 0.45 against the call title and problem. It never uses TF-IDF or hash fallbacks; if the model is unavailable, the gate stays lexical. The tier is enabled by `run_pipeline.py` and `run_batch2.py` via `RIF_SEMANTIC_GATE`, and CI installs CPU torch and sentence-transformers. The threshold was calibrated on one live call (on-topic 0.450–0.552; off-topic ≤ 0.436) and should be recalibrated as more calls are observed.

Batch-3 / ideas
- `#topic-security` matched "wildfire vulnerability" and "auditability". The security keywords now require security context.
- Opportunity and benefit framing ("offer a unique opportunity", "will help … guarding against double-counting risk") is no longer selected as a problem statement. This resolves the residual noted in the previous cycle.
- Idea templates (ESG carbon tokenization/MRV, etc.) were applied to ideas whose evidence never mentioned them. Templates are now unlocked only by the resolved problem statement; otherwise the hypothesis quotes the problem and the dataset need says "Not yet determined".
- Idea titles are built from the problem's own words, or say "(problem unresolved)". Authors are labelled as candidate contacts from the evidence, not as confirmed collaborators.

Tests
- The suite was rewriting the tracked `sources/source_registry.json` (Batch-2 registry preparation used the default path). Test runs now use a per-test copy, plus a session guard that fails if the tracked file changes.

Not code defects
- Stale "UPCOMING" status on some EU topics is publisher lag; it resolves on re-run.
- On this machine the local DNS resolver intermittently failed to resolve `www.sbir.gov` (8.8.8.8 resolves it), so two live Batch-1 runs were correctly BLOCKED on SBIR coverage. In the first run the domain resolved, but crawl4ai timed out on `page=5`.

## Batch-1 per-URL audit cycle (2026-11-20)

All 13 Batch-1 source URLs and every EU record were re-checked against the live publisher data before and after the fixes. Regression tests: `tests/test_batch1_url_audit_fixes.py` (23 of 25 fail on the old code).

Fixed
- **Problem sentences (funding only):** about half of the extracted EU "problems" were instructions, encouragements, expected outcomes, definitions, list items or fragments, mostly picked because of "risk" inside "risk assessment/management". All of these are now rejected, the EU "Expected Outcome" section is skipped, and real problem wording is recognised ("gaps and uncertainties remain", "backfire"). On replayed official text: D1-07's encouragement was dropped, D1-10 gets its real problem, and D1-12 (previously a false negative) is now selected. In the live run, all 22 remaining EU problem sentences are genuine.
  - One existing test expected the EU participation-restriction rationale ("vulnerabilities and high-risk dependencies…") to be extracted as a problem. It is boilerplate repeated across topics, so the test now asserts the opposite.
- **Themes:** "Carbon and MRV" (and the other secondary themes) only when the call text names them.
- **Domain anchors and vocabulary:** title anchors match "zero emission" as well as "zero-emission". ESG now includes decarbonisation word forms, climate neutrality and greenhouse gas; this adds 11 genuine decarbonisation topics plus one borderline transport-dissemination CSA (D6-12), none of them selected.
- **Labels:** the missing-anchor reason names the actual domain. Priority keywords are whole words.
- **EU collector:** cancelled topics are filtered (identifier `-CANCELLED` or an explicit notice; a proposed cancellation becomes a warning). Filtered records are itemised (title, URL, reason) in the summary.
- **Hard-coding removed:** the NEAR and Superteam hostname checks and the site-specific CTA phrases are replaced by generic rules.
- **Status evidence:** cites the sentence that closed a programme (Flashbots README: "grant giving through FRP is currently paused").
- **Dates and titles:** the deadline prefers "last day to submit" over a review cut-off; the opening date no longer crosses a deadline label (filpgf.io: now 16 Jun 2026); announcement verbs are stripped from titles ("General Track").
- **Resilience:** robots.txt gets one late retry for DNS/connection errors; the page-retry timeout cap went from 20 s to 40 s (sbir.gov page 5).
- **Semantic gate consistency (Batch-2/3):** the semantic pass is on by default inside the gate. Batch-3 had re-applied the gate without the flag and dropped 3 of 8 records ("Batch-3 modified the Batch-2 normalized handoff").

Verified live: Batch-1 `ci-batch1-20261120T161128738797Z` is **ACCEPTED, 0 blockers**, with all 13 sources clean. SBIR is VERIFIED; its 3 NIH parent announcements match Grants.gov (PA-27-100/101/102) and sbir.gov (open 5 Aug 2026; next due date 5 Jan 2027). All 112 EU records match the official topicDetails on title, opening date, deadline, budget, action type and research intent, and every problem sentence appears verbatim.

Not changed (documented)
- Non-Horizon EU programmes (Erasmus, CERV, Creative Europe, DIGITAL…) publish eligibility in separate call-document PDFs, so they are filtered as missing eligibility. None is in scope.
- NEAR records keep slogan titles ("User-Owned"). No generic rule can rename them without risking verified titles.
- UDAX and the Superteam Startup Accelerator Grant are rejected for lacking independent proof that applications are open. This can't be verified from their pages.
