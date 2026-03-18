from pathlib import Path

import pytest

from src.code_cycles import CALIFORNIA_2022, CALIFORNIA_2025
from src.extractor import SUPPORTED_EXTENSIONS, extract_text
from src.prompts import get_system_prompt, get_single_spec_user_message
from src.pipeline import _deduplicate_findings
from src.reviewer import Finding
from src.cross_checker import run_cross_check


def test_supported_extensions_docx_only(tmp_path: Path):
    assert SUPPORTED_EXTENSIONS == {".docx"}
    pdf = tmp_path / "sample.pdf"
    pdf.write_text("not a docx")
    with pytest.raises(ValueError):
        extract_text(pdf)


def test_cycle_prompts_change():
    p2022 = get_system_prompt(CALIFORNIA_2022)
    p2025 = get_system_prompt(CALIFORNIA_2025)
    assert "CBC 2022" in p2022
    assert "CBC 2025" in p2025

    msg = get_single_spec_user_message("Body", "file.docx", cycle=CALIFORNIA_2025)
    assert "ASCE 7-22" in msg


def test_dedup_does_not_merge_different_edits():
    f1 = Finding(severity="HIGH", fileName="a.docx", section="1", issue="Same issue", actionType="EDIT", existingText="foo", replacementText="bar", codeReference="CBC", confidence=0.8)
    f2 = Finding(severity="HIGH", fileName="b.docx", section="1", issue="Same issue", actionType="EDIT", existingText="different", replacementText="bar", codeReference="CBC", confidence=0.8)
    deduped = _deduplicate_findings([f1, f2])
    assert len(deduped) == 2


def test_cross_check_skip_status():
    result = run_cross_check([], [])
    assert result.cross_check_status == "skipped"