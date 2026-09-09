"""Pins for the provenance-only edition-authority correction (plan step 2c).

The defect (plan section 2.1) is an **inversion**: the verifier was told to
"treat the pinned edition as authoritative for the cycle" on modules where
every pin carries ``UNVERIFIED`` provenance, so a *correct* finding deferring
to what a jurisdiction actually adopted could be disputed on the strength of a
marked guess. A false positive gets human review; a silently discarded true
positive does not.

Three surfaces move together, and the coupling is the point — plan section 5.2:
a ``<pre_detected>`` alert primes the review model, so a firing stale-cycle
detector plus an authority-corrected prompt would send contradictory signals
into the same request. A fourth change, the cache namespace, exists because the
question itself changed: a verdict rendered under the old wording must not
replay under the new one.

California is deliberately untouched (plan section 3.2: "Do not apply the DC
edition-authority rewrite to California as a side effect"), and that is pinned
here rather than left to the goldens alone.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.input.preprocessor import (
    detect_stale_code_cycle_references,
    preprocess_spec,
)
from src.modules.registry import AVAILABLE_MODULES, get_module
from src.verification.verification_cache import (
    BASIS_POLICY_NAMESPACE,
    make_cache_key,
)
from src.verification.verifier import _pinned_standards_lines

_STALE_EXCERPT = (
    "Sprinkler systems shall comply with the 2021 IBC and the 2021 Virginia "
    "USBC. Seismic bracing shall follow ASCE 7-16."
)


def _finding():
    return SimpleNamespace(
        codeReference="NFPA 13 8.3.1",
        actionType="EDIT",
        issue="Edition question",
        fileName="21 13 13.docx",
    )


def _block(module_id: str) -> str:
    module = get_module(module_id)
    return "\n".join(_pinned_standards_lines(module.cycle, module=module))


class TestVerifierPromptAuthority:
    def test_the_authoritative_clause_is_gone_from_a_location_aware_module(self):
        assert "authoritative for the cycle" not in _block("datacenter_fire")

    def test_pins_are_framed_as_assumptions_not_adoptions(self):
        text = _block("datacenter_fire")
        assert "NOT established adoptions" in text
        assert "Do NOT treat a listed edition as authoritative" in text

    def test_it_does_not_invert_into_favouring_the_newest_edition(self):
        """The same defect facing the other way.

        A verifier that reflexively prefers the newest publication is as wrong
        as one that reflexively prefers the pin — an older edition a
        jurisdiction actually adopted governs over a newer one it has not.
        """
        text = _block("datacenter_fire")
        assert "newer published edition as automatically correct" in text
        assert "governs" in text

    def test_a_differing_citation_is_not_declared_an_error_by_itself(self):
        assert "NOT by itself" in _block("datacenter_fire")

    def test_unverified_is_named_as_the_honest_outcome(self):
        assert "return UNVERIFIED" in _block("datacenter_fire")

    def test_unconfirmed_pins_are_marked_individually(self):
        module = get_module("datacenter_fire")
        text = _block("datacenter_fire")
        unconfirmed = [
            s for s in module.cycle.standards
            if s.edition and str(s.source or "").upper().startswith("UNVERIFIED")
        ]
        assert unconfirmed
        for pin in unconfirmed:
            assert f"- {pin.name}: {pin.edition_phrase}  [provenance: not confirmed]" in text

    @pytest.mark.parametrize(
        "module_id",
        sorted(m for m, mod in AVAILABLE_MODULES.items() if mod.project_profile_enabled),
    )
    def test_every_location_aware_module_gets_the_correction(self, module_id):
        assert "authoritative for the cycle" not in _block(module_id)

    def test_california_keeps_the_original_wording(self):
        """Plan section 3.2 — not a side effect of the DC rewrite."""
        text = _block("california_k12_mep")
        assert "pinned edition as authoritative for the cycle" in text
        assert "NOT established adoptions" not in text

    def test_the_module_is_resolved_when_the_caller_omits_it(self):
        """A caller that forgets the module must not silently get the old text."""
        module = get_module("datacenter_fire")
        assert "authoritative for the cycle" not in "\n".join(
            _pinned_standards_lines(module.cycle)
        )


class TestPreScreenSuppression:
    def test_a_location_aware_run_surfaces_no_stale_cycle_alert(self):
        result = preprocess_spec(
            _STALE_EXCERPT, "21 13 13.docx", cycle=get_module("datacenter_fire").cycle
        )
        assert result.code_cycle_alerts == []

    def test_the_raw_detector_still_fires_so_the_check_is_not_vacuous(self):
        """Suppression must be the pipeline's choice, not an inert detector.

        If the excerpt could not trigger the detector at all, the test above
        would pass for the wrong reason and would keep passing if suppression
        were removed.
        """
        raw = detect_stale_code_cycle_references(
            _STALE_EXCERPT, "21 13 13.docx", get_module("datacenter_fire").cycle
        )
        assert raw, "excerpt cannot trigger the detector; the suppression test is vacuous"

    def test_california_still_flags_a_stale_cycle(self):
        result = preprocess_spec(
            "Comply with 2019 CBC.", "23 05 00.docx",
            cycle=get_module("california_k12_mep").cycle,
        )
        assert result.code_cycle_alerts

    @pytest.mark.parametrize(
        "module_id",
        sorted(m for m, mod in AVAILABLE_MODULES.items() if mod.project_profile_enabled),
    )
    def test_suppression_covers_every_location_aware_module(self, module_id):
        result = preprocess_spec(
            _STALE_EXCERPT, "21 13 13.docx", cycle=get_module(module_id).cycle
        )
        assert result.code_cycle_alerts == []


class TestCacheNamespace:
    """The question changed, so the answers must not be reused."""

    def test_a_location_aware_key_carries_the_namespace(self):
        key = make_cache_key(_finding(), cycle=get_module("datacenter_fire").cycle)
        assert key.endswith(f"|{BASIS_POLICY_NAMESPACE}")

    def test_the_california_key_is_unchanged(self):
        """Five segments, no namespace — existing CA entries stay warm."""
        key = make_cache_key(_finding(), cycle=get_module("california_k12_mep").cycle)
        assert BASIS_POLICY_NAMESPACE not in key
        assert len(key.split("|")) == 5

    def test_a_pre_correction_key_cannot_collide_with_a_corrected_one(self):
        """The verdicts under the old wording are a different question."""
        cycle = get_module("datacenter_fire").cycle
        corrected = make_cache_key(_finding(), cycle=cycle)
        legacy = corrected.rsplit("|", 1)[0]
        assert corrected != legacy

    def test_the_namespace_survives_alongside_a_jurisdiction_fingerprint(self):
        cycle = get_module("datacenter_fire").cycle
        key = make_cache_key(_finding(), cycle=cycle, jurisdiction_fingerprint="abc123")
        assert "abc123" in key
        assert key.endswith(f"|{BASIS_POLICY_NAMESPACE}")

    def test_key_construction_never_raises_on_an_unresolvable_cycle(self):
        """A cache key must not be the thing that breaks a run."""
        bogus = SimpleNamespace(label="not-a-registered-cycle", standards=())
        assert make_cache_key(_finding(), cycle=bogus)

    @pytest.mark.parametrize(
        "module_id",
        sorted(m for m, mod in AVAILABLE_MODULES.items() if mod.project_profile_enabled),
    )
    def test_every_corrected_module_is_namespaced(self, module_id):
        key = make_cache_key(_finding(), cycle=get_module(module_id).cycle)
        assert key.endswith(f"|{BASIS_POLICY_NAMESPACE}")


class TestSurfacesAgree:
    """Plan section 5.2: these must never disagree within one request."""

    def test_a_suppressed_detector_pairs_with_a_corrected_prompt(self):
        for module_id, module in sorted(AVAILABLE_MODULES.items()):
            corrected = "authoritative for the cycle" not in _block(module_id)
            suppressed = (
                preprocess_spec(
                    _STALE_EXCERPT, "21 13 13.docx", cycle=module.cycle
                ).code_cycle_alerts
                == []
            )
            assert corrected == suppressed, (
                f"{module_id}: prompt and pre-screen disagree — a firing detector "
                "with a corrected prompt sends contradictory signals into one request"
            )
