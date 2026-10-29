from core.crawl4ai_web import _is_supporting_funding_document


def test_current_call_supporting_attachments_are_not_standalone_opportunities():
    urls = [
        "https://neb.eitcommunity.eu/wp-content/uploads/2026/09/Guidelines-for-applicants_Enhance-NEB.pdf",
        "https://neb.eitcommunity.eu/wp-content/uploads/2026/09/Declaration-of-Affiliation-Template.docx",
        "https://womentecheurope.eu/wp-content/uploads/2026/09/WomenTechEU_Guidelines_Eligibility-V9.0.pdf",
        "https://womentecheurope.eu/wp-content/uploads/2026/07/Annex1_WomenTechEU_FullProposal_Application-form.pdf",
        "https://womentecheurope.eu/wp-content/uploads/2026/06/WTEU2EIC-Annex-1-Application-form-for-evaluators.pdf",
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/SME-Market-Expansion-Open-Call-2027_Guidance-for-Applicants_optimized.pdf",
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/09/Letter-of-Commitment-SME-Market-Expansion-Call-Template_PUBLIC-SECTOR.docx",
        "https://eitcc-dev.wmstudio.space/sites/default/files/2026-08/EITCC_Eligibility%20of%20Expenditures_v1-2026%20%281%29.pdf",
    ]
    for url in urls:
        assert _is_supporting_funding_document(url), url


def test_substantive_call_documents_are_not_overfiltered():
    assert not _is_supporting_funding_document(
        "https://neb.eitcommunity.eu/wp-content/uploads/2026/09/Enhance_NEB_with-cover.pdf"
    )
    assert not _is_supporting_funding_document(
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/Call-Manual_SME-Market-Expansion-Call-2027_final_v1.2_271026.pdf"
    )
