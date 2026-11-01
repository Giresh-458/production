# Batch-2: Collection and Normalization

Batch-2 is the evidence-acquisition and normalization boundary between validated Batch-1 funding-call selection and Batch-3 NLP processing. The standalone runner and the full pipeline use the same collection and normalization implementations.

## Architecture and scope

The execution path is:

1. Load one or more validated Batch-1 FundingCallContext objects.
2. Schedule the 11 downstream collection agents in bounded parallel or sequential mode.
3. Collect source evidence and write provenance-rich intermediate Markdown artifacts.
4. Evaluate collection health. At least 6 of the 11 agents must produce usable evidence; a successful HTTP response without usable evidence does not count.
5. Normalize the current-run artifacts separately for each funding call.
6. Enforce explicit funding-call ownership, run identity, source provenance, and mission relevance.
7. Write structured normalized JSON, indexes, an input manifest, a skip audit, and the SQLite evidence ledger.
8. Validate the Batch-3 handoff and stop. The standalone runner never starts tagging, clustering, trends, synthesis, idea generation, or proposal generation.

### The 11 downstream collectors

| Agent | Collection responsibility |
|---|---|
| Literature | Research papers and technical literature |
| Lab | Research laboratories and academic groups |
| Company | Company deployments, products, and industry evidence |
| Regulation | Official regulation, standards, and policy material |
| Open Source | Open-source projects and repository evidence |
| Practitioner | Practitioner and engineering experience |
| Investment | Investment and market activity signals |
| Failure | Incidents, failures, outages, and negative outcomes |
| Data Availability | Datasets and data-source availability |
| Hackathon | Hackathons, challenges, and competition problem statements |
| Expert | Domain experts and technical specialists |

The Funding Agent belongs to Batch-1 discovery and is not one of these 11 downstream collectors.

## Batch-1 handoff

A normal full-pipeline run writes validated contexts to:

    outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json

The JSON object contains the Batch-1 run ID and a funding_contexts array. The standalone runner reads this file directly and does not rerun Batch-1.

Before collection, the standalone runner validates:
- FundingCallContext schema and non-empty, unique funding_call_id values.
- Confirmed OPEN_CALL status.
- At least one absolute HTTP(S) official source or application URL.
- Substantive applicant eligibility evidence.
- Explicit research intent.
- Known research-context confidence.
- Source-grounded challenge, evidence references, inference provenance, and research-intent evidence.
- Consistent research-context and selected-call funding IDs.

Do not manually invent or edit funding-call context values. Invalid or incomplete contexts are rejected before collection begins.

## Windows 10/11 and VS Code setup

Use Python 3.11 or newer; the project declares Python >=3.11. From PowerShell:

    py -3.11 --version
    git clone https://github.com/Giresh-458/production.git
    cd production
    git checkout main
    git pull origin main
    py -3.11 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python --version
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m playwright install chromium

Open the repository folder in VS Code and select the interpreter at .venv\Scripts\python.exe.

### Run the deterministic tests

    python -m pytest -q

Focused Batch-2, agent-contract, and normalization checks:

    python -m pytest tests/test_all_collection_agents.py tests/test_manual_url_collection.py tests/test_batch2_runner.py tests/test_normalization_skip_audit.py tests/test_agent_normalization_refresh.py tests/test_batch4_normalization.py tests/test_multi_call_evidence_attribution.py -q

These tests use controlled fixtures and mocks where appropriate. They do not prove that every live external source is reachable today; live source availability must be assessed from the actual collection report.

## Run Batch-2 independently

Plan the run without fetching sources or normalizing evidence:

    python scripts/pipeline_tools/run_batch2.py --funding-context-file "outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json" --profile smoke --dry-run

Run a complete refresh profile:

    python scripts/pipeline_tools/run_batch2.py --funding-context-file "outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json" --profile refresh --execution-mode parallel --max-workers 6

Available profiles:
- smoke: one item per configured source and a short bounded timeout.
- regression: bounded source collection for regression checks.
- refresh: full configured source limits with a bounded global timeout.

Use --execution-mode sequential to troubleshoot one collector at a time. Use --scheduled-refresh only when intentionally honoring configured source refresh intervals instead of forcing a live fetch. Use --output-root to change the output location and --run-id to choose an explicit unique run ID.

Every run receives a new output directory. The runner refuses to reuse a non-empty run directory so stale artifacts cannot silently mix with the current execution.

### Exit codes

- 0: normalized handoff is ready for Batch-3, or a dry run completed successfully.
- 1: input or execution exception.
- 2: collection health or normalized handoff did not meet the readiness gate.

## Output contract

    outputs/batch2_runs/<batch2-run-id>/
      intermediate/
        <agent>/
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
            skipped_artifacts.json
            evidence_ledger.db
      workflow/
        funding_contexts.json
        shared_downstream_collection_plan.json
        batch2_report.json

The workflow/funding_contexts.json copy preserves both the Batch-1 run ID and Batch-2 run ID, together with the validated contexts. batch2_report.json records collector health, per-call normalization status, skipped artifacts, normalization errors, and handoff validation errors.

A call is marked ready_for_batch3 only when:
- The collection health gate passes.
- Normalized records exist.
- Required normalized outputs and the evidence ledger exist.
- No normalization errors were reported.
- Every handoff record has the current Batch-2 run ID and selected funding-call ID.
- Every record has a usable HTTP(S) source URL and NORMALIZED status.

A successful HTTP request alone is not evidence of successful collection. The live health gate retains the existing policy: at least 6 of the 11 collectors must produce usable artifacts. Each of the 11 agents must still pass its deterministic contract and regression tests.

## Normalization, provenance, and isolation

Normalization discovers intermediate Markdown artifacts under the run's intermediate roots and preserves structured fields such as title, source identity, source URL, collection agent, layer, research area, evidence snippets, problem/context summary, timestamps, content hash, run ID, and funding-call attribution.

The normalization audit distinguishes:
- errors.json: malformed artifacts or processing/ledger errors.
- skipped_artifacts.json: intentional exclusions with a reason, including stale or unknown run IDs, conflicting explicit funding-call ownership, missing/unsupported funding attribution, ambiguous broad multi-call attribution, downstream insights files, and unrelated Markdown files.

An explicit funding_call_id is authoritative. A conflicting explicit owner cannot be overridden by a secondary funding_call_ids list. Shared unassigned evidence may be attributed only through the existing bounded multi-call rules. Each funding call receives its own mission-relevance evaluation and normalized output.

The input manifest fingerprints the relevant Markdown inputs and selected funding context. Cache refresh checks detect additions, deletions, content changes, timestamp-preserving edits, context changes, and older caches that do not contain the skip audit. Intermediate Markdown and normalized JSON outputs use atomic file writes.

A call with malformed evidence, invalid provenance, missing run/call identity, normalization errors, or no relevant records is not marked ready_for_batch3. Inspect batch2_report.json, normalization/errors.json, normalization/skipped_artifacts.json, and workflow/mission_relevance_gate.json for the exact reason.

## Full pipeline execution

The canonical full pipeline remains:

    python run_pipeline.py

The backward-compatible entry point remains:

    python run_full_pipeline.py

The full pipeline runs Batch-1, then the same 11-agent collection and per-call normalization implementation, and continues to Batch-3 only after upstream collection and normalization gates pass. The standalone runner is for independently executing and verifying Batch-2 without rerunning Batch-1 or starting Batch-3.

## Runtime dependencies and external services

The Python dependencies are declared in requirements.txt and pyproject.toml. The Batch-2 code paths use the repository's existing HTTP and HTML collection clients, Crawl4AI/Playwright for browser-rendered sources, PDF extraction support, YAML configuration loading, deterministic semantic ranking, and the built-in sqlite3 module for the evidence ledger. Install the repository requirements rather than installing a separate, incompatible crawler stack.

Playwright's Chromium browser is installed separately with python -m playwright install chromium. Live configured collection requires outbound network access to the selected source sites. The default Batch-2 path does not require Docker, an external database, or a local Ollama service. Existing Docker-related files are left unchanged.

## Ollama and embedding dependencies

The standalone Batch-2 runner explicitly uses:
- analysis_mode="collect_only"
- allow_llm_refinement=False
- allow_llm_analysis=False

Therefore the normal Batch-2 collection and normalization path does not call Ollama. The Literature Agent's LLM synthesis option remains disabled. Optional analysis modes in collection agents can call core.llm_provider.generate; later intelligence stages may also use the configured local model.

The existing configuration is preserved: model qwen3.5:4b at http://localhost:11434/api/generate in config.yaml.

Optional Windows verification for workloads that use Ollama:

    ollama --version
    ollama list
    ollama pull qwen3.5:4b
    python -m core.llm_provider

Run ollama pull only if qwen3.5:4b is not already present. If the local Ollama service is not running, start it using the Ollama desktop service or run ollama serve in a separate PowerShell window.

The Literature Agent also calls semantic_scores during collection. By default, RIF_USE_TRANSFORMER_EMBEDDINGS is false, so it uses the deterministic TF-IDF ranking path and does not load a transformer model. If explicitly enabled, the optional SentenceTransformer path can load sentence-transformers/all-MiniLM-L6-v2. This is an embedding dependency, not an Ollama dependency. The same optional embedding switch can affect later semantic-retrieval stages.

## Troubleshooting

- ModuleNotFoundError: activate .venv and reinstall requirements in that environment.
- Browser or Playwright errors: run python -m playwright install chromium from the active environment.
- Ollama connection errors: they do not block the default Batch-2 path; they matter only for optional LLM-enabled stages.
- FileExistsError for a run directory: choose a new --run-id or let the runner generate one.
- Exit code 2: inspect workflow/batch2_report.json and the per-call errors/skipped-artifact and mission-relevance reports before retrying.
- A live source that is unavailable is recorded as a collection failure/coverage gap; do not interpret it as proof that the source has no evidence.
