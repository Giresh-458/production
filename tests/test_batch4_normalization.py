import pytest
import sqlite3
import json
from pathlib import Path
from core.normalization import (
    save_normalized_collection_records,
    canonicalize_url,
    extract_source_identity,
    generate_content_hash
)

@pytest.fixture
def temp_outputs(tmp_path: Path):
    out = tmp_path / "outputs"
    (out / "intermediate").mkdir(parents=True)
    return out

def create_artifact(outputs_root: Path, filename: str, content: str):
    path = outputs_root / "intermediate" / filename
    path.write_text(content, encoding="utf-8")
    return path

def get_db(outputs_root: Path):
    conn = sqlite3.connect(outputs_root / "normalized" / "evidence_ledger.db")
    conn.row_factory = sqlite3.Row
    return conn

# Helper to make a standard artifact markdown
def make_artifact_md(
    title: str = "Test Title",
    source: str = "https://example.com/paper",
    doi: str = "",
    funding_call_id: str = "CALL-001",
    run_id: str = "RUN-001",
    problem: str = "Problem description here.",
    evidence_snippets: list[str] = None
):
    snippets = evidence_snippets or ["This is an evidence snippet about testing."]
    snippets_str = "\n".join(f"- {s}" for s in snippets)
    
    metadata_lines = []
    if doi:
        metadata_lines.append(f"- doi: {doi}")
    
    metadata_section = ""
    if metadata_lines:
        metadata_section = "## Machine Metadata\n" + "\n".join(metadata_lines)
    
    return f"""# {title}

## Shared Metadata
- Title: {title}
- Source: {source}
- Funding Call Id: {funding_call_id}
- Run Id: {run_id}
- Layer: Literature

{metadata_section}

## Problem
{problem}

## Evidence Bundle
### Evidence Snippets
{snippets_str}
"""

def test_case_1_same_doi(temp_outputs):
    # Case 1: Same DOI, diff URLs
    create_artifact(temp_outputs, "a.md", make_artifact_md(doi="10.123/456", source="http://siteA.com/1"))
    create_artifact(temp_outputs, "b.md", make_artifact_md(doi="10.123/456", source="http://siteB.com/2"))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT source_id, evidence_id FROM canonical_evidence").fetchall()
    assert len(recs) == 1
    assert recs[0]["source_id"] == "doi:10.123/456"

def test_case_2_and_3_canonical_url(temp_outputs):
    # Case 2/3: Canonical URL stripping
    create_artifact(temp_outputs, "a.md", make_artifact_md(source="https://site.com/paper?utm_source=twitter"))
    create_artifact(temp_outputs, "b.md", make_artifact_md(source="https://site.com/paper/")) 
    create_artifact(temp_outputs, "c.md", make_artifact_md(source="https://site.com/paper"))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT source_id, canonical_url FROM canonical_evidence").fetchall()
    
    # a.md utm_source is stripped
    assert recs[0]["canonical_url"] == "https://site.com/paper"
    assert recs[0]["source_id"] == "url:https://site.com/paper"

def test_case_4_and_5_title_identity(temp_outputs):
    # Case 4: Same paper with title variation (hash handles this implicitly if fallback)
    # Case 5 says same title but different DOIs -> different.
    create_artifact(temp_outputs, "a.md", make_artifact_md(title="Cool Paper", doi="10.1/a"))
    create_artifact(temp_outputs, "b.md", make_artifact_md(title="Cool Paper", doi="10.1/b"))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT source_id FROM canonical_evidence").fetchall()
    assert recs[0]["source_id"] != recs[1]["source_id"]

def test_case_6_and_7_copied_news(temp_outputs):
    # Copied news has the same content hash -> same independence group
    content = "Breaking news: huge discovery in science."
    create_artifact(temp_outputs, "a.md", make_artifact_md(title="News A", source="http://sitea", problem=content))
    create_artifact(temp_outputs, "b.md", make_artifact_md(title="News A", source="http://siteb", problem=content))
    
    create_artifact(temp_outputs, "c.md", make_artifact_md(title="News C", source="http://sitec", problem="Completely different discovery!"))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT independence_group, independence_status, source_id FROM canonical_evidence").fetchall()
    
    # A and B share same hash since title and content match, so they group
    group_a = recs[0]["independence_group"]
    group_b = recs[1]["independence_group"]
    group_c = recs[2]["independence_group"]
    
    assert group_a == group_b
    assert group_c != group_a
    assert "DERIVED" in [r["independence_status"] for r in recs[:2]]
    assert "INDEPENDENT" in [r["independence_status"] for r in recs[:2]]

def test_case_10_and_11_missing_provenance(temp_outputs):
    # Missing funding call id
    create_artifact(temp_outputs, "a.md", make_artifact_md(funding_call_id=""))
    # Missing source url
    create_artifact(temp_outputs, "b.md", make_artifact_md(source=""))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    # Release the published ledger before re-normalizing: Windows cannot
    # atomically replace a file that still has an open SQLite handle.
    conn.close()
    create_artifact(temp_outputs, "c.md", make_artifact_md(title="Manual Source Placeholder", source="Manual"))
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT normalization_status FROM canonical_evidence").fetchall()
    assert len(recs) == 3
    assert all(r["normalization_status"] == "NEEDS_REVIEW" for r in recs)

def test_case_12_same_source_multi_call(temp_outputs):
    # Same source for CALL-A and CALL-B -> Same source id, different evidence relationships
    create_artifact(temp_outputs, "a.md", make_artifact_md(source="http://one", funding_call_id="CALL-A"))
    create_artifact(temp_outputs, "b.md", make_artifact_md(source="http://one", funding_call_id="CALL-B"))
    
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    with open(paths['records']) as f:
        print("RECORDS JSON:", json.load(f))
    with open(paths['errors']) as f:
        print("ERRORS JSON:", json.load(f))
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT source_id, funding_call_id, evidence_id, record_id FROM canonical_evidence").fetchall()
    for r in recs:
        print("REC:", dict(r))
    assert len(recs) == 2
    assert recs[0]["source_id"] == recs[1]["source_id"]
    assert recs[0]["funding_call_id"] != recs[1]["funding_call_id"]

def test_case_13_multi_claim(temp_outputs):
    # One source, multiple claims
    create_artifact(temp_outputs, "a.md", make_artifact_md(evidence_snippets=["Claim 1 text goes here and is long enough", "Claim 2 text goes here and is also long"]))
    
    errors_dict = save_normalized_collection_records(temp_outputs, temp_outputs)
    print("ERRORS:", temp_outputs.joinpath("normalized", "errors.json").read_text())
    print("RECORDS:", temp_outputs.joinpath("normalized", "records.json").read_text())
    conn = get_db(temp_outputs)
    claims = conn.execute("SELECT * FROM claims").fetchall()
    assert len(claims) == 2

def test_case_8_and_9_contradictions(temp_outputs):
    # Same source, different evidence snippets that contradict
    create_artifact(temp_outputs, "a.md", make_artifact_md(source="http://site", evidence_snippets=["X is good"]))
    create_artifact(temp_outputs, "b.md", make_artifact_md(source="http://site", evidence_snippets=["X is bad"]))
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT evidence_id, evidence FROM canonical_evidence").fetchall()
    
    # Both evidences must be preserved because they contain distinct evidence texts, generating distinct evidence_ids
    assert len(recs) == 2

def test_case_16_independent_contradictions(temp_outputs):
    create_artifact(temp_outputs, "a.md", make_artifact_md(source="http://site1", title="A", evidence_snippets=["X is good"]))
    create_artifact(temp_outputs, "b.md", make_artifact_md(source="http://site2", title="B", evidence_snippets=["X is bad"]))
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT independence_group, evidence FROM canonical_evidence").fetchall()
    assert len(recs) == 2
    assert recs[0]["independence_group"] != recs[1]["independence_group"]

def test_case_15_same_source_two_agents(temp_outputs):
    # Literature agent and Expert agent collect the same source.
    # We will simulate this using same source_url but different layer.
    md1 = make_artifact_md(source="http://site", title="Paper X")
    md2 = md1.replace("- Layer: Literature", "- Layer: Expert")
    create_artifact(temp_outputs, "a.md", md1)
    create_artifact(temp_outputs, "b.md", md2)
    
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn = get_db(temp_outputs)
    recs = conn.execute("SELECT layer, source_id FROM canonical_evidence").fetchall()
    
    # Repeated evidence from the same source is still traceable to each collection layer.
    assert len(recs) == 2
    assert {r["layer"] for r in recs} == {"Literature", "Expert"}
    assert recs[0]["source_id"] == recs[1]["source_id"]

def test_missing_url_fallback_identity_is_scoped_by_layer(temp_outputs):
    md1 = make_artifact_md(
        title="Generic Program Overview",
        source="",
        problem="The same generic page title is used by different collectors.",
        evidence_snippets=["The same generic evidence snippet should not erase provenance."],
    )
    md2 = md1.replace("- Layer: Literature", "- Layer: Expert")
    create_artifact(temp_outputs, "a.md", md1)
    create_artifact(temp_outputs, "b.md", md2)

    save_normalized_collection_records(temp_outputs, temp_outputs)
    with open(temp_outputs / "normalized" / "records.json", encoding="utf-8") as handle:
        records = json.load(handle)

    assert len(records) == 2
    assert records[0]["source_id"] != records[1]["source_id"]
    assert {record["layer"] for record in records} == {"Literature", "Expert"}
    assert all(record["source_url"] == "" for record in records)
    assert all(record["normalization_status"] == "NEEDS_REVIEW" for record in records)


def test_manual_placeholder_is_not_a_source_url(temp_outputs):
    create_artifact(
        temp_outputs,
        "manual.md",
        make_artifact_md(title="Missing Source Evidence", source="Manual"),
    )
    save_normalized_collection_records(temp_outputs, temp_outputs)
    with open(temp_outputs / "normalized" / "records.json", encoding="utf-8") as handle:
        records = json.load(handle)

    assert len(records) == 1
    assert records[0]["source_url"] == ""
    assert records[0]["canonical_url"] == ""
    assert records[0]["normalization_status"] == "NEEDS_REVIEW"


def test_idempotency(temp_outputs):
    create_artifact(temp_outputs, "a.md", make_artifact_md())
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn1 = get_db(temp_outputs)
    count1 = len(conn1.execute("SELECT * FROM canonical_evidence").fetchall())
    conn1.close()
    
    # Run again
    save_normalized_collection_records(temp_outputs, temp_outputs)
    conn2 = get_db(temp_outputs)
    count2 = len(conn2.execute("SELECT * FROM canonical_evidence").fetchall())
    conn2.close()
    
    assert count1 == count2 == 1


def test_raw_page_prefix_is_never_promoted_to_problem_statement(temp_outputs):
    """Live regression: a lab artifact with no extracted problem had the first 280
    characters of its raw page ("Page title: ... Source URL: ... Network About Join
    Blog") normalized as its problem_statement and handed to Batch-3."""
    md = make_artifact_md(title="Protocol Labs", source="https://protocol.ai/work", problem="", evidence_snippets=None)
    md = md.replace("- This is an evidence snippet about testing.\n", "")
    md += (
        "\n## Raw Content\nPage title: Our Vision: PL Update 2026\nSource URL: https://protocol.ai/blog\n"
        "Network About Join Blog The latest from Protocol Labs Available as RSS\n"
    )
    create_artifact(temp_outputs, "pl.md", md)
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["problem_statement"] == "No explicit problem statement found."
    # The raw page is still preserved as context, not discarded.
    assert "Protocol Labs" in records[0]["context_summary"]


@pytest.mark.parametrize("snippet", [
    # Verbatim shapes from live Batch-2 lab/regulation artifacts.
    "Page title: Our Vision: PL Update 2026 Source URL: https://protocol.ai/blog Network About Join Blog",
    "Corporate sustainability reporting [Depth 0] Corporate sustainability reporting | "
    "https://finance.ec.europa.eu/capital-markets-union-and-financial-markets/company-reporting",
])
def test_serialized_page_block_snippet_is_not_the_problem_statement(temp_outputs, snippet):
    create_artifact(temp_outputs, "s.md", make_artifact_md(problem="", evidence_snippets=[snippet]))
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    assert records[0]["problem_statement"] == "No explicit problem statement found."
    assert snippet in records[0]["evidence_snippets"]  # evidence itself is preserved


def test_genuine_snippet_remains_the_problem_statement(temp_outputs):
    real = "The CAD Trust connection will help transparently track these transfers, guarding against double-counting risk."
    create_artifact(temp_outputs, "g.md", make_artifact_md(problem="", evidence_snippets=[real]))
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    assert records[0]["problem_statement"] == real


def test_snippet_is_cut_where_another_crawled_page_begins(temp_outputs):
    """Live regression: a CSRD regulation snippet ran on into a second crawled page."""
    first = (
        "The proposal extends the scope to all large companies and requires the audit (assurance) "
        "of reported information."
    )
    snippet = (
        first + " [Depth 1] The Commission adopts the European Sustainability Reporting Standards | "
        "https://finance.ec.europa.eu/news/commission-adopts-esrs The Commission adopted today the ESRS."
    )
    create_artifact(temp_outputs, "c.md", make_artifact_md(problem="", evidence_snippets=[snippet]))
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    problem = records[0]["problem_statement"]
    # No content from the second crawled page or its URL may reach the problem.
    assert "[Depth" not in problem and "https://" not in problem and "adopted today" not in problem
    assert snippet in records[0]["evidence_snippets"]  # full evidence is preserved


@pytest.mark.parametrize("snippet", [
    # Verbatim non-problems observed live that pass a keyword "risk/fails" test.
    "Current Rates 625 th Meeting of Central Board of the Reserve Bank of India Economic Resilience, Thinner Buffers, Emerging Risks Globally: Where Does India Stand",
    "Prediction Markets Note SEP 14, 2026 Amodei, Musk and Altman called for slowing AI down, and chip stocks fell on risk.",
    "Please file issues directly on GitHub , or send them to the list if that is not possible because of risk.",
    "error FailedToWithdrawEth ( address owner , address target , uint256 value ) ; // Used when the withdrawal of Ether fails.",
])
def test_listing_navigation_instruction_and_code_are_not_problems(temp_outputs, snippet):
    create_artifact(temp_outputs, "n.md", make_artifact_md(problem="", evidence_snippets=[snippet]))
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    assert records[0]["problem_statement"] == "No explicit problem statement found."


def test_one_bounded_problem_sentence_is_selected_not_joined_snippets(temp_outputs):
    snippets = [
        "Delegated Regulation Related links Timeline of the initiative and stakeholder feedback.",
        "The effectiveness of carbon pricing relies on the quality of emission data, and the information "
        "constraint function of the ESG system is degraded by the greenwashing phenomenon. More text follows here.",
    ]
    create_artifact(temp_outputs, "p.md", make_artifact_md(problem="", evidence_snippets=snippets))
    paths = save_normalized_collection_records(temp_outputs, temp_outputs)
    import json
    records = json.loads(Path(paths["records"]).read_text(encoding="utf-8"))
    assert records[0]["problem_statement"] == (
        "The effectiveness of carbon pricing relies on the quality of emission data, and the information "
        "constraint function of the ESG system is degraded by the greenwashing phenomenon."
    )


def test_copied_run_folder_reuses_normalized_cache(tmp_path):
    """Moving/copying a run folder must not force re-normalization (content is unchanged).

    Live regression: absolute paths and mtimes in the cache manifest made a copied
    run rebuild its Batch-2 handoff, which the Batch-3 contract then blocked."""
    import shutil
    import time
    from core.normalization import ensure_normalized_collection_records

    original = tmp_path / "a" / "outputs"
    (original / "intermediate").mkdir(parents=True)
    create_artifact(original, "x.md", make_artifact_md(
        problem="Current verifiers cannot maintain target latency at high transaction volume today."))
    save_normalized_collection_records(original, original)

    copied = tmp_path / "b" / "outputs"
    time.sleep(0.05)
    shutil.copytree(original, copied)  # new absolute paths and new modification times
    before = {p.name: p.read_bytes() for p in (copied / "normalized").iterdir() if p.is_file()}
    ensure_normalized_collection_records(copied)
    after = {p.name: p.read_bytes() for p in (copied / "normalized").iterdir() if p.is_file()}
    assert after == before


def test_content_change_after_copy_still_refreshes(tmp_path):
    import shutil
    from core.normalization import ensure_normalized_collection_records

    original = tmp_path / "a" / "outputs"
    (original / "intermediate").mkdir(parents=True)
    create_artifact(original, "x.md", make_artifact_md(title="Version One",
        problem="Current verifiers cannot maintain target latency at high transaction volume today."))
    save_normalized_collection_records(original, original)
    copied = tmp_path / "b" / "outputs"
    shutil.copytree(original, copied)
    create_artifact(copied, "x.md", make_artifact_md(title="Version Two",
        problem="Current verifiers cannot maintain target latency at high transaction volume today."))
    ensure_normalized_collection_records(copied)
    import json
    titles = [r["title"] for r in json.loads((copied / "normalized" / "records.json").read_text(encoding="utf-8"))]
    assert titles == ["Version Two"]
