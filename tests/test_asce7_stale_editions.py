"""Deterministic stale-ASCE-7 detection covers pre-2005 editions
(TRUST_AUDIT P2-1).

The stale-ASCE-7 detector previously recognized only editions
``{05,10,16,22}`` and compared two-digit years directly. Two gaps fell out
of that:

1. Genuinely old real editions (7-88/93/95/98/02) were ``not in`` the
   recognized set, so they were skipped and never flagged.
2. Even once recognized, a naive two-digit comparison inverts across the
   century boundary — ``int("98") >= int("22")`` treats the 1998 edition as
   *newer* than 2022 — so a pre-2000 edition would be skipped anyway.

These tests pin both halves of the fix: every real edition older than the
cycle's ASCE 7 edition (7-22 for California 2025) is flagged, the current
edition is not, stray non-edition numbers are ignored, and the existing
suppression / already-recognized behavior is unchanged.
"""
from __future__ import annotations

import pytest

from src.core.code_cycles import CALIFORNIA_2025
from src.input.preprocessor import (
    DETERMINISTIC_RULE_STALE_ASCE7,
    _asce7_edition_year,
    detect_stale_code_cycle_references,
)


def _asce7_alerts(content: str) -> list[dict]:
    """Run the detector and keep only the stale-ASCE-7 alerts."""
    alerts = detect_stale_code_cycle_references(content, "s.docx", CALIFORNIA_2025)
    return [a for a in alerts if a["deterministic_rule"] == DETERMINISTIC_RULE_STALE_ASCE7]


# ---------------------------------------------------------------------------
# 1. Century-aware year widening
# ---------------------------------------------------------------------------


class TestEditionYear:
    @pytest.mark.parametrize(
        "two_digit,expected",
        [
            ("88", 1988),
            ("93", 1993),
            ("95", 1995),
            ("98", 1998),
            ("02", 2002),
            ("05", 2005),
            ("10", 2010),
            ("16", 2016),
            ("22", 2022),
        ],
    )
    def test_widens_across_century_boundary(self, two_digit: str, expected: int):
        assert _asce7_edition_year(two_digit) == expected

    def test_old_edition_is_ordered_before_new(self):
        # The exact inversion the old int() comparison got wrong.
        assert _asce7_edition_year("98") < _asce7_edition_year("22")


# ---------------------------------------------------------------------------
# 2. Pre-2005 editions are now flagged (the core P2-1 gap)
# ---------------------------------------------------------------------------


class TestOldEditionsFlagged:
    @pytest.mark.parametrize("edition", ["88", "93", "95", "98", "02"])
    def test_pre_2005_edition_flagged_as_stale(self, edition: str):
        content = f"Design wind loads per ASCE 7-{edition} for all rooftop equipment."
        alerts = _asce7_alerts(content)
        assert len(alerts) == 1, f"7-{edition} should be flagged stale vs 7-22"
        assert alerts[0]["found_edition"] == f"7-{edition}"
        assert alerts[0]["expected_edition"] == CALIFORNIA_2025.asce7

    @pytest.mark.parametrize("edition", ["05", "10", "16"])
    def test_already_recognized_editions_still_flagged(self, edition: str):
        # Regression guard: the editions the detector handled before the fix
        # must keep flagging (behavior unchanged for them).
        content = f"Comply with ASCE 7-{edition} seismic provisions."
        alerts = _asce7_alerts(content)
        assert len(alerts) == 1
        assert alerts[0]["found_edition"] == f"7-{edition}"


# ---------------------------------------------------------------------------
# 3. Current / non-edition / suppressed cases are NOT flagged
# ---------------------------------------------------------------------------


class TestNotFlagged:
    def test_current_edition_not_flagged(self):
        content = "Comply with ASCE 7-22 for wind and seismic design."
        assert _asce7_alerts(content) == []

    def test_stray_two_digit_number_not_flagged(self):
        # "7-42" matches the regex shape but is not a real edition; it must be
        # ignored rather than flagged as a stale edition.
        content = "Reference detail ASCE 7-42 in the structural notes."
        assert _asce7_alerts(content) == []

    def test_descriptive_old_edition_suppressed(self):
        # Same historical-context suppression as stale code cycles: an author
        # describing a superseded edition is not stating a requirement.
        content = "Previously per ASCE 7-98; now comply with the current edition."
        assert _asce7_alerts(content) == []

    def test_active_old_requirement_still_flagged_despite_nearby_text(self):
        # An active requirement for an old edition must still flag.
        content = "All structural calculations shall use ASCE 7-95 load combinations."
        alerts = _asce7_alerts(content)
        assert len(alerts) == 1
        assert alerts[0]["found_edition"] == "7-95"
