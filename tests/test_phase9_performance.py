"""Phase 9 regression tests: local preflight, extraction/token cache, telemetry.

Covers plan section 13.1 (deterministic preflight checks), 13.2 (extraction
and token-count caching), and 13.4 (output-size and search-budget telemetry).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from src import extraction_cache
from src.code_cycles import CALIFORNIA_2025
from src.diagnostics import DiagnosticsReport
from src.extraction_cache import (
    cache_token_count,
    clear_extraction_cache,
    clear_token_cache,
    extract_multiple_specs_cached,
    extract_text_cached,
    extraction_cache_stats,
    get_cached_token_count,
    token_count_cache_key,
)
from src.preprocessor import (
    detect_duplicate_headings,
    detect_empty_sections,
    detect_inconsistent_file_naming,
    detect_stale_code_cycle_references,
    preprocess_spec,
)


# --- Section 13.1: preflight checks ----------------------------------------


def test_stale_code_cycle_flags_old_cbc_when_cycle_is_2025():
    content = "References to 2019 CBC must be updated. The 2025 CBC governs."
    alerts = detect_stale_code_cycle_references(content, "spec.docx", CALIFORNIA_2025)
    assert any("2019" in a["match"] for a in alerts)
    assert all(a["expected_year"] == "2025" for a in alerts)
    # 2025 references must NOT be flagged.
    assert not any("2025" in a["match"] for a in alerts)


def test_stale_code_cycle_does_not_flag_current_cycle():
    content = "Comply with 2025 CBC."
    alerts = detect_stale_code_cycle_references(content, "spec.docx", CALIFORNIA_2025)
    assert alerts == []


def test_stale_code_cycle_flags_old_asce7_edition():
    content = "Provide ASCE 7-10 bracing."
    alerts = detect_stale_code_cycle_references(content, "spec.docx", CALIFORNIA_2025)
    assert any("ASCE" in a["match"].upper() for a in alerts)


def test_detect_empty_sections_reports_heading_with_no_body():
    content = (
        "1.01 GENERAL\n\n"
        "Body text for general.\n\n"
        "1.02 EMPTY SECTION\n\n"
        "1.03 NEXT SECTION\n\n"
        "Body text here."
    )
    alerts = detect_empty_sections(content, "spec.docx")
    assert any(a["section_number"] == "1.02" for a in alerts)
    assert not any(a["section_number"] == "1.01" for a in alerts)


def test_detect_duplicate_headings_reports_repeats():
    content = (
        "1.01 GENERAL\n\nFirst body.\n\n"
        "1.02 PRODUCTS\n\nProducts body.\n\n"
        "1.01 GENERAL\n\nSecond body."
    )
    alerts = detect_duplicate_headings(content, "spec.docx")
    assert any(a["section_number"] == "1.01" for a in alerts)


def test_detect_duplicate_headings_no_alert_for_unique_headings():
    content = "1.01 GENERAL\n\nBody.\n\n1.02 PRODUCTS\n\nBody."
    alerts = detect_duplicate_headings(content, "spec.docx")
    assert alerts == []


def test_detect_inconsistent_file_naming_flags_mixed_styles():
    files = [
        "23 21 13 - Hydronic Piping.docx",
        "23 22 13 - Steam Piping.docx",
        "23-23-13 - Refrigerant Piping.docx",  # dash style, minority
    ]
    alerts = detect_inconsistent_file_naming(files)
    assert len(alerts) == 1
    assert alerts[0]["filename"] == "23-23-13 - Refrigerant Piping.docx"


def test_detect_inconsistent_file_naming_no_alert_when_all_match():
    files = ["23 21 13 - A.docx", "23 22 13 - B.docx"]
    assert detect_inconsistent_file_naming(files) == []


def test_preprocess_spec_includes_phase9_alerts_when_cycle_provided():
    content = "1.01 OLD\n\nUse 2019 CBC.\n\n1.01 OLD\n\nMore body."
    result = preprocess_spec(content, "spec.docx", cycle=CALIFORNIA_2025)
    assert result.code_cycle_alerts, "expected cycle alert"
    assert result.structural_alerts, "expected duplicate heading alert"


def test_preprocess_spec_without_cycle_skips_cycle_alerts():
    content = "Use 2019 CBC."
    result = preprocess_spec(content, "spec.docx")
    assert result.code_cycle_alerts == []


# --- Section 13.2: extraction and token-count cache ------------------------


def _make_docx(path: Path, body: str) -> None:
    doc = Document()
    doc.add_paragraph(body)
    doc.save(path)


def test_extract_text_cached_returns_same_content_on_hit(tmp_path: Path):
    clear_extraction_cache()
    p = tmp_path / "a.docx"
    _make_docx(p, "Hello cache.")
    first = extract_text_cached(p)
    second = extract_text_cached(p)
    assert first.content == second.content
    stats = extraction_cache_stats()
    assert stats["hits"] >= 1


def test_extract_text_cached_invalidates_after_modification(tmp_path: Path):
    clear_extraction_cache()
    p = tmp_path / "b.docx"
    _make_docx(p, "First version.")
    first = extract_text_cached(p)
    # Rewrite with different body and bump mtime.
    import os
    import time as _t
    _t.sleep(0.01)
    _make_docx(p, "Second version completely different.")
    # Force mtime change for filesystems with low resolution.
    new_mtime = p.stat().st_mtime + 5
    os.utime(p, (new_mtime, new_mtime))
    second = extract_text_cached(p)
    assert "Second" in second.content
    assert first.content != second.content


def test_extract_text_cached_isolates_mutations(tmp_path: Path):
    clear_extraction_cache()
    p = tmp_path / "iso.docx"
    _make_docx(p, "Stable text.")
    first = extract_text_cached(p)
    # Mutate the returned spec — the next call must return a fresh copy.
    first.content = "MUTATED"
    if first.paragraph_map is not None:
        first.paragraph_map.clear()
    second = extract_text_cached(p)
    assert second.content == "Stable text."
    assert second.paragraph_map  # still populated


def test_extract_multiple_specs_cached_preserves_order(tmp_path: Path):
    clear_extraction_cache()
    paths = []
    for letter in "abcd":
        p = tmp_path / f"{letter}.docx"
        _make_docx(p, f"Body {letter}.")
        paths.append(p)
    first = extract_multiple_specs_cached(paths)
    assert [s.filename for s in first] == [f"{l}.docx" for l in "abcd"]
    second = extract_multiple_specs_cached(paths)
    assert [s.filename for s in second] == [f"{l}.docx" for l in "abcd"]
    stats = extraction_cache_stats()
    assert stats["hits"] >= 4  # all four hit on the second pass


def test_extraction_cache_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_EXTRACTION_CACHE", "0")
    clear_extraction_cache()
    p = tmp_path / "off.docx"
    _make_docx(p, "Body.")
    extract_text_cached(p)
    extract_text_cached(p)
    stats = extraction_cache_stats()
    assert stats["hits"] == 0


def test_token_count_cache_round_trip():
    clear_token_cache()
    key = token_count_cache_key(
        model="claude-opus-4-6",
        system_prompt="sys",
        user_message="msg",
        cycle_label="2025",
    )
    assert get_cached_token_count(key) is None
    cache_token_count(key, 12345)
    assert get_cached_token_count(key) == 12345


def test_token_count_cache_key_changes_with_cycle():
    a = token_count_cache_key(
        model="m", system_prompt="s", user_message="u", cycle_label="2025",
    )
    b = token_count_cache_key(
        model="m", system_prompt="s", user_message="u", cycle_label="2028",
    )
    assert a != b


# --- Section 13.4: output-size and search-budget telemetry -----------------


def test_diagnostics_summary_reports_output_telemetry():
    diag = DiagnosticsReport()
    diag.log("review", "success", "ok", {
        "input_tokens": 100, "output_tokens": 1000,
        "stop_reason": "end_turn", "max_output_tokens": 64_000,
    })
    diag.log("review", "success", "ok", {
        "input_tokens": 100, "output_tokens": 5000,
        "stop_reason": "max_tokens", "max_output_tokens": 64_000,
    })
    diag.finish()
    summary = diag.summary()
    out_t = summary["output_telemetry"]
    assert out_t["samples"] == 2
    assert out_t["max_observed"] == 5000
    assert out_t["truncated_calls"] == 1
    assert out_t["truncated_by_phase"] == {"review": 1}
    assert out_t["max_cap_observed"] == 64_000


def test_diagnostics_summary_reports_search_budget():
    diag = DiagnosticsReport()
    # Two web-grounded verifications and one cache hit (should be excluded).
    diag.log("verification", "info", "v1", {
        "verdict": "CONFIRMED", "grounded": True, "cache_status": "miss",
        "web_search_requests": 3, "search_error_count": 0,
    })
    diag.log("verification", "info", "v2", {
        "verdict": "CONFIRMED", "grounded": True, "cache_status": "miss",
        "web_search_requests": 5, "search_error_count": 1,
    })
    diag.log("verification", "info", "v3", {
        "verdict": "CONFIRMED", "grounded": True, "cache_status": "hit",
        "web_search_requests": 0, "search_error_count": 0,
    })
    diag.finish()
    summary = diag.summary()
    budget = summary["search_budget"]
    assert budget["samples"] == 2
    assert budget["max_observed"] == 5
    assert budget["total"] == 8


def test_diagnostics_text_report_includes_telemetry_when_present():
    diag = DiagnosticsReport()
    diag.log("review", "success", "ok", {
        "input_tokens": 1, "output_tokens": 2000,
        "stop_reason": "end_turn", "max_output_tokens": 64_000,
    })
    diag.log("verification", "info", "v", {
        "verdict": "CONFIRMED", "grounded": True, "cache_status": "miss",
        "web_search_requests": 4,
    })
    diag.finish()
    text = diag.to_text()
    assert "Output Tokens" in text
    assert "Search Budget" in text
