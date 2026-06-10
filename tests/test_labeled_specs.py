"""Structural invariants of the labeled spec set (:mod:`evals.labeled_specs`).

The labels are the live-capture eval's oracle: ``expected_verdict`` /
``expected_status`` seed calibration fixtures and the categories slot into
the calibration scorer's per-profile view. These tests pin the set's
contract so a future label edit (or growth pass) can't silently feed the
harness a value the loaders reject — wrong ground truth is worse than no
ground truth, and a structurally invalid label is the cheapest kind of
wrong to catch.
"""
from __future__ import annotations

import pytest

from evals.labeled_specs import LABELED_SPECS, defect_matched
from evals.live_capture import _VALID_VERDICTS
from src.output.report_status import ReportStatus
from src.verification.verification_profiles import VerificationProfile

_VALID_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "GRIPES"})
_VALID_CATEGORIES = frozenset(p.value for p in VerificationProfile)
_VALID_STATUSES = frozenset(s.value for s in ReportStatus)

# Cost guard: every spec is reviewed live on the flagship model per capture
# run. Bodies are deliberately tiny — a body this large means someone pasted
# a real spec into the labeled set instead of distilling the defect.
_MAX_BODY_CHARS = 2_000

_ALL_DEFECTS = [
    (spec.spec_id, defect) for spec in LABELED_SPECS for defect in spec.expected_defects
]


class TestLabeledSetIntegrity:
    def test_spec_ids_unique(self) -> None:
        ids = [s.spec_id for s in LABELED_SPECS]
        assert len(ids) == len(set(ids))

    def test_clean_specs_carry_no_defects_and_dirty_specs_carry_some(self) -> None:
        for spec in LABELED_SPECS:
            if spec.is_clean:
                assert not spec.expected_defects, spec.spec_id
            else:
                assert spec.expected_defects, spec.spec_id

    def test_set_keeps_multiple_clean_specs(self) -> None:
        # False-positive measurement needs more than one clean sample.
        assert sum(1 for s in LABELED_SPECS if s.is_clean) >= 2

    def test_bodies_present_and_tiny(self) -> None:
        for spec in LABELED_SPECS:
            assert spec.spec_text.strip(), spec.spec_id
            assert len(spec.spec_text) <= _MAX_BODY_CHARS, (
                f"{spec.spec_id}: body is {len(spec.spec_text)} chars — distill it"
            )

    def test_categories_match_verification_profile_taxonomy(self) -> None:
        for spec in LABELED_SPECS:
            assert spec.category in _VALID_CATEGORIES, (
                f"{spec.spec_id}: category {spec.category!r} is not a "
                "VerificationProfile value"
            )

    @pytest.mark.parametrize("spec_id,defect", _ALL_DEFECTS, ids=lambda v: str(v)[:40])
    def test_defect_labels_are_loader_compatible(self, spec_id, defect) -> None:
        assert defect.label.strip(), spec_id
        # The substring fallback must always be possible: non-empty tokens.
        assert defect.must_match and all(t.strip() for t in defect.must_match), spec_id
        assert defect.expected_severity in _VALID_SEVERITIES, spec_id
        # The calibration loader rejects fixtures whose seeded verdict is
        # outside its accepted set — catch that here, not mid-capture.
        assert defect.expected_verdict in _VALID_VERDICTS, spec_id
        if defect.expected_status is not None:
            assert defect.expected_status in _VALID_STATUSES, spec_id

    def test_duplicate_paragraph_spec_actually_duplicates(self) -> None:
        """The duplicated paragraph must stay verbatim and >= 80 chars —
        the deterministic detector's threshold — or the case stops testing
        what its label claims."""
        spec = next(s for s in LABELED_SPECS if s.spec_id == "duplicate_paragraph")
        paragraphs = [
            line.lstrip("AB. ").strip()
            for line in spec.spec_text.splitlines()
            if line.startswith(("A. ", "B. "))
        ]
        assert len(paragraphs) == 2
        assert paragraphs[0] == paragraphs[1]
        assert len(paragraphs[0]) >= 80

    def test_substring_fallback_self_consistent(self) -> None:
        """Each defect's tokens match a finding that quotes its own label +
        the spec line it targets — the minimal sanity check that the
        fallback matcher CAN fire for every labeled defect."""

        class _Probe:
            def __init__(self, text: str):
                self.issue = text
                self.existingText = text
                self.section = ""
                self.codeReference = ""

        for spec in LABELED_SPECS:
            for defect in spec.expected_defects:
                probe = _Probe(f"{defect.label} {spec.spec_text}")
                assert defect_matched(defect, [probe]) is probe, (
                    f"{spec.spec_id}: must_match tokens {defect.must_match!r} "
                    "can never fire even against label+body text"
                )
