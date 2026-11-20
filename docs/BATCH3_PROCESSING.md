# Batch-3: NLP Processing (tagging → clustering → trends)

Batch-3 consumes one call's Batch-2 normalized handoff
(`<run>/calls/<funding_call_id>/normalized/`) and writes:

- `processed/tags/tagged_records.json` — topic and signal-type tags with evidence spans
- `processed/clusters/clusters.json` — evidence-aware problem clusters (TF-IDF, threshold 0.20, no LLM)
- `processed/trends/multi-area-trends.json` — counts, topic series and trend direction

The stage calls are the ones the full pipeline has always used; `core/batch3.py`
holds them in one place (`batch3_stage_plan`) and adds an output contract.

## Contract (`core.batch3.validate_batch3_outputs`)

A call is `ready_for_batch4` only when:

- the normalized handoff is non-empty;
- every handed-off record is tagged exactly once and clustered exactly once;
- tags, clusters and trends carry the call's `funding_call_id`, and tags preserve the handoff `run_id`;
- trend counts equal the handoff record count and the cluster count;
- the Batch-2 normalized handoff files are byte-identical before and after Batch-3.

A stage status of `partial_success` with nothing processed does not pass.
Blocked statuses: `blocked_missing_handoff`, `blocked_processing`, `blocked_validation`.

Cluster signal types that assert a problem (`research_problem`, `practitioner_pain_point`,
`failure_mode`, `engineering_gap`, `market_need`, `prototype_challenge`) are assigned only when
the member has an extracted problem statement; otherwise the neutral `general_signal` is used.

## Running

Full pipeline: `run_pipeline.py` runs Batch-3 per call after Batch-2 and writes
`calls/<id>/workflow/batch3_report.json`; synthesis starts only when Batch-3 is `ready_for_batch4`.

Standalone, on a completed Batch-2 run (only calls Batch-2 marked `ready_for_batch3`):

    python scripts/pipeline_tools/run_batch3.py --batch2-run-dir outputs/batch2_runs/<run_id>

Exit code 0 = every processed call `ready_for_batch4`; 2 = blocked or no ready call. The
runner re-validates each Batch-2 context and requires the Batch-1/Batch-2 run IDs in
`workflow/funding_contexts.json` to match `workflow/batch2_report.json`.

CI (`eu-funding-tests.yml`) runs this step after the bounded Batch-2 smoke whenever Batch-2
reached `ready_for_batch3`.
