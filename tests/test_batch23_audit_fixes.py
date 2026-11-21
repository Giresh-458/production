"""Regression tests for the Batch-2/Batch-3 audit (2026-11-20, live examples)."""
from __future__ import annotations

import pytest


# ---------------- Batch-2: relevance gate ----------------

def _ctx():
    from tests.test_mission_gate_tiers import _ctx as base
    return base()


GREENWASHING = {
    "title": "Greenwashing, carbon emission, and ESG",
    "problem_statement": "No explicit problem statement found.",
    "context_summary": "ESG ratings and carbon footprints of firms.",
    "evidence_snippets": ["Our findings indicate that high ESG-rated firms do not have lower carbon emissions."],
}


def test_domain_word_alone_needs_semantic_relation_to_the_call(monkeypatch):
    import core.semantic_retrieval as sr
    from core.mission_gate import relevance

    monkeypatch.setenv("RIF_SEMANTIC_GATE", "true")
    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: [0.313])  # live cosine
    result = relevance(GREENWASHING, _ctx())
    assert result["passed"] is False and "semantically unrelated" in result["reason"]

    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: [0.531])
    assert relevance(GREENWASHING, _ctx())["relevance_tier"] == "domain_anchor"

    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: None)  # model unavailable
    assert relevance(GREENWASHING, _ctx())["passed"] is True


# ---------------- Batch-2: problem statements ----------------

@pytest.mark.parametrize("candidates,title", [
    (["How risk management can prevent future wildfire disasters in the wildland-urban interface."],
     "How risk management can prevent future wildfire disasters in the wildland-urban interface"),
    (["Current regulatory developments, such as the European Corporate Sustainability Reporting Directive (CSRD), "
      "are pushing to make ESG indicators (within this triple perspective: social, environmental and governance "
      "risks) a key set of information to be used for reporters and users of information."],
     "Climate change, ESG criteria and recent regulation"),
])
def test_title_and_context_sentences_are_not_problems(candidates, title):
    from core.normalization import _select_problem_statement

    assert _select_problem_statement(candidates, title=title) == "No explicit problem statement found."


def test_genuine_problem_still_selected():
    from core.normalization import _select_problem_statement

    s = ("Despite these critical benefits, restoration efforts often face significant challenges, including slow "
         "progress, limited scalability, high costs, and uncertain outcomes.")
    assert _select_problem_statement([s], title="Transformative living labs") == s


def test_url_is_never_the_actor():
    from core.normalization import _normalized_actor_and_focus

    actor, _ = _normalized_actor_and_focus({}, {"source": "https://doi.org/10.1002/bse.3929"}, {}, "ESG")
    assert not actor.startswith("http")


def test_openalex_documents_carry_authors_venue_and_type(monkeypatch):
    import core.http_client as http
    from agents.adaptive_research_agent import _openalex

    work = {
        "display_name": "Greenwashing, carbon emission, and ESG", "doi": "https://doi.org/10.1002/bse.3929",
        "abstract_inverted_index": {"Firms": [0], "report": [1]},
        "authorships": [{"author": {"display_name": "Jane Doe"}}, {"author": {"display_name": "John Roe"}}],
        "primary_location": {"source": {"display_name": "Business Strategy and the Environment"}},
    }
    monkeypatch.setattr(http, "fetch_json", lambda *a, **k: ({"results": [work]}, None))
    doc = _openalex("greenwashing")[0]
    assert doc.authors == ["Jane Doe", "John Roe"]
    assert doc.venue == "Business Strategy and the Environment"
    assert doc.evidence_type == "Scholarly publication record" and doc.source_type == "Research publication"


# ---------------- Batch-3: shared topic vocabulary ----------------

@pytest.mark.parametrize("text,expected", [
    ("This study explores the real effects of environmental, social, and governance (ESG) reporting.", "governance"),
    ("The proposal extends the scope to all large companies.", "governance"),
    ("A lack of robust analytics is a bottleneck for developing predictive analytics.", "scalability"),
    ("This is a candidate gap, validated against the evidence already preserved.", "identity"),
    ("This is a candidate gap, validated against the evidence already preserved.", "stablecoin"),
    ("Finally, we incorporated wildfire vulnerability into the model.", "security"),
])
def test_wrong_word_sense_does_not_create_topics(text, expected):
    from core.topic_keywords import match_topics

    assert expected not in match_topics(text)


@pytest.mark.parametrize("text,expected", [
    ("Engaging multiple levels of governance in living labs.", "governance"),
    ("A smart contract audit found a security vulnerability.", "security"),
    ("Verification bottleneck: throughput collapses under load.", "scalability"),
])
def test_genuine_topics_still_match(text, expected):
    from core.topic_keywords import match_topics

    assert expected in match_topics(text)


def test_all_agents_share_one_topic_vocabulary():
    import agents.idea_agent as idea
    import agents.synthesis_agent as synthesis
    import agents.tagging_agent as tagging
    import agents.trend_agent as trend
    from core.topic_keywords import TOPIC_KEYWORDS

    assert tagging.TOPIC_KEYWORDS is TOPIC_KEYWORDS
    assert trend.TOPIC_KEYWORDS is TOPIC_KEYWORDS
    assert synthesis.TOPIC_LABELS is TOPIC_KEYWORDS
    assert idea.TOPIC_HINTS is TOPIC_KEYWORDS


# ---------------- synthesis / ideas ----------------

def test_recurrence_claim_counts_only_members_with_a_problem():
    from agents.synthesis_agent import build_evidence_assessment

    members = [
        {"problem_statement": "No explicit problem statement found.", "source_url": f"https://s{i}.org", "layer": "Literature",
         "agent_name": "literature", "actor": f"A{i}"}
        for i in range(3)
    ]
    assessment = build_evidence_assessment({"members": members}, [])
    claim = assessment["claims"][0]
    assert claim["support"] == 0 and claim["confidence"] == "None"


def test_unsupported_gap_disables_experiment_prototype_and_gap_flag():
    from agents.idea_agent import infer_experiment_direction, infer_prototype_scope, validate_idea_payload

    sections = {"Gap": "No defensible unresolved research gap can be established from the current evidence alone."}
    assert infer_experiment_direction("ESG", sections, "x").startswith("Not designed")
    assert infer_prototype_scope("ESG", sections, "x").startswith("Not scoped")
    v = validate_idea_payload("A real problem statement here.", sections["Gap"], "If x then y", "compare baseline",
                              "Unassessed", {"source_urls": ["u"]})
    assert v["checks"]["gap_present"] is False and v["ready_for_external_review"] is False


def test_proposal_gate_rejects_ideas_without_a_problem():
    from agents.proposal_agent import _proposal_gate

    record = {"problem": "No explicit problem statement found.",
              "validation": {"novelty_status": "external_search_completed", "novelty_search": {}}}
    ok, info = _proposal_gate(record, _ctx())
    assert ok is False and "no source-grounded problem" in info["reason"]


def test_unresolved_gap_is_not_a_gap():
    from agents.idea_agent import _gap_not_supported

    assert _gap_not_supported("Unresolved") is True
    assert _gap_not_supported("Provisional research gap: the collected evidence contains 2 signals.") is False


def test_openalex_literature_prefers_full_call_queries(monkeypatch):
    import core.http_client as http
    import agents.literature_agent as lit

    sent = []
    monkeypatch.setattr(lit, "_search_terms", lambda area: [
        "living", "labs", "co-creating", "solutions",
        "Living labs co-creating solutions for the restoration of ecosystems",
    ])
    monkeypatch.setattr(lit, "enforce_api_spacing", lambda: None)
    monkeypatch.setattr(http, "fetch_json", lambda url, params=None, **k: (sent.append(params["search"]) or ({"results": []}, None)))
    lit.fetch_openalex_entries("Unscoped")
    assert sent[0] == "Living labs co-creating solutions for the restoration of ecosystems"


def test_method_statement_is_not_a_problem():
    """Live (doi 10.3897/aca.8.e149488): a method sentence was selected as the problem."""
    from core.normalization import _select_problem_statement

    method = ("Additionally, we evaluated the role of restoration in mitigating drought risk, a major climate change "
              "component, mapping the results from the project.")
    real = ("These challenges are acute in watersheds like the Citarum River in West Java, Indonesia, which suffers "
            "from severe pollution due to inadequate waste management.")
    assert _select_problem_statement([method], title="Mediterranean restoration") == "No explicit problem statement found."
    assert _select_problem_statement([real], title="Citarum Living Lab") == real


def test_batch3_resolves_calls_inside_the_given_run_dir():
    from pathlib import Path

    source = Path("scripts/pipeline_tools/run_batch3.py").read_text(encoding="utf-8")
    assert 'run_dir / "calls" / str(call_id)' in source


# ---------------- external API limits (live: OpenAlex/S2 429, GitHub 403) ----------------

def test_openalex_budget_exhaustion_stops_search_but_not_free_lookups(monkeypatch):
    import core.http_client as http

    class Resp:
        status_code = 429
        headers = {"X-RateLimit-Remaining": "0", "Retry-After": "14773"}

        def close(self):
            pass

    assert http._openalex_note_rate_limit("https://api.openalex.org/works?search=x", Resp()) is True
    with pytest.raises(RuntimeError, match="budget exhausted"):
        http._openalex_budget_check("https://api.openalex.org/works", {"search": "x"})
    http._openalex_budget_check("https://api.openalex.org/works/doi:10.1002/bse.3929", None)  # free singleton


def test_openalex_api_key_is_sent_when_configured(monkeypatch):
    import core.http_client as http

    monkeypatch.setenv("OPENALEX_API_KEY", "k123")
    assert http._openalex_params("https://api.openalex.org/works", {"search": "x"})["api_key"] == "k123"
    assert http._openalex_params("https://example.org/x", {"q": 1}) == {"q": 1}


def test_openalex_search_responses_are_cached(monkeypatch, tmp_path):
    import core.http_client as http

    monkeypatch.setenv("RIF_OPENALEX_CACHE_DIR", str(tmp_path))
    calls = []

    class Resp:
        status_code = 200
        url = "https://api.openalex.org/works"

        def json(self):
            return {"results": [{"id": "W1"}]}

        def close(self):
            pass

    monkeypatch.setattr(http, "get_response", lambda *a, **k: (calls.append(1), Resp())[1])
    first, _ = http.fetch_json("https://api.openalex.org/works", params={"search": "living labs"})
    second, _ = http.fetch_json("https://api.openalex.org/works", params={"search": "living labs"})
    assert first == second == {"results": [{"id": "W1"}]} and len(calls) == 1


def test_batch2_reports_rate_limited_collectors():
    from core.batch2 import _external_api_limit_messages

    tasks = [
        {"agent_name": "literature", "warnings": ["OpenAlex query 'x' collection failed: 429 Client Error: Too Many Requests"]},
        {"agent_name": "lab", "warnings": ["Generated 3 records"]},
    ]
    assert list(_external_api_limit_messages(tasks)) == ["literature"]


def test_semantic_scholar_uses_one_focused_query_and_documented_key_header(monkeypatch):
    import agents.literature_agent as lit

    seen = {}
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2key")
    monkeypatch.setattr(lit, "enforce_api_spacing", lambda: None)
    monkeypatch.setattr(lit, "_search_terms", lambda area: ["living", "labs", "Living labs for ecosystem restoration"])
    monkeypatch.setattr(lit, "_fetch_provider_json", lambda url, name, extra=None: (seen.update(url=url, headers=extra) or {}))
    lit.fetch_semanticscholar_entries("Unscoped")
    assert "query=Living%20labs%20for%20ecosystem%20restoration&" in seen["url"]
    assert seen["headers"] == {"x-api-key": "s2key"}


def test_single_source_synthesis_cannot_claim_high_confidence():
    """Live: one source + local LLM 'supported' review produced 'Confidence: High'."""
    from agents.synthesis_agent import cap_confidence_by_evidence

    assert cap_confidence_by_evidence("High", 1) == "Low"
    assert cap_confidence_by_evidence("High", 2) == "Medium"
    assert cap_confidence_by_evidence("High", 3) == "High"
    assert cap_confidence_by_evidence("Low", 5) == "Low"


def test_llm_requests_are_reproducible_by_default(monkeypatch):
    """Live: identical evidence got different LLM gap decisions between runs."""
    from core.llm_provider import build_generate_payload

    monkeypatch.delenv("RIF_LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("RIF_LLM_SEED", raising=False)
    payload = build_generate_payload(" Is this a problem? ", "qwen3.5:4b")
    assert payload["options"] == {"temperature": 0.0, "seed": 42} and payload["prompt"] == "Is this a problem?"


@pytest.mark.parametrize("module", ["company", "expert", "failure", "investment", "lab", "opensource",
                                    "practitioner", "regulation", "data_availability"])
def test_collectors_analyze_mode_helpers_are_defined(module):
    """Live: analyze mode raised NameError: name 'coalesce_text' is not defined."""
    import importlib

    mod = importlib.import_module(f"agents.{module}_agent")
    assert callable(getattr(mod, "coalesce_text"))
    assert mod.coalesce_text(None) == "UNKNOWN" and mod.coalesce_text("  x ") == "x"


def test_failure_incident_type_inference_stays_in_taxonomy():
    from agents.failure_agent import FAILURE_TYPES, infer_incident_type

    assert infer_incident_type("The cross-chain bridge was hacked and drained of funds.") == "Bridge Hack"
    assert infer_incident_type("The stablecoin lost its peg during the depeg event.") == "Depeg"
    assert infer_incident_type("Maintenance window caused downtime.") == "Operational Failure"
    assert all(infer_incident_type(t) in FAILURE_TYPES for t in ("x", "oracle manipulation attack"))


def test_renamed_helpers_resolve():
    import agents.expert_agent as expert
    import agents.lab_agent as lab

    assert expert.build_problem_signature("RWA", "Ada", "tokenization").startswith("RWA | ada")
    assert lab.infer_theme_area("tokenized real-world asset settlement") in lab.FOCUS_AREAS


def test_no_accidental_tuple_assignments_in_collector_analysis():
    """Live: `x = a, b` (meant `a or b`) crashed analyze mode in 4 collectors."""
    import ast
    import glob

    found = []
    for path in glob.glob("agents/*_agent.py"):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "analyze_document":
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)
                            and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                        found.append(f"{path}:{node.lineno}")
    assert found == []
