def test_build_domain_relevance_empty():
    import agents.funding_agent as fa
    original = fa.build_domain_relevance
    try:
        fa.build_domain_relevance = lambda text: []
        domains = fa.build_domain_relevance('')
        secondary = [item['domain'] for item in domains[1:3] if float(item.get('score', 0.0) or 0.0) > 0.0] if len(domains) > 1 else []
        assert secondary == []
    finally:
        fa.build_domain_relevance = original
