# Research Intelligence Framework (RIF)

RIF is a fully automated, multi-agent research intelligence system designed for discovering and synthesizing high-value blockchain research problems. It collects signals across 12 distinct real-world domains, processes them through NLP clustering pipelines, and automatically generates preliminary grant proposals aligned with active funding opportunities.

## Architecture

RIF uses a structured, sequential pipeline to move from raw web data to structured proposals:

1. **Context Initialization**: The orchestrator scans for open grant opportunities and ranks them based on funding amount, relevance, and deadline. The strongest opportunity becomes the strict `FundingCallContext` for the entire run.
2. **Context-Driven Collection**: 11 concurrent downstream agents scrape the web (using headless browsers and fallback engines), filtering all signals through the active funding context. Irrelevant signals are discarded.
3. **Data Normalization & Storage**: Extracted signals are normalized and saved into an intermediate SQLite ledger and Markdown artifacts for traceability.
4. **NLP Processing**:
   - **Tagging**: Normalizes research layers and domains.
   - **Clustering**: Groups conceptually identical problems via TF-IDF cosine similarity. (Note: LLM ambiguity resolution is supported but disabled for batch orchestrations to guarantee deterministic completion without API timeouts).
   - **Trends**: Identifies velocity and recurrence of themes.
5. **Baseline Intelligence**: Synthesizes processed evidence into structured artifacts with unbroken source provenance tracking.
6. **Proposal Generation**: Drafts preliminary grant proposals aligned with the funding context. **Safety gate:** The system refuses to generate proposals if the underlying idea lacks sufficient external novelty validation across multiple collection layers.

## Core Components

- **12 Collection Agents**: Fetch and filter raw signals (Funding, Literature, Labs, Companies, Regulation, Open Source, Practitioners, Investment, Failure, Data Availability, Hackathons, Experts).
- **3 Processing Agents**: Normalize, group, and trend the data (Tagging, Clustering, Trends).
- **3 Intelligence Agents**: Generate insights and proposals (Ideas, Synthesis, Proposals).
- **Shared Orchestrator**: Located in `core/`, handles caching, LLM routing, and network boundaries.
- **Testing**: Over 390 deterministic Pytest regression tests validate evidence gates and limits.

## Invocation

The entire system is executed autonomously via a single entrypoint:

```bash
python run_full_pipeline.py
```

This script enforces:
- Strict live caching bypass (`force_refresh=True`)
- SQLite database persistence for memory
- Centralized LLM routing via `core/llm_provider.py`

*(For production and CI/CD deployment specifics, please refer to [docs/CI_CD_PIPELINE.md](docs/CI_CD_PIPELINE.md)).*
