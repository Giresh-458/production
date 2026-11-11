# Batch-2: Collection and Normalization

Batch-2 is the evidence-acquisition and normalization boundary between validated Batch-1 funding-call selection and Batch-3 NLP processing. The standalone runner and the full pipeline use the same collection and normalization implementations.

- Batch-2 source-registry synchronization is filtered to the 11 downstream collector agents. It does not perform an unfiltered registry sync that could rewrite or deprecate Batch-1 funding-source entries.

## Architecture and scope

The execution path is:

1. Load one or more validated Batch-1 FundingCallContext objects.
2. Schedule the 11 downstream collection agents in bounded parallel or sequential mode.
3. Collect source evidence and write provenance-rich intermediate Markdown artifacts. Configured recursive crawls in Company, Hackathon, Open Source, and Regulation use each source's name/focus vocabulary instead of one global cross-domain link vocabulary, reducing unrelated sibling-page capture.
4. Audit persisted Markdown artifacts against the collector response manifest, expected agent directory, current run ID, and matching source/provenance URLs. Invalid artifacts are quarantined, not silently normalized. Symlinked or redirected run, intermediate, workflow, and quarantine roots are rejected before discovery. Symlinked/junction agent roots and nested aliases are never traversed; only the link itself is quarantined, so another collector's original evidence cannot be moved through an alias. Collector output manifests that point outside the agent directory, point to missing files, or conflict with a non-empty persisted-file manifest are critical blockers. A collector that reports an error after persisting a valid artifact may still count as partial success; the persisted artifact is authoritative, while the count discrepancy remains visible in the audit.
5. Evaluate collection health from verified persisted artifacts. At least 6 of the 11 agents must produce usable evidence; a successful HTTP response or an inflated response counter without a valid artifact does not count.
6. Normalize the current-run artifacts separately for each funding call.
7. Enforce explicit funding-call ownership, call-scoped funding_call_ids, original attribution trace, run identity, source provenance, and mission relevance. Canonical domain anchors are retained only when their domain-ranking evidence meets the same raw-score and strong-term threshold as the domain router; verified matched terms remain available as anchors, while generic priorities such as market adoption cannot independently admit unrelated evidence.
8. Write structured normalized JSON, indexes, an input manifest, a skip audit, and the SQLite evidence ledger. Verify SQLite integrity and exact record/run/call/provenance correspondence.


**Evidence promotion rule:** configured live-source artifacts that fail the collector quality gate may remain persisted for audit/review, but they are not eligible for the normalized Batch-3 handoff. The mission gate also rejects Data Availability records whose extracted evidence is only the configured source name/focus metadata, and applies technical anchors from a specific funding-call title when available. Source names, configured focus strings, retrieval queries, and other metadata are not treated as source observations.
9. Validate the Batch-3 handoff and stop. The standalone runner never starts tagging, clustering, trends, synthesis, idea generation, or proposal generation.

The integrated full pipeline has one additional, per-call supplemental `adaptive_research` stage when canonical research-area coverage is insufficient. It is not one of the 11 shared collectors and does not count toward the 11-agent collection-health threshold. Its current-run artifacts may be included in the full pipeline's call-scoped normalized handoff under the explicit supplemental identity `adaptive_research`; the standalone Batch-2 runner does not execute that stage.

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

The JSON object contains the Batch-1 run ID and a `funding_contexts` array. The standalone runner reads this file directly and does not rerun Batch-1. It accepts either (a) a global `batch1_acceptance` attestation with `accepted=true`, `status=ACCEPTED`, `source_coverage_verified=true`, zero blockers, and the same Batch-1 run ID, or (b) a `context_validation` envelope with `status=VALIDATED`, `scope=selected_contexts_only`, the same Batch-1 run ID, and an exact list of validated funding-call IDs. In both cases, the runner independently revalidates every serialized FundingCallContext before starting collection. The selected-context form permits independent Batch-2 execution when an unrelated configured Batch-1 source blocks the separate full-source-coverage audit; it does not mark Batch-1 globally accepted. The normal `run_pipeline.py` flow continues to apply its own current-call qualification and selected-context validation; a Batch-2 smoke result is not evidence that every configured Batch-1 source was covered. Bare context objects/lists and mismatched context IDs are rejected.

Before collection, the standalone runner validates:
- FundingCallContext schema and non-empty, unique funding_call_id values.
- Confirmed OPEN_CALL status.
- At least one absolute HTTP(S) source or application URL supplied by the Batch-1 selector.
- Substantive applicant eligibility evidence.
- Explicit research intent and non-empty research-intent evidence.
- Known research-context confidence.
- At least one absolute HTTP(S) research evidence reference, source-grounded challenge, and inference provenance.
- Consistent research-context and selected-call funding IDs. If the Batch-1 selector's canonical call ID differs from the research-context's earlier ID, the boundary factory rebinds it only when a research evidence reference identifies that same selected call's source or application URL, and retains the original ID in `research_context_original_funding_call_id`.

Do not manually invent or edit funding-call context values. Invalid or incomplete contexts are rejected before collection begins.

## Windows 10/11 and VS Code setup

Use Python 3.11 or newer; the project declares Python >=3.11. From PowerShell:

    py -3.11 --version
    git clone https://github.com/Giresh-458/production.git
    cd production
    git checkout main
    git pull origin main
    # To test the unmerged Batch-2 PR branch:
    git fetch origin feat/batch2-collection-normalization-20261101
    git checkout feat/batch2-collection-normalization-20261101
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

    python -m pytest tests/test_all_collection_agents.py tests/test_manual_url_collection.py tests/test_regulation_source_scoping.py tests/test_batch2_runner.py tests/test_normalization_skip_audit.py tests/test_research_mission_grounding.py tests/test_agent_normalization_refresh.py tests/test_batch4_normalization.py tests/test_multi_call_evidence_attribution.py -q

These tests use controlled fixtures and mocks where appropriate. The RIF funding and collection quality-gates workflow keeps the full Batch-1 source-coverage acceptance decision strict. Separately, it writes a selected-context handoff for the subset of real Batch-1 selected calls that pass the individual downstream-context checks; invalid selected calls are excluded and recorded in the audit. The bounded Batch-2 smoke runs only against that validated subset, even if an unrelated configured Batch-1 source blocks global coverage. This smoke verifies Batch-2 independently; it does not mark Batch-1 globally accepted or make the full pipeline eligible to proceed. A failed global Batch-1 gate still fails the full quality workflow and must be reported separately from the Batch-2 smoke result.

## Run Batch-2 independently

Plan the run without fetching sources or normalizing evidence:

    python scripts/pipeline_tools/run_batch2.py --funding-context-file "outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json" --profile smoke --dry-run

Run a complete refresh profile:

    python scripts/pipeline_tools/run_batch2.py --funding-context-file "outputs/pipeline_run/<batch1-run-id>/workflow/funding_contexts.json" --profile refresh --execution-mode parallel --max-workers 6

The standalone runner accepts either a globally accepted Batch-1 quality-audit handoff or a selected-context validation envelope emitted by the strict selector for real selected records. For the selected-context form, every context is revalidated by Batch-2: non-empty call title, current OPEN_CALL status, absolute HTTP(S) source/application URL, substantive eligibility, matching call/research-context identity, research intent, confidence, source-grounded challenges, HTTP(S) evidence references, and inference provenance. The exact funding-call IDs and Batch-1 run ID must match the envelope. This selected-context validation is not a global Batch-1 acceptance and cannot authorize the full pipeline to proceed when its global Batch-1 quality gate is blocked. Do not hand-author this envelope or fabricate contexts.

Available profiles:
- smoke: one item per configured source and a short bounded timeout.
- regression: bounded source collection for regression checks.
- refresh: full configured source limits with a bounded global timeout.

Use --execution-mode sequential to troubleshoot one collector at a time. Use --scheduled-refresh only when intentionally honoring configured source refresh intervals instead of forcing a live fetch. Use --output-root to change the output location and --run-id to choose an explicit unique run ID.

Every run receives a new output directory. The runner refuses to reuse any existing run directory, including an empty directory. Exclusive directory creation prevents concurrent runs from sharing the same run ID.

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
        quarantined_intermediate/
          <agent>/
            *.md

The workflow/funding_contexts.json copy preserves both the Batch-1 run ID and Batch-2 run ID, together with the validated contexts. batch2_report.json records collector health, per-call normalization status, skipped artifacts, normalization errors, and handoff validation errors.

The standalone runner and full pipeline share the same normalized-record validator and persisted-artifact audit; a weaker duplicate handoff check must not be introduced in either path.

Before collection, the coordinator synchronizes the configured source registry and builds its refresh plan once. Workers defer registry synchronization so they do not race on the shared registry. If registry preparation fails, collection stops instead of running against stale source lifecycle state.

The batch timeout is cooperative because Python cannot safely kill a running worker thread. A worker that finishes after the deadline is marked `timeout_budget_exceeded`; its output directory is removed. Any late or invalid artifact that remains is quarantined and excluded from normalization. A quarantine failure blocks the handoff.

A call is marked ready_for_batch3 only when:
- The collection health gate passes using verified persisted artifacts.
- Normalized records exist.
- Required normalized outputs exist.
- The SQLite evidence ledger passes `PRAGMA quick_check`, contains exactly the normalized records for this run/call, and its row fields and stored provenance match the JSON records.
- No normalization or ledger-integrity errors were reported.
- Every handoff record has the current Batch-2 run ID and selected funding-call ID, and belongs to one of the expected 11 collection agents.
- Every record has a usable HTTP(S) source URL, NORMALIZED status, a recognized collector identity, stable record/evidence/source/content IDs, a timezone-aware collection timestamp, non-empty evidence text, exactly one funding_call_id equal to the output call, a preserved source_funding_call_ids attribution trace, and a passed mission-relevance decision.

A successful HTTP request alone is not evidence of successful collection. Both the standalone runner and the full pipeline require all 11 expected collector tasks to be present and reject unexpected task names. The live evidence-health gate retains the existing policy: at least 6 of the 11 collectors must produce usable artifacts. Each of the 11 agents must still pass its deterministic contract and regression tests.

## Normalization, provenance, and isolation

Normalization discovers intermediate Markdown artifacts under the run's intermediate roots and preserves structured fields such as title, source identity, source URL, collection agent, layer, research area, evidence snippets, problem/context summary, timestamps, content hash, run ID, and funding-call attribution.

The normalization audit distinguishes:
- errors.json: malformed artifacts or processing/ledger errors.
- skipped_artifacts.json: intentional exclusions with a reason, including stale or unknown run IDs, conflicting explicit funding-call ownership, missing/unsupported funding attribution, ambiguous broad multi-call attribution, downstream insights files, and unrelated Markdown files.

An explicit funding_call_id is authoritative. A conflicting explicit owner cannot be overridden by a secondary funding_call_ids list. Shared unassigned evidence may be attributed only through the existing bounded multi-call rules. Each funding call receives its own mission-relevance evaluation and normalized output.

The input manifest fingerprints the relevant Markdown inputs and selected funding context. Cache reuse requires an exact input-manifest match, a valid empty errors.json, a valid skipped-artifact list, and a healthy SQLite evidence ledger whose rows exactly match normalized record IDs, run/call IDs, source URLs, and content hashes. Missing, corrupt, or mismatched ledgers and prior normalization errors force a rebuild. Cache refresh checks detect additions, deletions, content changes, timestamp-preserving edits, context changes, and older caches that do not contain the skip audit. Intermediate Markdown and normalized JSON outputs use atomic file writes. Shared evidence may appear in multiple call outputs only as separate call-scoped records with distinct evidence IDs; the original multi-call attribution is retained in source_funding_call_ids and ledger provenance.

A call with malformed evidence, invalid provenance, missing run/call identity, normalization errors, or no relevant records is not marked ready_for_batch3. Inspect batch2_report.json, normalization/errors.json, normalization/skipped_artifacts.json, and workflow/mission_relevance_gate.json for the exact reason.

## Full pipeline execution

The canonical full pipeline remains:

    python run_pipeline.py

The backward-compatible entry point remains:

    python run_full_pipeline.py

The full pipeline runs Batch-1, then the same 11-agent collection and per-call normalization implementation, and continues to Batch-3 only after upstream collection and normalization gates pass. The standalone runner is for independently executing and verifying Batch-2 without rerunning Batch-1 or starting Batch-3.

## Runtime dependencies and external services

The Python dependencies are declared in requirements.txt and pyproject.toml. The Batch-2 code paths use the repository's existing HTTP and HTML collection clients, Playwright for browser-rendered sources, PDF extraction support, YAML configuration loading, deterministic semantic ranking, and the built-in sqlite3 module for the evidence ledger. Crawl4AI is included in the repository dependency set for the separate Batch-1 Funding Agent; the standalone Batch-2 runner does not execute the Funding Agent's Crawl4AI traversal. Install the repository requirements rather than installing a separate, incompatible crawler stack.

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

- ModuleNotFoundError: activate .venv and reinstall requirements in that environment. The standalone CLI resolves the repository root from its own file path; invoke it from the repository checkout and do not rely on a globally configured `PYTHONPATH`.
- Browser or Playwright errors: run python -m playwright install chromium from the active environment.
- Ollama connection errors: they do not block the default Batch-2 path; they matter only for optional LLM-enabled stages.
- FileExistsError for a run directory: choose a new --run-id or let the runner generate one.
- Exit code 2: inspect workflow/batch2_report.json and the per-call errors/skipped-artifact and mission-relevance reports before retrying.
- A live source that is unavailable is recorded as a collection failure/coverage gap; do not interpret it as proof that the source has no evidence.


## Design references used for the handoff and provenance fixes

- [Python documentation: initialization of `sys.path`](https://docs.python.org/3/library/sys_path_init.html). When a script is run by file path, Python places the script's directory—not necessarily the repository root—at the first search-path entry. The standalone CLI now resolves and inserts the repository root from `__file__`, and a regression test launches it from an unrelated working directory without relying on `PYTHONPATH`.
- [W3C PROV-O Recommendation](https://www.w3.org/TR/prov-o/). PROV-O models provenance using entities, activities, agents, and their relationships. The funding-context boundary therefore does not silently discard a mismatched source research-context ID: it reconciles the ID only when a research evidence reference identifies the selected call's source/application URL, and persists the prior ID as `research_context_original_funding_call_id`.
- Khan et al., [“Sharing interoperable workflow provenance: A review of best practices and their practical application in CWLProv”](https://doi.org/10.1093/gigascience/giz095), *GigaScience* (2019). The paper motivates preserving workflow inputs, outputs, execution context, and retrospective provenance together so results can be inspected and reproduced. Batch-2's per-run artifacts, input manifest, collector metadata, evidence ledger, and call-scoped handoff checks implement that principle within this repository's existing format.
