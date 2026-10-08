import pytest
import tempfile
import unittest
from pathlib import Path

pytestmark = pytest.mark.live

from core.agent_registry import AGENT_REGISTRY, run_registered_agent
from core.collection_schemas import get_collection_schema
from agents.data_availability_agent import DataDocument, fallback_analysis as data_fallback
from agents.expert_agent import ExpertDocument, fallback_analysis as expert_fallback
from agents.failure_agent import FailureDocument, fallback_analysis as failure_fallback

COLLECTION_AGENTS = [
    "literature", "funding", "lab", "company", "regulation", "opensource",
    "practitioner", "investment", "failure", "data_availability", "hackathon", "expert",
]

# These are deliberately evidence-rich fixtures. The acceptance test must prove that each
# collector can accept a realistic artifact, not merely avoid throwing an exception.
TEXTS = {
    "literature": """
    Zero-knowledge verification for real-world asset settlement is a research problem in
    scalable privacy-preserving distributed systems. Recent experiments compare proving
    latency, verifier throughput, memory consumption and communication overhead at 10K,
    100K and 1M transactions. The paper reports that verification latency grows sharply,
    memory pressure becomes a bottleneck, and current proof systems do not maintain the
    required throughput for large tokenized-asset settlement workloads. The authors identify
    open questions around recursive proving, batching, verifier parallelism and practical
    deployment constraints. Results include benchmark measurements, limitations and a clear
    research gap for scalable zero-knowledge verification of real-world assets.
    """,
    "funding": """
    Open funding call for real-world asset tokenization and secure settlement research.
    The funding body invites universities and research organizations to submit proposals
    on privacy, compliance, scalable verification and digital-asset infrastructure. The
    call opens in August 2026 and closes in December 2026. Eligible applicants include
    universities and research institutes in the stated geography. Funding supports research,
    prototyping, evaluation and eligible personnel costs. Applications must explain the
    technical problem, expected research contribution, measurable deliverables and project
    milestones. The call is directly relevant to RWA research and has a published deadline,
    eligibility requirements and proposal evaluation criteria.
    """,
    "lab": """
    Research laboratory working on real-world asset tokenization, cryptography, privacy,
    distributed systems and secure digital-asset settlement. The lab publishes recent work
    on zero-knowledge verification, privacy-preserving compliance and scalable blockchain
    infrastructure. Its researchers have evaluated proving latency, verifier throughput,
    transaction scalability and secure settlement architectures. Current projects include
    experimental prototypes and benchmark studies for tokenized assets. The lab collaborates
    with industry and public research programs and can contribute cryptographic expertise,
    evaluation methodology, prototype development and research supervision. These activities
    make the laboratory relevant to a funded RWA research project.
    """,
    "company": """
    A company deploying tokenized real-world assets needs secure settlement, privacy,
    compliance and scalable verification. Its production platform handles digital-asset
    issuance and settlement and requires low verification latency at high transaction volume.
    The company can provide anonymized deployment data, operational requirements and access
    to production testing environments. It has engineering teams capable of integrating a
    prototype and can serve as an industry partner for pilot deployment. Its geographic
    operations are compatible with the proposed project, and its commercial need aligns with
    research into scalable privacy-preserving verification for RWA settlement. The company
    therefore has a concrete potential project role rather than being merely a company in
    the same technology sector.
    """,
    "regulation": """
    A binding regulation for real-world asset tokenization requires identity verification,
    privacy protection, auditability, secure custody and compliant digital-asset settlement.
    The instrument applies within a defined jurisdiction, has an effective date and remains
    legally enforceable unless amended or repealed. It supersedes an earlier rule and imposes
    mandatory compliance requirements on covered entities. The document distinguishes binding
    obligations from non-binding guidance and technical standards. Relevant requirements
    create implementation constraints for privacy-preserving verification, transaction
    monitoring and secure settlement of tokenized assets. These requirements are important
    evidence for RWA research because the legal obligations can directly shape system design.
    """,
    "opensource": """
    GitHub issue in a real-world asset settlement project reports that zero-knowledge
    verification becomes impractical above 100K transactions because proving latency and
    verifier throughput degrade sharply. Maintainers discuss a performance limitation,
    architecture constraints and the need for batching, parallel verification and improved
    proof recursion. The issue includes benchmark observations and affects production-scale
    tokenized-asset settlement. Related pull requests attempt optimizations but do not yet
    eliminate the scalability bottleneck. The problem is classified as a performance and
    architecture limitation with potential research value, not documentation or a cosmetic
    feature request. Releases and roadmap discussions indicate that scalable verification
    remains an unresolved technical issue.
    """,
    "practitioner": """
    An engineer operating a real-world asset settlement system reports production pain from
    zero-knowledge verification latency. At high transaction volumes, proof generation and
    verification cause timeouts and reduce throughput. The practitioner describes deployment
    constraints, monitoring observations and repeated failures under load. The team has tried
    batching and parallel verification, but latency remains above the operational requirement.
    This is concrete practitioner evidence from a production context rather than marketing
    commentary. The problem affects secure settlement, privacy and scalability of tokenized
    assets and could motivate research into faster verification, better batching strategies,
    proof recursion and production-grade performance evaluation. The report identifies the
    role of the author as an engineer and provides operational context.
    """,
    "investment": """
    A venture investor led a $50M Series B investment in a company building infrastructure
    for real-world asset tokenization and privacy-preserving settlement. The round closed in
    August 2026 and followed earlier funding, indicating repeat investor interest and funding
    velocity in the sector. The investment thesis emphasizes enterprise settlement, compliance,
    scalable verification and institutional digital assets. Portfolio companies address similar
    infrastructure needs. This is strong market-validation evidence and a useful signal of
    commercial demand, but it does not by itself establish a scientific research gap. The
    record includes round size, date, stage, lead investor, sector and repeat-funding signals.
    """,
    "failure": """
    Security postmortem for a real-world asset settlement bridge reports a validator compromise
    that caused a $10M loss. The incident occurred in July 2026 and involved an attack vector
    exploiting insufficient verification and weak validator controls. The affected system was
    the bridge and settlement layer used for tokenized assets. The postmortem documents root
    causes, attack steps, operational impact and remediation attempts. The incident demonstrates
    a recurring security problem around verification, authorization and secure cross-domain
    settlement. Evidence comes from a technical incident report and is suitable for failure
    analysis because the mechanism, affected system, loss estimate and recurrence risk are
    explicitly described.
    """,
    "data_availability": """
    Public benchmark dataset for zero-knowledge verification of real-world asset settlement
    provides transaction traces, proving latency, verifier throughput, memory use and
    communication-overhead measurements. It covers multiple transaction volumes and includes
    reproducible scripts and documented evaluation procedures. The dataset is openly accessible
    under stated license terms and supports benchmarking of privacy-preserving verification.
    However, it lacks large-scale heterogeneous production traces and does not cover several
    recent proof systems, creating a benchmark gap. The documentation specifies dataset scope,
    coverage period, access conditions, metrics and reproducibility instructions. This makes it
    useful for evaluating the proposed RWA research while clearly identifying missing evidence.
    """,
    "hackathon": """
    Hackathon challenge sponsored by an RWA infrastructure organization asks teams to build a
    scalable zero-knowledge identity and settlement wallet for tokenized real-world assets.
    The challenge requires measurable verification latency, privacy protection, compliance
    integration and a production-oriented prototype. The organizer provides APIs, sample data,
    evaluation criteria and deployment constraints. The challenge indicates emerging developer
    and ecosystem demand, but it is not treated as proof of a novel scientific gap by itself.
    Stronger evidence would be literature, production failure, regulation, funding or deployment
    corroboration. The challenge is therefore classified as an emerging-demand signal with a
    concrete technical problem statement and measurable prototype requirements.
    """,
    "expert": """
    Professor at a research university works on zero-knowledge proofs, cryptography, privacy,
    distributed systems and scalable verification for real-world asset settlement. Recent papers
    from 2025 and 2026 study proving latency, recursive proofs, verifier parallelism and privacy-
    preserving compliance. The researcher leads a laboratory with relevant prototypes and has
    collaborated with industry on digital-asset infrastructure. Their recent publication record
    directly overlaps the RWA research problem, and the expertise includes both theoretical and
    applied work. The profile provides affiliation, role, research themes, recent-work evidence,
    relevant papers and collaboration context, making the person a plausible project partner.
    """,
}

AREAS = {name: "ZK-IoV" if name in {"literature", "data_availability", "hackathon", "expert"} else "RWA" for name in COLLECTION_AGENTS}


class AllCollectionAgentTests(unittest.TestCase):
    def test_registry_contains_exactly_12_collection_agents(self):
        self.assertEqual([name for name in COLLECTION_AGENTS if name in AGENT_REGISTRY], COLLECTION_AGENTS)
        self.assertEqual(len(COLLECTION_AGENTS), 12)

    def test_all_12_manual_collection_paths_produce_real_outputs(self):
        root = Path(tempfile.mkdtemp())
        for name in COLLECTION_AGENTS:
            with self.subTest(agent=name):
                result = run_registered_agent(
                    name,
                    "manual_text",
                    area=AREAS[name],
                    input_data={
                        "text": TEXTS[name],
                        "source_name": f"Test {name}",
                        "analysis_mode": "collect_only",
                        "output_dir": str(root / name),
                    },
                )
                self.assertEqual(result["status"], "success", result)
                self.assertGreaterEqual(result["items_processed"], 1, result)
                self.assertGreaterEqual(result["items_saved"], 1, result)
                self.assertGreaterEqual(len(result["outputs"]), 1, result)
                self.assertFalse(result.get("errors"), result)
                output = result["outputs"][0]
                self.assertTrue(output.get("source"), output)
                self.assertTrue(output.get("title"), output)
                self.assertTrue(output.get("problem"), output)

    def test_all_12_collection_schemas_exist_and_have_required_fields(self):
        for name in COLLECTION_AGENTS:
            with self.subTest(agent=name):
                schema = get_collection_schema(name)
                self.assertIsNotNone(schema)
                self.assertTrue(schema.fields)
                self.assertTrue(any(field.required for field in schema.fields))

    def test_failure_mode_isolated_and_does_not_crash_registry(self):
        root = Path(tempfile.mkdtemp())
        for name in COLLECTION_AGENTS:
            with self.subTest(agent=name):
                result = run_registered_agent(
                    name,
                    "manual_text",
                    area=AREAS[name],
                    input_data={"text": "", "source_name": "Empty", "output_dir": str(root / name)},
                )
                self.assertIn(result["status"], {"partial_success", "error"}, result)
                self.assertIsInstance(result["errors"], list)
                self.assertIsInstance(result["warnings"], list)

    def test_data_availability_new_fields_are_present(self):
        doc = DataDocument("Benchmark", "Benchmark", "Dataset Description", "ZK-IoV", "evaluation", "manual://x", TEXTS["data_availability"], "Manual")
        a = data_fallback(doc)
        self.assertTrue(a.benchmark_gap)
        self.assertTrue(a.reproducibility)
        self.assertTrue(a.dataset_scope)

    def test_expert_fit_fields_are_present(self):
        doc = ExpertDocument("Professor Profile", "Expert Profile", "Profile", "Professor", "University", "Faculty", "ZK-IoV", "cryptography", "manual://x", TEXTS["expert"], "Manual")
        a = expert_fallback(doc)
        self.assertGreaterEqual(a.collaboration_fit_score, 0)
        self.assertLessEqual(a.collaboration_fit_score, 100)
        self.assertTrue(a.recent_publication_signal)

    def test_failure_evidence_fields_are_present(self):
        doc = FailureDocument("Bridge Incident", "Postmortem", "Incident Report", "RWA", "bridge exploit", "manual://x", TEXTS["failure"], "Manual")
        a = failure_fallback(doc)
        self.assertTrue(a.attack_vector)
        self.assertTrue(a.evidence_strength)
        self.assertTrue(a.affected_system)


if __name__ == "__main__":
    unittest.main()
