# Batch-2: Collection and Normalization

Batch-2 is the evidence acquisition and normalization boundary between validated Batch-1 funding-call selection and Batch-3 NLP processing.

## Scope

The standalone runner executes the 11 downstream collection agents:

- Literature
- Lab
- Company
- Regulation
- Open Source
- Practitioner
- Investment
- Failure
- Data Availability
- Hackathon / Challenge
- Expert

It then:
1. Preserves the Batch-1 run ID and all selected funding-call contexts.
2. Runs the collectors in collect_only mode.
3. Preserves evidence, source URLs, agent identity, run provenance, and funding-call attribution in intermediate artifacts.
4. Applies the per-call mission-relevance gate.
5. Normalizes accepted evidence into normalized/records.json, by_area.json, by_layer.json, and the evidence ledger.
6. Validates that every handoff record belongs to the active run and selected funding call and has a usable HTTP(S) source URL.
7. Writes workflow/batch2_report.json and stops. It does not start tagging, clustering, trends, synthesis, ideas, or proposals.

## Batch-1 handoff

A normal full-pipeline run writes the validated selected contexts to:

    outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json

This file is a JSON object containing the original Batch-1 run ID and a funding_contexts array. It is the supported input to the standalone Batch-2 runner. Do not manually invent or edit funding-call context values.

## Windows / VS Code execution

From the repository root in PowerShell:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m playwright install chromium

Run a complete refresh profile:

    python scripts/pipeline_tools/run_batch2.py --funding-context-file "outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json" --profile refresh --execution-mode parallel --max-workers 6

For a bounded smoke run, use --profile smoke. For a bounded regression run, use --profile regression. Use --scheduled-refresh only when intentionally honoring source refresh intervals instead of forcing a live fetch.

Each execution writes to a new directory under outputs/batch2_runs/<run-id>/. The runner refuses to reuse a non-empty run directory so that stale artifacts cannot silently mix with the current collection.

## Output contract

    outputs/batch2_runs/<run-id>/
      intermediate/
        <agent>/
          <research-area>/
            *.md
      calls/
        <funding-call-id>/
          workflow/
            selected_funding_call.json
            mission_relevance_gate.json
          normalized/
            records.json
            by_area.json
            by_layer.json
            input_manifest.json
            errors.json
            evidence_ledger.db
      workflow/
        shared_downstream_collection_plan.json
        batch2_report.json

A call is marked ready_for_batch3 only when normalized records exist, normalization reports no errors, and every record passes run, funding-call, source-URL, and normalization-status checks. Otherwise the report identifies the blocker and the runner exits non-zero.

## Local LLM requirement

The standalone Batch-2 runner does not require Ollama or a local model. It explicitly uses analysis_mode="collect_only" and allow_llm_refinement=False; Literature Agent LLM analysis remains disabled by default. Configured collection still requires network access and the browser dependencies needed by sources that require browser rendering.

Optional in-agent analysis and later Batch-3 intelligence stages are separate workloads and may use the configured Ollama model in config.yaml. The existing model configuration is not changed by this Batch-2 work.
