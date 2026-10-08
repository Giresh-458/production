# System Architecture

The Research Intelligence Framework (RIF) is built around a modular, multi-agent architecture designed to ensure high-fidelity signal extraction and strict provenance tracking.

## Core Modules

### 1. Agent Registry (`core/agent_registry.py`)
Provides a unified interface for all agents. The registry allows the orchestrator (`run_full_pipeline.py`) to invoke any collection or processing agent dynamically without tight coupling to its internal implementation. 

### 2. LLM Provider (`core/llm_provider.py`)
A centralized routing layer for all language model inferences. 
- Prevents agents from making direct, unmonitored calls to OpenAI/Anthropic/Ollama.
- Enforces strict JSON formatting and response validation.
- Automatically handles network routing (e.g., resolving `OLLAMA_HOST` in containerized environments).

### 3. Funding Context Propagation (`core/funding_context.py`)
The most critical constraint in the system. 
- When the pipeline starts, a `FundingCallContext` object is instantiated from the strongest active grant opportunity.
- This context is passed sequentially to all downstream agents.
- Agents use this context to filter web signals, ensuring all collected data is directly relevant to the funding parameters.

### 4. SQLite Memory (`core/evidence_ledger.py` & `core/db.py`)
To prevent memory bloat and API timeout crashes during massive batch runs, RIF relies on local SQLite databases rather than in-memory dictionaries.
- **Deduplication**: `shared_crawl_manager.py` checks URL hashes against the database to prevent re-scraping.
- **Persistence**: All normalized signals are saved to disk, allowing processing agents to run as separate asynchronous jobs if necessary.

## The Execution Lifecycle

1. **Initialization**: `run_full_pipeline.py` clears temporary cache files and forces a full source refresh.
2. **Context Selection**: The orchestrator ranks and selects the target grant opportunity.
3. **Scatter**: 11 Collection Agents are spawned sequentially (or concurrently depending on the runner) to scrape domains (e.g., GitHub, arXiv, SEC filings).
4. **Gather**: The extracted intelligence is normalized and written to the SQLite ledger.
5. **Process**: NLP layers apply tags, cluster identical problems (via deterministic TF-IDF), and identify temporal trends.
6. **Synthesize**: The synthesis and idea agents compile the structured ledger into a final markdown artifact.
