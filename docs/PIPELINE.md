# RIF Autonomous Pipeline

This document details the exact, step-by-step data flow of the Research Intelligence Framework (RIF) when executing a full batch run via `run_full_pipeline.py`.

The pipeline is completely autonomous and operates in five distinct, strictly ordered stages.

---

## Stage 1: Initialization & Context Selection

Before scraping general intelligence, the system must establish *why* it is researching.

1. **Cache Reset**: The orchestrator sets `force_refresh=True` and resets crawler statistics to ensure it fetches live data rather than stale cache.
2. **Funding Discovery**: The orchestrator triggers the `funding_agent`. Using the deep-spidering Crawl4AI engine, it scrapes active grant portals (defined in `sources/funding_sources.yaml`) and saves them to `outputs/funding.db`.
3. **Context Selection**: The `execute_selection()` function analyzes the database and ranks the grants based on total funding amount, approaching deadlines, and overall relevance.
4. **Context Lock**: The highest-ranking grant is locked into a global `FundingCallContext` object. If no valid grants are found, the system immediately fast-fails (`NO_VALID_FUNDING_CONTEXT`) to save compute resources.

---

## Stage 2: The Multi-Agent Scatter (Collection)

With the `FundingCallContext` locked, the orchestrator triggers the 11 downstream collection agents.

1. **Target Loading**: Each agent reads its specific target list from `sources/` (e.g., the `literature_agent` reads `sources/literature_sources.yaml`).
2. **Execution**: The agents pass their URLs to the `SharedCrawlManager`, which enforces a strict 2-connection limit per domain and downloads the raw HTML using the dual-engine (Playwright + HTTP) strategy.
3. **Context Filtering**: As the text is extracted, it is passed to the LLM alongside the `FundingCallContext`. The agent is instructed to *only* extract intelligence that directly supports the active grant opportunity. Irrelevant data is discarded.
4. **Output Generation**: The filtered intelligence is written as raw, traceable Markdown files into `outputs/intermediate/<agent_name>/`.

---

## Stage 3: Normalization (The Gather Phase)

The 11 agents output unstructured Markdown files. Stage 3 converts this into structured, queryable memory.

1. **Squash**: The orchestrator calls `save_normalized_collection_records()`.
2. **Schema Alignment**: This function parses every Markdown file in the `intermediate/` directory, extracts the metadata headers, and aligns them into a unified schema.
3. **Database Write**: The normalized records are committed to the central SQLite ledger, making the intelligence available for the heavy NLP processing agents.

---

## Stage 4: Natural Language Processing (NLP)

The Processing Agents analyze the massive SQLite ledger to find patterns.

1. **Tagging Agent**: Reads the database and standardizes the taxonomy (e.g., mapping "Ethereum DeFi" and "Yield Farming" to a normalized `DeFi` tag).
2. **Clustering Agent**: Groups conceptually identical problems across different domains.
   - *Optimization Note:* To prevent LLM timeouts across thousands of records, clustering relies on a deterministic `TF-IDF` cosine similarity matrix. (Pairwise LLM ambiguity resolution is supported in the code but explicitly disabled during batch runs).
3. **Trend Agent**: Analyzes the timestamp metadata of the clusters to calculate the velocity and recurrence of specific research themes.
4. **Outputs**: Processed relationships are saved to `outputs/processed/`.

---

## Stage 5: Intelligence Synthesis & Proposal Generation

The Intelligence Agents take the clustered data and generate final human-readable artifacts.

1. **Synthesis Agent**: Compiles the processed evidence clusters into structured "Baseline Artifacts", explicitly preserving unbroken source URLs so every claim can be audited.
2. **Idea Agent**: Translates the synthesis artifacts into concrete prototype/research ideas. 
   - *Safety Gate:* The Idea Agent enforces an "External Novelty Validation" check. If an idea relies on signals from only a single source, it is flagged as weak/unverified.
3. **Proposal Agent**: Takes the strongest verified Idea, pulls the original `FundingCallContext` from Stage 1, and drafts a preliminary grant proposal tailored exactly to the funder's requirements.
4. **Final Output**: The proposals are written to `outputs/proposals/` and are ready for human review in the Streamlit UI.
