"""Adaptive research may only use a work's own abstract as evidence text."""
from agents import adaptive_research_agent


def _fake_fetch_json(results):
    def fetch_json(url, **kwargs):
        return {"results": results}, url
    return fetch_json


def test_work_without_abstract_is_not_turned_into_evidence(monkeypatch):
    """Live regression: OpenAlex concept metadata repr became a problem statement."""
    import core.http_client as http_client

    monkeypatch.setattr(http_client, "fetch_json", _fake_fetch_json([
        {
            "display_name": "A framework for considering justice aspects in integrated wildfire risk management",
            "doi": "https://doi.org/10.1038/s41558-023-01726-0",
            "abstract_inverted_index": None,
            "concepts": [{"id": "https://openalex.org/C2777042776", "display_name": "Preparedness", "score": 0.72}],
        },
        {
            "display_name": "Carbon pricing and ESG governance",
            "doi": "https://doi.org/10.54097/0q428r94",
            "abstract_inverted_index": {"Accountable": [0], "carbon": [1], "performance": [2], "reduces": [3], "selective": [4], "disclosure.": [5]},
        },
    ]))
    docs = adaptive_research_agent._openalex("wildfire restoration justice", limit=6)
    assert [d.source for d in docs] == ["https://doi.org/10.54097/0q428r94"]
    assert "openalex.org/C" not in docs[0].content
    assert docs[0].content == "Carbon pricing and ESG governance. Accountable carbon performance reduces selective disclosure."
