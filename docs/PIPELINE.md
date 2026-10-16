# RIF Autonomous Pipeline

This document describes the canonical funding-to-proposal workflow implemented by
`run_pipeline.py`. The historical `run_full_pipeline.py` entry point delegates
to this runner so both commands use the same quality gates and output layout.

---

## Stage 1: Initialization and funding context selection

1. **Run isolation:** A run ID creates a dedicated root at
   `outputs/pipeline_run/<run_id>/`. Intermediate, processed, synthesis, idea,
   proposal, and workflow artifacts for that run are kept under this root.
2. **Funding discovery:** The funding agent scans `sources/funding_sources.yaml`
   using the configured collectors and official structured APIs where available.
   Persistent source/funding memory may be reused, but current-call selection is
   based on the current discovery response, not stale database rows.
3. **Qualification:** Only calls passing the strict funding selector are considered.
   It checks explicit current-open status, deadline, source/application evidence,
   substantive applicant eligibility, and strong canonical RIF-domain evidence.
   Closed, upcoming, unknown-status, weak-domain, generic, or eligibility-free
   records are rejected.
4. **Research-intent gate:** Qualified calls are checked for research intent.
   Calls that do not establish a research mission are excluded from proposal work.
   The runner writes the selected call and rejected alternatives to the workflow
   report; if no valid research opportunity remains, it stops before collection.

---

## Stage 2: Shared multi-agent collection

Once research-eligible calls are selected, the runner executes the 11 downstream
collection agents through `core/batch_runner.py`. Collection is shared across the
selected call contexts to avoid duplicating the same web crawl.

1. **Target loading:** Each collector reads its configured sources under
   `sources/`.
2. **Bounded collection:** Agents use the shared crawl manager for request
   deduplication, cache control, per-domain concurrency limits, deadlines, and
   HTTP/browser fallback behavior.
3. **Funding-context propagation:** Each collector receives the selected funding
   contexts; collected artifacts preserve their run and funding-call metadata.
4. **Run-scoped output:** Collector artifacts are written to
   `outputs/pipeline_run/<run_id>/intermediate/<agent_name>/`, not a shared
   directory containing files from older full-pipeline runs.
5. **Collection-health gate:** The pipeline requires a strict majority of the
   configured collectors to complete with usable outputs. For the current set of
   11 downstream collectors, at least 6 must be healthy. A batch with too little
   evidence stops instead of silently proceeding.

The collection report is saved to
`outputs/pipeline_run/<run_id>/workflow/shared_downstream_collection_plan.json`.

---

## Stage 3: Per-call normalization and evidence gating

For each selected funding call, the runner normalizes both the run-level collector
artifacts and any call-specific adaptive-research artifacts into that call's
output root.

1. **Schema alignment:** Markdown artifacts are converted into structured records
   with title, source URL, timestamps, collection agent, research area, evidence
   snippets, and machine-readable provenance.
2. **Funding-call attribution:** The normalizer filters records to the active
   funding-call ID.
3. **Mission relevance:** Each record is checked against the active
   `FundingCallContext`; irrelevant records are excluded and recorded in the
   gate report.
4. **Empty-evidence stop:** If no usable normalized evidence remains, the runner
   stops proposal work for that call rather than inventing a research gap.

Normalized records and the mission-relevance report are stored under
`outputs/pipeline_run/<run_id>/calls/<funding_call_id>/`.

---

## Stage 4: Natural-language processing

The processing agents run per call against the normalized evidence in that call's
output root.

1. **Tagging** standardizes labels and canonical research areas.
2. **Clustering** groups similar research problems using deterministic TF-IDF
   cosine similarity in batch mode; LLM ambiguity resolution is disabled for this
   path to reduce timeout and nondeterminism risk.
3. **Trend analysis** evaluates recurrence and velocity from collected evidence.
4. **Stage gate:** Processing failure or a non-success status prevents synthesis
   and proposal generation for the affected call.

Outputs are stored in the call root under `processed/`.

---

## Stage 5: Synthesis, idea generation, and proposal review

1. **Synthesis:** The synthesis agent creates traceable baseline artifacts and
   a manifest that preserves source URLs.
2. **Idea generation:** The idea agent derives candidate research ideas and
   applies external novelty validation.
3. **Proposal generation:** The proposal agent uses the original selected
   `FundingCallContext` and only proceeds after upstream stage gates pass.
4. **Final reporting:** Each call receives a `workflow/pipeline_report.json` and
   `workflow/pipeline_manifest.json`. The run-level summary is written to
   `outputs/pipeline_run/<run_id>/workflow/pipeline_summary.json`.

A proposal is a preliminary review artifact, not a submission-ready grant
application. Human review is still required.
