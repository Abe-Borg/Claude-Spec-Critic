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
    _asce7_edition_key,
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


# ---------------------------------------------------------------------------
# 4. Designation syntax (plan WP-04C, chunk S03)
# ---------------------------------------------------------------------------


class TestDesignationSyntax:
    """ASCE has published the standard as ``ASCE 7``, ``SEI/ASCE 7`` and
    ``ASCE/SEI 7``; specs add the word Standard, Word turns the hyphen into a
    dash, and some write the edition as a four-digit year. Each form is the
    same edition to the detector."""

    @pytest.mark.parametrize(
        "designation, found",
        [
            ("ASCE/SEI 7-16", "7-16"),
            ("ASCE / SEI 7-16", "7-16"),
            ("SEI/ASCE 7-02", "7-02"),
            ("ASCE Standard 7-16", "7-16"),
            ("ASCE/SEI Standard 7-16", "7-16"),
            ("ASCE 7\u201316", "7-16"),  # en dash
            ("ASCE 7\u201416", "7-16"),  # em dash
            ("ASCE 7\u201116", "7-16"),  # non-breaking hyphen
            ("ASCE 7\u221216", "7-16"),  # minus sign
            ("ASCE\u00a07-16", "7-16"),  # non-breaking space
            ("ASCE 7-2016", "7-16"),
            ("ASCE 7-1998", "7-98"),
            ("ASCE/SEI 7\u20132010", "7-10"),
            ("ASCE7-16", "7-16"),
            ("ASCE-7-16", "7-16"),
        ],
    )
    def test_each_form_is_recognized_and_normalized(self, designation, found):
        (alert,) = _asce7_alerts(f"Design loads per {designation}.")
        assert alert["found_edition"] == found
        assert alert["type"] == f"Stale ASCE 7 edition ({found} vs selected 7-22)"
        # The alert quotes the document's own text, not the normalized form.
        assert alert["match"] == designation

    @pytest.mark.parametrize(
        "designation",
        [
            "ASCE 7-2022",  # the cycle's own edition, four-digit
            "ASCE/SEI 7\u201322",  # the cycle's own edition, en dash
            "ASCE 7-1916",  # right digits, wrong century: no such edition
            "ASCE 7-2042",  # not a published edition
            "ASCE 7-201",  # neither two nor four digits
            "ASCE 7-16a",  # not a designation boundary
        ],
    )
    def test_current_and_non_editions_are_not_flagged(self, designation):
        assert _asce7_alerts(f"Design loads per {designation}.") == []

    @pytest.mark.parametrize(
        "captured, key",
        [("16", "16"), ("98", "98"), ("2016", "16"), ("1998", "98"), ("2002", "02")],
    )
    def test_edition_keys(self, captured, key):
        assert _asce7_edition_key(captured) == key

    @pytest.mark.parametrize("captured", ["1916", "2098", "201", "", "7-16"])
    def test_no_key_for_a_non_edition(self, captured):
        assert _asce7_edition_key(captured) is None

    def test_suppression_applies_to_every_form(self):
        assert _asce7_alerts("ASCE/SEI 7\u201310 is no longer used for new work.") == []
        assert _asce7_alerts("The prior edition, ASCE 7-2010, required less.") == []
