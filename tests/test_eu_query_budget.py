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
        max_pages=100,
        max_documents=5000,
    )

    assert budgets["pages_per_query"] == 10
    assert budgets["documents_per_query"] == 500
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
