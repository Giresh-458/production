"""Regression tests for per-query budgeting of EU Funding & Tenders discovery."""

def test_eu_source_budget_gives_each_search_term_more_than_one_page():
    """The configured production budget must not silently cap each of ten searches at 3 pages."""
    from agents.funding_collector import _eu_query_budget

    budgets = _eu_query_budget(
        search_texts=[
            "tokenized real-world assets", "carbon credit", "MRV emissions",
            "sustainability data", "zero-knowledge", "internet of vehicles",
            "decentralized identity", "physical infrastructure",
            "maximal extractable value", "stablecoin payments",
        ],
        page_size=50,
        max_pages=200,
        max_documents=10000,
    )

    assert budgets["pages_per_query"] == 20
    assert budgets["documents_per_query"] == 1000
    assert budgets["unallocated_pages"] == 0


def test_eu_query_budget_records_remainder_without_dropping_queries():
    from agents.funding_collector import _eu_query_budget

    budgets = _eu_query_budget(
        search_texts=["a", "b", "c"],
        page_size=50,
        max_pages=10,
        max_documents=1000,
    )

    assert budgets["pages_per_query"] == 3
    assert budgets["unallocated_pages"] == 1
    assert budgets["query_count"] == 3

def test_eu_source_configured_budget_is_bounded_but_not_the_old_three_page_cap():
    from pathlib import Path
    import yaml
    from agents.funding_collector import _eu_query_budget

    config = yaml.safe_load(Path("sources/funding_sources.yaml").read_text(encoding="utf-8"))
    source = next(
        entry
        for group in config["sources"].values()
        for entry in group
        if entry["name"] == "EU Open and Forthcoming Funding Calls"
    )
    api = source["collection"]["api"]
    limits = source["collection"]["limits"]
    budgets = _eu_query_budget(
        search_texts=api["search_texts"],
        page_size=api["page_size"],
        max_pages=limits["max_pages"],
        max_documents=limits["max_documents"],
    )

    assert budgets["pages_per_query"] >= 20
    assert budgets["documents_per_query"] >= 1000
    assert budgets["query_count"] == len(api["search_texts"])
