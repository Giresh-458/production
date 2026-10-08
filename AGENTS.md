# AGENTS.md

## 1. System Overview

RIF is organized around modular agent categories:

- collection agents
- processing agents
- intelligence agents

Each agent owns one clear stage or responsibility in the pipeline.

The repository currently has:

- implemented collection agents
- implemented processing agents
- implemented baseline intelligence agents
- a shared agent execution interface
- a registry for uniform invocation
- a Streamlit UI for manual testing and browsing outputs

---

## 2. General Rules

- Each agent should remain modular and independently testable
- Each agent should own one clear responsibility
- All Markdown outputs should be readable and reusable
- Collection agents should default to saving intermediate artifacts first
- All LLM calls must go through `llm_provider.py`
- UI and future orchestration layers must use the shared agent registry rather than direct collection-agent internals

---

## 3. Shared Interface

Every callable collection-agent module should expose:

```python
run_agent(
    mode: str,
    area: str | None = None,
    input_data: dict | None = None,
) -> dict
```

Collection agents are invoked through:

```python
from core.agent_registry import run_registered_agent
```

Supported modes:

- `configured_scan`
- `manual_url`
- `manual_text`

---

## 4. Implemented Collection Agents

### 1. `literature_agent.py`
- fetches papers from arXiv
- collects intermediate literature signals for later synthesis

### 2. `funding_agent.py`
- tracks grant and funding opportunities
- collects funder-defined problem signals

### 3. `lab_agent.py`
- tracks research labs and projects
- collects themes and active problems

### 4. `company_agent.py`
- tracks companies and use cases
- collects market-facing problems and deployment constraints

### 5. `regulation_agent.py`
- tracks standards, regulation, and compliance signals
- collects technical constraints created by policy or standards

### 6. `opensource_agent.py`
- tracks repositories, docs, and engineering issues
- collects unresolved protocol and implementation problems

### 7. `practitioner_agent.py`
- tracks practitioner-facing signals
- collects deployment pain points and operational friction

### 8. `investment_agent.py`
- tracks VC and investment signals
- collects infrastructure needs implied by capital flow

### 9. `failure_agent.py`
- tracks incidents and postmortems
- collects root causes, technical weaknesses, and prevention ideas

### 10. `data_availability_agent.py`
- tracks datasets, benchmarks, registries, and test suites
- collects what can be evaluated and where benchmark gaps remain

### 11. `hackathon_agent.py`
- tracks hackathon tracks, bounties, challenge statements, and builder programs
- collects prototype-ready problem statements and expected outputs

### 12. `expert_agent.py`
- tracks researchers, faculty, labs, and domain experts
- collects expertise, affiliations, recent work, and collaboration relevance

---

## 5. Implemented Processing Agents

### 13. `tagging_agent.py`
- normalize areas and tags

### 14. `clustering_agent.py`
- group similar problems

### 15. `trend_agent.py`
- detect growth and recurrence of themes

---

## 6. Intelligence Agents

### 16. `synthesis_agent.py`
- generate first-pass synthesis artifacts from processed evidence
- combine signals across layers
- preserve structure, provenance, and ranking context
- best used as a baseline intelligence compiler before final chat-assisted refinement

### 17. `idea_agent.py`
- generate first-pass research and prototype ideas from synthesis artifacts
- best used as a baseline idea generator before final chat-assisted refinement

### 18. `proposal_agent.py`
- align ideas with funding opportunities
- generate baseline proposal scaffolds before final chat-assisted refinement

---

## 7. Research Areas

- RWA
- ESG
- ZK-IoV
- DID
- DePIN
- MEV
- Stablecoins

---

## 8. Output and Storage

### Markdown outputs

```text
outputs/
  intermediate/
  synthesis/
```

### Source configs

```text
sources/
```

### Shared execution layer

```text
core/
  agent_interface.py
  agent_registry.py
  schemas.py
```

---

## 9. Current Known Limitation

Configured scans are improving, but public source structure still makes some auto-scans less reliable than manual modes. Collection-first mode is now the default execution path for collection agents, and baseline intelligence outputs should still be treated as first-pass artifacts rather than final research judgments.

---

## 10. LLM Rules

- Do not call OpenAI directly from agents
- Use `llm_provider.py`
- Current local development uses Ollama
- Future providers can be swapped behind the shared LLM layer
