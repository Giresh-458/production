# System Architecture

The Research Intelligence Framework (RIF) is built around modular collection,
processing, and intelligence agents. The canonical orchestration logic lives in
`run_pipeline.py`; the legacy `run_full_pipeline.py` command delegates to it
to avoid maintaining two divergent pipeline implementations.

## Core Modules

### 1. Agent Registry (`core/agent_registry.py`)
Provides a unified interface for collection, processing, and intelligence agents.
The registry isolates orchestration from individual agent implementations and
validates run inputs and agent response structures.

### 2. Canonical Orchestrator (`run_pipeline.py`)
Coordinates the funding-to-proposal lifecycle. It creates a unique run-scoped
output root, qualifies current funding opportunities, filters for research intent,
dispatches shared collection work, records gate decisions, and refuses to
continue when required evidence or an upstream stage is missing.

### 3. LLM Provider (`core/llm_provider.py`)
Centralizes language-model inference, response validation, and provider routing.
Agents should use this module rather than making unmonitored direct model calls.

### 4. Funding Context Propagation (`core/funding_context.py`)
A `FundingCallContext` carries the selected funder's constraints and research
mission to downstream collectors and intelligence agents. In multi-call mode,
the shared collection phase receives the valid contexts, and per-call processing
uses the matching context for relevance and proposal gates.

### 5. Shared Crawl Manager (`core/shared_crawl_manager.py`)
Provides request deduplication, cache coordination, per-domain concurrency
limits, HTTP collection, and browser fallback. Crawl telemetry and stop reasons
are propagated into batch reports.

### 6. Normalization and Evidence Memory (`core/normalization.py`, `core/evidence.py`)
Normalizes intermediate artifacts into structured records with source URLs,
collection metadata, run IDs, and funding-call attribution. Per-call relevance
checks filter the records before NLP and synthesis.

## Execution Lifecycle

1. **Discover:** Gather current funding-call evidence and apply strict qualification.
2. **Select:** Retain research-eligible calls and construct a validated funding context.
3. **Collect:** Run the 11 downstream collectors and require a healthy majority with evidence.
4. **Normalize:** Convert only run-scoped artifacts and enforce funding-call and mission-relevance gates.
5. **Process:** Tag, cluster, and analyze evidence trends.
6. **Synthesize:** Build baseline artifacts and research ideas with source provenance.
7. **Review:** Generate preliminary proposals only when the required upstream gates pass.

The canonical runner stores reports and artifacts under
`outputs/pipeline_run/<run_id>/`, keeping successive pipeline runs isolated.
