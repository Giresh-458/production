"""Add-source persistence (survives a browser reload) and the discard list."""
from __future__ import annotations

import pytest

from core import add_source_state as state
from core.agent_fit import AgentFit, PageFinding
from core.user_sources import UrlInspection


@pytest.fixture(autouse=True)
def _tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RIF_ADD_SOURCE_STATE_DIR", str(tmp_path))


def test_progress_round_trips_through_disk():
    inspection = UrlInspection(url="https://colosseum.com/", final_url="https://colosseum.com/", status=200, title="Colosseum",
                               domain_evidence=[("DePIN", 3.0)],
                               agent_fits=[AgentFit("investment", 18.0, ["\"venture capital\""])])
    scan = {"url": "https://colosseum.com/", "given": PageFinding("https://colosseum.com/", "Colosseum", None, 0.0, [], []),
            "found": [PageFinding("https://colosseum.com/hackathon", "Hackathon", "hackathon", 24.0, ["hackathon"], [])],
            "notes": ["1 linked page(s) fit no agent and were discarded."]}
    state.save_state({"as_inspection": inspection, "as_scan": scan, "as_sel_abc": True, "as_form_domains": ["DePIN"]})
    loaded = state.load_state()
    assert loaded["as_inspection"] == inspection
    assert loaded["as_inspection"].domain_evidence == [("DePIN", 3.0)]
    assert loaded["as_scan"]["found"][0].agent == "hackathon" and loaded["as_scan"]["given"].agent is None
    assert loaded["as_sel_abc"] is True and loaded["as_form_domains"] == ["DePIN"]
    state.clear_state()
    assert state.load_state() == {}


def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / "add_source_state.json").write_text("{not json", encoding="utf-8")
    assert state.load_state() == {}


def test_discarded_urls_are_remembered_and_restorable():
    state.discard(["https://colosseum.com/companies/x", "https://other.org/a"], "discarded by you")
    assert state.is_discarded("https://www.colosseum.com/companies/x/")
    assert not state.is_discarded("https://colosseum.com/hackathon")
    assert [d["url"] for d in state.discarded_for_site("https://colosseum.com/")] == ["https://colosseum.com/companies/x"]
    assert state.restore_site("https://colosseum.com/") == 1
    assert not state.is_discarded("https://colosseum.com/companies/x")
    assert state.is_discarded("https://other.org/a")  # other sites untouched


def test_volatile_query_parameters_do_not_defeat_the_discard_list():
    state.discard(["https://colosseum.com/arena/explore?hackathonId=7&seed=b16d6c"], "fits no agent")
    assert state.is_discarded("https://colosseum.com/arena/explore?seed=ffff00&hackathonId=7")
    assert not state.is_discarded("https://colosseum.com/arena/explore?hackathonId=8&seed=ffff00")


def test_scan_site_does_not_fetch_discarded_links():
    from types import SimpleNamespace

    from core.agent_fit import scan_site

    pages = {
        "https://solana.org/": SimpleNamespace(final_url="https://solana.org/", title="Solana", error=None, clean_text="Welcome.",
                                               html='<a href="/grants-funding">Grants and funding</a><a href="/hackathons">Hackathons</a>'),
        "https://solana.org/hackathons": SimpleNamespace(final_url="https://solana.org/hackathons", title="Hackathons", error=None,
                                                         html="", clean_text="Global hackathons. Prize pool. Submission deadline."),
    }
    fetched = []
    state.discard(["https://solana.org/grants-funding"], "discarded by you")
    _given, found, notes = scan_site("https://solana.org/", fetch=lambda url: fetched.append(url) or pages[url], skip=state.is_discarded)
    assert "https://solana.org/grants-funding" not in fetched
    assert [f.url for f in found] == ["https://solana.org/hackathons"]
    assert any("previously discarded" in n for n in notes)
