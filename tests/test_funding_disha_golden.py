import pytest
from core.agent_registry import run_registered_agent

def test_funding_agent_disha_golden():
    text = """Greetings from IITI DRISHTI CPS Foundation, IIT Indore.
We are delighted to invite you and your esteemed institution/organisation to participate in DISHA 3.0 (Developing Innovations for Successful Harnessing and Adoption), an initiative of IITI DRISHTI CPS Foundation focused on advancing innovative Cyber-Physical Systems (CPS) technologies in Digital Healthcare and facilitating their translation from research and innovation to real-world applications.
About DISHA 3.0
DISHA 3.0 aims to identify and support promising research, technologies, and innovations with the potential to make a meaningful impact in Digital Healthcare.
The program provides comprehensive support for technology development and translation through funding, technical and domain mentorship, product development, technology validation, commercialization support, go-to-market guidance, and industry and investor connect.
The program is designed to help innovators move their technologies from research and proof of concept to validation, deployment, and market adoption.
Who Can Apply
Faculty members and researchers from higher education and research institutions across India with innovative technologies, research outcomes, prototypes, or solutions in Digital Healthcare.
Faculty members and research teams seeking support to advance their technology, validate their solutions, develop products, or explore commercialisation and spinoff opportunities.
DPIIT-registered deep-tech startups working on innovative Digital Healthcare technologies with potential for further development and commercialisation.
Innovators, technology developers, founders, and teams with promising solutions seeking support for technology development, product refinement, market validation, commercialisation, and scaling.
Program Highlights
Financial Support: Up to ₹25 lakh–₹1 crore for projects and ₹25 lakh–₹2 crore for startups, based on TRL/stage, with milestone-based disbursement.
Technical & Domain Mentorship: Guidance from experienced experts for technology development, validation, and application.
Technology & Product Development: Support for prototype refinement, testing, validation, product development, and technology maturation.
Go-to-Market Support: Assistance in developing commercialization, market-entry, and adoption strategies.
Business & Product Development: Support for product-market fit, business models, regulatory considerations, and scaling.
Industry & Investor Connect: Opportunities to engage with industry leaders, investors, healthcare stakeholders, and ecosystem partners.
Innovation Resources: Access to relevant entrepreneurial resources, credits, equipment, and other ecosystem support.
Eligibility Criteria
Proposed technologies must focus on Digital Healthcare within the Cyber-Physical Systems (CPS) domain.
Startups must be registered with Startup India and have valid DPIIT registration before fund transfer.
A minimum of 51% of startup shares must be held by Indian citizens (excluding PIO/OCI holders).
Project proposals must include at least 1 Ph.D. candidate and 2 UG candidates for a duration of 6–18 months.
Applicants should demonstrate a clear understanding of the target application, market opportunity, and competing technologies/solutions.
Applicable equity transfer (1%–5%) and revenue-sharing provisions will apply as per the program guidelines.
Why Participate?
DISHA 3.0 provides an opportunity to:
Translate research and innovative technologies into real-world healthcare solutions.
Advance promising technologies towards higher TRLs and market readiness.
Access funding and expert mentorship for technology development and validation.
Strengthen product-market fit and commercialisation pathways.
Explore industry collaboration, technology licensing, and spin off opportunities.
Connect with industry experts, investors, healthcare stakeholders, and ecosystem partners.
Application Link
Apply for DISHA 3.0:
[DISHA 3.0 Application Portal](https://iiti-ac-in-dot-summer-prism-462512-u0.uc.r.appspot.com/?c=1yrmP3vyITgjDn4FSpgasAlIK1ItOV6p_RXuEH0xjVPk&q=688685264&r=1a057459ae9c5041&z=1788170574721&o=https%3A%2F%2Fincubator.drishticps.org%2Ffa%2F6a7187f15e4be7172e5d18fa%3Futm_source%3Dchatgpt.com)
We encourage you to share this opportunity with faculty members, researchers, research teams, innovators, technology developers, startups, and other relevant stakeholders working in the field of Digital Healthcare.
We look forward to your participation and to supporting the translation of promising research and technologies into impactful, scalable, and market-ready healthcare solutions.
"""

    result = run_registered_agent(
        agent_name="funding",
        mode="manual_text",
        area="Digital Healthcare",
        input_data={
            "text": text,
            "title": "DISHA 3.0 Funding Call",
            "url": "https://incubator.drishticps.org"
        }
    )
    
    assert result["status"] == "success"
    assert len(result["outputs"]) == 1
    
    output = result["outputs"][0]
    
    # 1. Verify canonical structure (no problem_statement at top level)
    assert "problem_statement" not in output
    assert output["opportunity_type"] == "FUNDING_CALL"
    assert output["status"] == "OPEN_CALL"
    assert "Projects: ₹25 lakh–₹1 crore" in output["funding_amount"]
    assert "Startups: ₹25 lakh–₹2 crore" in output["funding_amount"]
    assert "IITI DRISHTI CPS Foundation" in output["funding_body"]
    assert output["program_name"] == "DISHA 3.0 (Developing Innovations for Successful Harnessing and Adoption)"
    
    # 2. Verify research context (Deterministic Mission Generator)
    rc = output["research_context"]
    assert rc is not None
    assert "Cyber-Physical Systems" in rc["technology_themes"]
    assert "Digital Healthcare" in rc["technology_themes"]
    
    # Check investigation questions generated
    questions = rc["investigation_questions"]
    assert len(questions) > 3
    assert any("What technical barriers" in q for q in questions)
    
    # Check provenance
    assert "INFERENCE" in rc["inference_provenance"]
    
    # Ensure problem_intelligence is NOT present
    assert "problem_intelligence" not in output

def test_funding_agent_negative_not_a_call():
    text = "We just awarded 3 projects in the area of ZK-IoV. Congratulations to the teams."
    result = run_registered_agent(
        agent_name="funding",
        mode="manual_text",
        area="ZK-IoV",
        input_data={
            "text": text,
            "title": "Announcement",
            "url": "https://example.com"
        }
    )
    
    assert result["status"] == "success"
    output = result["outputs"][0]
    assert output["opportunity_type"] == "FUNDED_PROJECT"

