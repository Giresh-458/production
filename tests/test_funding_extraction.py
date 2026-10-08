from agents.funding_agent import _extract_funding_amount

def test_extract_funding_amount_valid():
    text = "We will award a grant of $7M for the project. Apply now."
    assert "$7M" in _extract_funding_amount(text)

    text2 = "The maximum award is up to EUR2,500,000 per project for startups."
    assert "2,500,000" in _extract_funding_amount(text2)

def test_extract_funding_amount_invalid():
    text = "Our platform has 7B users.\nAnnual revenue was $1M.\nThe company is not offering any grants."
    assert _extract_funding_amount(text) is None

    text2 = "We raised $2158B in a Series X funding round."
    assert _extract_funding_amount(text2) is None
