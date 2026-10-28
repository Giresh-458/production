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

## Acceptance decision

**`BLOCKED` pending a new full configured scan and resolution of the remaining blockers.** This log does not claim that the budget change fixed the live audit. The next run must be inspected at its exact commit, including `summary.json`, `records.json`, `selected.json`, source summaries, and failure logs. Do not merge or start Batch 2 based on deterministic tests alone.
