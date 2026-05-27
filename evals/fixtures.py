"""Golden-set fixture taxonomy.

Every fixture knows three things:

1. **What it represents.** ``category`` is one of the cases the harness
   covers (``clean_spec``, ``stale_code_cycle``, ``placeholder``,
   ``internal_contradiction``, ``coordination``, ``valid_edit``,
   ``invalid_edit``, ``verification_with_source``,
   ``verification_sourceless_confirmed``).
2. **How to exercise production code.** ``spec_text`` is the raw spec
   body; ``review_payload`` / ``verification_payload`` are dicts that
   match the structured tool schemas (so the production parsers in
   :mod:`src.reviewer` / :mod:`src.verifier` consume them unchanged).
3. **What "right" looks like.** ``expected`` carries the per-metric
   ground-truth values the harness checks against; a missing key means
   the metric does not apply to this fixture.

The fixtures are deliberately small so the whole harness runs in
under a second. Each one is a single contract-shaped case, not a
realistic full-spec sample — that's the eval part of "golden set":
we measure parser / detector / verifier behavior, not model behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Fixture data class
# ---------------------------------------------------------------------------


@dataclass
class GoldenFixture:
    """One row in the golden-set taxonomy."""

    fixture_id: str
    category: str
    description: str
    spec_text: str = ""
    filename: str = "fixture.docx"
    review_payload: Optional[dict] = None
    verification_payload: Optional[dict] = None
    searched_urls: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Spec body constants — kept tiny because every fixture only needs enough
# text to exercise one production code path.
# ---------------------------------------------------------------------------

_CLEAN_SPEC_BODY = (
    "SECTION 23 21 13 - HYDRONIC PIPING\n"
    "PART 1 GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Comply with California Plumbing Code 2025 requirements.\n"
    "1.02 REFERENCES\n"
    "A. CBC 2025 - California Building Code.\n"
    "PART 2 PRODUCTS\n"
    "2.01 GENERAL\n"
    "A. Provide hydronic piping as scheduled.\n"
    "PART 3 EXECUTION\n"
    "3.01 INSTALLATION\n"
    "A. Install per manufacturer's written instructions.\n"
)

_STALE_CYCLE_SPEC_BODY = (
    "SECTION 23 21 13 - HYDRONIC PIPING\n"
    "PART 1 GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Comply with 2019 CBC requirements.\n"
    "1.02 REFERENCES\n"
    "A. CMC 2019 - California Mechanical Code.\n"
)

_PLACEHOLDER_SPEC_BODY = (
    "SECTION 22 13 13 - SANITARY SEWER\n"
    "PART 1 GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Provide [INSERT MANUFACTURER] equipment per project requirements.\n"
    "B. Submittals due TODO: confirm date with owner.\n"
)

_INTERNAL_CONTRADICTION_BODY = (
    "SECTION 23 31 13 - DUCTWORK\n"
    "PART 2 PRODUCTS\n"
    "2.01 GENERAL\n"
    "A. Provide galvanized steel ductwork rated for 2 inches w.g.\n"
    "B. Duct shall be rated for 4 inches w.g. throughout.\n"
)

_COORDINATION_SPEC_BODY = (
    "SECTION 23 05 23 - VALVES\n"
    "PART 2 PRODUCTS\n"
    "2.01 GENERAL\n"
    "A. Valves: see Section 22 05 23 for chilled water valves.\n"
    "B. Tagging: per Section 23 05 53.\n"
)


# ---------------------------------------------------------------------------
# Helper builders for structured payloads
# ---------------------------------------------------------------------------


def _review_payload(findings: list[dict], summary: str = "Reviewed.") -> dict:
    """Return a structured ``submit_review_findings`` tool input dict."""
    return {"analysis_summary": summary, "findings": findings}


def _verdict_payload(
    *,
    verdict: str,
    explanation: str,
    sources: list[str] | None = None,
    correction: str | None = None,
) -> dict:
    """Return a structured ``submit_verification_verdict`` tool input dict."""
    return {
        "verdict": verdict,
        "explanation": explanation,
        "sources": sources or [],
        "correction": correction,
    }


# ---------------------------------------------------------------------------
# The 10-case taxonomy
# ---------------------------------------------------------------------------


def _make_fixtures() -> list[GoldenFixture]:
    fixtures: list[GoldenFixture] = []

    # ------------------------------------------------------------------
    # 1. Clean spec — no expected findings.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="clean_spec",
        category="clean_spec",
        description="Clean spec on the current cycle; review should emit zero findings.",
        spec_text=_CLEAN_SPEC_BODY,
        filename="23 21 13 - Hydronic Clean.docx",
        review_payload=_review_payload(findings=[], summary="No issues identified."),
        expected={
            "seeded_finding_count": 0,
            "expected_review_findings": 0,
            "expected_false_positive_count": 0,
            "preprocessor_alerts_expected": 0,
        },
    ))

    # ------------------------------------------------------------------
    # 2. Known stale code-cycle issue.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="stale_code_cycle",
        category="stale_code_cycle",
        description="Spec cites 2019 CBC / CMC 2019; preprocessor + review should flag.",
        spec_text=_STALE_CYCLE_SPEC_BODY,
        filename="23 21 13 - Hydronic Stale.docx",
        review_payload=_review_payload(
            findings=[{
                "severity": "HIGH",
                "fileName": "23 21 13 - Hydronic Stale.docx",
                "section": "1.01",
                "issue": "Cited California Building Code edition is outdated for the 2025 cycle.",
                "actionType": "EDIT",
                "existingText": "2019 CBC",
                "replacementText": "2025 CBC",
                "codeReference": "CBC 2025",
                "confidence": 0.85,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": "p3",
            }],
            summary="One stale code reference found.",
        ),
        expected={
            "seeded_finding_count": 1,
            "expected_review_findings": 1,
            "preprocessor_alerts_expected_min": 1,
            "preprocessor_rule_expected": "stale_code_cycle",
        },
    ))

    # ------------------------------------------------------------------
    # 3. Placeholder / template marker.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="placeholder_marker",
        category="placeholder",
        description="Spec contains [INSERT ...] placeholder and TODO marker; preprocessor should catch both.",
        spec_text=_PLACEHOLDER_SPEC_BODY,
        filename="22 13 13 - Sanitary Placeholder.docx",
        review_payload=_review_payload(findings=[], summary="Preprocessor alerts cover the placeholders."),
        expected={
            "seeded_finding_count": 0,
            "expected_review_findings": 0,
            "preprocessor_alerts_expected_min": 2,
            "preprocessor_rules_expected_any": ["placeholder", "template_marker"],
        },
    ))

    # ------------------------------------------------------------------
    # 4. Internal contradiction — REPORT_ONLY, no executable edit.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="internal_contradiction",
        category="internal_contradiction",
        description="Ductwork pressure rating contradiction; reported as REPORT_ONLY.",
        spec_text=_INTERNAL_CONTRADICTION_BODY,
        filename="23 31 13 - Ductwork.docx",
        review_payload=_review_payload(
            findings=[{
                "severity": "HIGH",
                "fileName": "23 31 13 - Ductwork.docx",
                "section": "2.01",
                "issue": "Duct pressure rating is contradictory: 2 in. w.g. vs. 4 in. w.g.",
                "actionType": "REPORT_ONLY",
                "existingText": None,
                "replacementText": None,
                "codeReference": None,
                "confidence": 0.8,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": "p3",
            }],
            summary="Internal contradiction detected.",
        ),
        expected={
            "seeded_finding_count": 1,
            "expected_review_findings": 1,
            "expected_report_only": 1,
        },
    ))

    # ------------------------------------------------------------------
    # 5. Cross-section coordination — REPORT_ONLY by intent.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="coordination",
        category="coordination",
        description="Section 23 05 23 references Section 22 05 23; coordination flag, no edit.",
        spec_text=_COORDINATION_SPEC_BODY,
        filename="23 05 23 - Valves.docx",
        review_payload=_review_payload(
            findings=[{
                "severity": "MEDIUM",
                "fileName": "23 05 23 - Valves.docx",
                "section": "2.01",
                "issue": "Coordinate valve scope with Section 22 05 23.",
                "actionType": "REPORT_ONLY",
                "existingText": None,
                "replacementText": None,
                "codeReference": None,
                "confidence": 0.7,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": "p3",
            }],
            summary="Coordination finding emitted.",
        ),
        expected={
            "seeded_finding_count": 1,
            "expected_review_findings": 1,
            "expected_report_only": 1,
        },
    ))

    # ------------------------------------------------------------------
    # 6. Valid edit proposal — EDIT must survive parse with a proposal.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="valid_edit",
        category="valid_edit",
        description="EDIT with both existingText and replacementText survives parse.",
        spec_text=_STALE_CYCLE_SPEC_BODY,
        filename="23 21 13 - Hydronic ValidEdit.docx",
        review_payload=_review_payload(
            findings=[{
                "severity": "HIGH",
                "fileName": "23 21 13 - Hydronic ValidEdit.docx",
                "section": "1.01",
                "issue": "Cited CBC edition is outdated.",
                "actionType": "EDIT",
                "existingText": "2019 CBC",
                "replacementText": "2025 CBC",
                "codeReference": "CBC 2025",
                "confidence": 0.9,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": None,
            }],
            summary="One safe edit candidate.",
        ),
        expected={
            "seeded_finding_count": 1,
            "expected_review_findings": 1,
            "expected_edit_proposal_valid": 1,
        },
    ))

    # ------------------------------------------------------------------
    # 7. Invalid edit proposal — must demote to REPORT_ONLY at parse time.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="invalid_edit_missing_existing",
        category="invalid_edit",
        description="EDIT with missing existingText must demote to REPORT_ONLY (Chunk 7).",
        spec_text=_STALE_CYCLE_SPEC_BODY,
        filename="23 21 13 - Hydronic InvalidEdit.docx",
        review_payload=_review_payload(
            findings=[{
                "severity": "HIGH",
                "fileName": "23 21 13 - Hydronic InvalidEdit.docx",
                "section": "1.01",
                "issue": "Cited CBC edition is outdated.",
                "actionType": "EDIT",
                "existingText": None,
                "replacementText": "2025 CBC",
                "codeReference": "CBC 2025",
                "confidence": 0.85,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": None,
            }],
            summary="Edit with missing fields — should demote.",
        ),
        expected={
            "seeded_finding_count": 1,
            "expected_review_findings": 1,
            "expected_demoted_findings": 1,
            "expected_edit_proposal_valid": 0,
        },
    ))

    # ------------------------------------------------------------------
    # 8. Verification with accepted source — CONFIRMED stays CONFIRMED.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="verification_accepted_source",
        category="verification_with_source",
        description="CONFIRMED verdict with a cited URL that the search tool actually retrieved.",
        verification_payload=_verdict_payload(
            verdict="CONFIRMED",
            explanation="2025 CBC is the current code cycle per DSA.",
            sources=["https://www.dgs.ca.gov/DSA/"],
        ),
        searched_urls=["https://www.dgs.ca.gov/DSA/"],
        expected={
            "expected_verdict_after_grounding": "CONFIRMED",
            "expected_accepted_citation_count": 1,
            "expected_downgrade": False,
        },
    ))

    # ------------------------------------------------------------------
    # 9. Verification source-less CONFIRMED — must downgrade to UNVERIFIED.
    # ------------------------------------------------------------------
    fixtures.append(GoldenFixture(
        fixture_id="verification_sourceless_confirmed",
        category="verification_sourceless_confirmed",
        description="CONFIRMED verdict with no accepted citations — must downgrade (Chunk 5).",
        verification_payload=_verdict_payload(
            verdict="CONFIRMED",
            explanation="The 2025 CBC is the current cycle.",
            sources=["https://invented.example.com/never-searched"],
        ),
        # Searched returns no URLs at all so the cited URL cannot ground.
        searched_urls=[],
        expected={
            "expected_verdict_after_grounding": "UNVERIFIED",
            "expected_accepted_citation_count": 0,
            "expected_downgrade": True,
        },
    ))

    return fixtures


_FIXTURES: list[GoldenFixture] = _make_fixtures()


def all_fixtures() -> list[GoldenFixture]:
    """Return a fresh list of every golden-set fixture (in stable order)."""
    return list(_FIXTURES)


def fixture_by_id(fixture_id: str) -> GoldenFixture:
    """Look up a fixture by its ``fixture_id``."""
    for fx in _FIXTURES:
        if fx.fixture_id == fixture_id:
            return fx
    raise KeyError(fixture_id)
