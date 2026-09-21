"""Trust-model gating: which findings a run may write into a document.

Spec Critic classifies but does not gate — ``classify_edit_action`` is a bare
"is there a proposal?" check, and its docstring hands the gating decision to a
downstream applier. These tests pin the gate that decision produced, and in
particular the one rule that no policy relaxes.
"""
from __future__ import annotations

import pytest

from applier.models import EditEntry
from applier.policy import (
    COUNTERSIGNALLED,
    NEVER_AUTOMATIC,
    SUPERSEDED,
    Policy,
    PolicyConfig,
    parse_force_statuses,
)
from src.output.report_status import ReportStatus


def entry(status: str, *, confidence: float = 0.9, finding_id: str = "rf-1") -> EditEntry:
    return EditEntry(
        finding_id=finding_id,
        file_name="215000.docx",
        action_type="EDIT",
        existing_text="old",
        replacement_text="new",
        anchor_text=None,
        insert_position=None,
        target_element_id="p1",
        evidence_element_id="p1",
        edit_confidence=confidence,
        report_status=status,
        verification_verdict=None,
        severity="HIGH",
        section="21 13 13",
        issue="…",
        code_reference=None,
        has_per_file_original=True,
    )


ALL_STATUSES = [member.value for member in ReportStatus]


class TestPolicyLadder:
    def test_strict_admits_only_the_claim_confirmed_as_stated(self):
        config = PolicyConfig(policy=Policy.STRICT)
        allowed = {s for s in ALL_STATUSES if config.decide(entry(s)).allowed}
        assert allowed == {ReportStatus.VERIFIED_SUPPORTED.value}

    def test_conservative_adds_locally_classified(self):
        config = PolicyConfig(policy=Policy.CONSERVATIVE)
        allowed = {s for s in ALL_STATUSES if config.decide(entry(s)).allowed}
        assert allowed == {
            ReportStatus.VERIFIED_SUPPORTED.value,
            ReportStatus.LOCALLY_CLASSIFIED.value,
        }

    def test_all_adds_the_unsettled_but_never_the_never_automatic(self):
        config = PolicyConfig(policy=Policy.ALL)
        allowed = {s for s in ALL_STATUSES if config.decide(entry(s)).allowed}
        assert allowed == set(ALL_STATUSES) - NEVER_AUTOMATIC

    def test_the_ladder_only_widens(self):
        def allowed(policy):
            config = PolicyConfig(policy=policy)
            return {s for s in ALL_STATUSES if config.decide(entry(s)).allowed}

        strict, conservative, everything = (
            allowed(Policy.STRICT),
            allowed(Policy.CONSERVATIVE),
            allowed(Policy.ALL),
        )
        assert strict < conservative < everything

    def test_conservative_is_the_default(self):
        assert PolicyConfig().policy is Policy.CONSERVATIVE


class TestCorrectedFindingsAreNeverAutomatic:
    """The sidecar carries the verdict but NOT ``VerificationResult.correction``,
    and nothing regenerates the proposal after verification — so a
    VERIFIED_CONTRADICTED entry holds the review model's *pre-correction*
    wording while its verdict says that wording was refuted.

    The calibration fixture ``tp_dc_corrected_misattributed_amendment`` is the
    worked example, and it is checked here against the real file rather than
    described, so this rule cannot drift into folklore."""

    def test_superseded_is_exactly_verified_contradicted(self):
        assert SUPERSEDED == {ReportStatus.VERIFIED_CONTRADICTED.value}

    @pytest.mark.parametrize("policy", list(Policy))
    def test_no_policy_applies_a_corrected_finding(self, policy):
        decision = PolicyConfig(policy=policy).decide(
            entry(ReportStatus.VERIFIED_CONTRADICTED.value)
        )
        assert not decision.allowed
        assert "pre-correction proposal" in decision.reason

    def test_the_fixture_that_motivates_the_rule_still_says_so(self):
        import json
        from pathlib import Path as _Path

        fixture = json.loads(
            (
                _Path(__file__).resolve().parent.parent
                / "evals/calibration/fixtures/tp_dc_corrected_misattributed_amendment.json"
            ).read_text(encoding="utf-8")
        )
        truth = fixture["ground_truth"]
        assert truth["correct_verdict"] == "CORRECTED"
        assert truth["expected_status"] == ReportStatus.VERIFIED_CONTRADICTED.value
        # The verifier's correction says to leave the clause alone ...
        assert "no provincial amendment" in truth["correct_correction_text"]
        # ... while the proposal the sidecar would carry still rewrites it.
        assert "provincial fire-code amendment" in fixture["finding"]["replacementText"]

    def test_a_reviewer_can_still_force_it_after_reading_the_correction(self):
        config = PolicyConfig(
            force_statuses=frozenset({ReportStatus.VERIFIED_CONTRADICTED.value})
        )
        assert config.decide(entry(ReportStatus.VERIFIED_CONTRADICTED.value)).allowed


class TestCountersignalledFindingsAreNeverAutomatic:
    """DISPUTED and VERIFIED_CONTESTED are the two verdicts that tell a
    reviewer *not* to act. Writing them in — even as a tracked change —
    inverts the signal the trust model exists to send."""

    def test_the_excluded_set_is_exactly_those_two(self):
        assert COUNTERSIGNALLED == {
            ReportStatus.DISPUTED.value,
            ReportStatus.VERIFIED_CONTESTED.value,
        }

    def test_never_automatic_is_the_union(self):
        assert NEVER_AUTOMATIC == COUNTERSIGNALLED | SUPERSEDED

    @pytest.mark.parametrize("status", sorted(NEVER_AUTOMATIC))
    @pytest.mark.parametrize("policy", list(Policy))
    def test_no_policy_admits_them(self, status, policy):
        assert not PolicyConfig(policy=policy).decide(entry(status)).allowed

    @pytest.mark.parametrize("status", sorted(NEVER_AUTOMATIC))
    def test_the_refusal_names_the_override(self, status):
        reason = PolicyConfig().decide(entry(status)).reason
        assert "--force-status" in reason
        assert status in reason

    @pytest.mark.parametrize("status", sorted(NEVER_AUTOMATIC))
    def test_force_status_is_the_only_door(self, status):
        config = PolicyConfig(force_statuses=frozenset({status}))
        decision = config.decide(entry(status))
        assert decision.allowed
        assert "forced" in decision.reason

    def test_forcing_one_does_not_force_the_other(self):
        config = PolicyConfig(
            force_statuses=frozenset({ReportStatus.DISPUTED.value})
        )
        assert config.decide(entry(ReportStatus.DISPUTED.value)).allowed
        assert not config.decide(
            entry(ReportStatus.VERIFIED_CONTESTED.value)
        ).allowed


class TestConfidenceThreshold:
    def test_off_by_default(self):
        assert PolicyConfig().min_edit_confidence == 0.0
        low = entry(ReportStatus.VERIFIED_SUPPORTED.value, confidence=0.01)
        assert PolicyConfig().decide(low).allowed

    def test_below_the_threshold_is_withheld(self):
        config = PolicyConfig(min_edit_confidence=0.75)
        assert not config.decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, confidence=0.5)
        ).allowed
        assert config.decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, confidence=0.75)
        ).allowed

    def test_the_threshold_still_applies_to_a_forced_status(self):
        """Forcing a status overrides the verdict gate, not the numeric one —
        two independent decisions, and --force-status only speaks to one."""
        config = PolicyConfig(
            force_statuses=frozenset({ReportStatus.DISPUTED.value}),
            min_edit_confidence=0.9,
        )
        assert not config.decide(
            entry(ReportStatus.DISPUTED.value, confidence=0.4)
        ).allowed

    def test_the_reason_names_the_numbers(self):
        config = PolicyConfig(min_edit_confidence=0.8)
        reason = config.decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, confidence=0.3)
        ).reason
        assert "0.30" in reason and "0.80" in reason


class TestFindingIdFilter:
    def test_only_restricts_to_the_named_ids(self):
        config = PolicyConfig(only_finding_ids=frozenset({"rf-keep"}))
        assert config.decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, finding_id="rf-keep")
        ).allowed
        assert not config.decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, finding_id="rf-other")
        ).allowed

    def test_empty_filter_restricts_nothing(self):
        assert PolicyConfig().decide(
            entry(ReportStatus.VERIFIED_SUPPORTED.value, finding_id="anything")
        ).allowed


class TestForceStatusParsing:
    def test_known_statuses_normalize(self):
        assert parse_force_statuses(["disputed", " VERIFIED_CONTESTED "]) == {
            ReportStatus.DISPUTED.value,
            ReportStatus.VERIFIED_CONTESTED.value,
        }

    def test_an_unknown_status_raises_rather_than_silently_forcing_nothing(self):
        with pytest.raises(ValueError) as excinfo:
            parse_force_statuses(["DISPUTEED"])
        assert "Unknown report status" in str(excinfo.value)

    def test_none_is_empty(self):
        assert parse_force_statuses(None) == frozenset()
        assert parse_force_statuses([]) == frozenset()

    def test_every_real_status_is_accepted(self):
        assert parse_force_statuses(ALL_STATUSES) == set(ALL_STATUSES)


class TestUnknownStatusIsNotSilentlyAdmitted:
    @pytest.mark.parametrize("policy", list(Policy))
    def test_an_empty_or_foreign_status_is_withheld(self, policy):
        config = PolicyConfig(policy=policy)
        assert not config.decide(entry("")).allowed
        assert not config.decide(entry("SOMETHING_NEW")).allowed
