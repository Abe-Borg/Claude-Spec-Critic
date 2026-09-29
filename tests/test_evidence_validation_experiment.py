"""Plan EX-04, part A: evidence validation in observation mode.

The validator (``src/verification/evidence_validation.py``) reads each
conclusive verdict's quoted evidence against the claim and records whether it
agrees. These tests pin, in order:

* the switch (``SPEC_CRITIC_EVIDENCE_VALIDATION``), including that there is no
  enforcing value;
* the reference reader both experiments share;
* each check, in both directions (CONFIRMED / CORRECTED support a statement,
  DISPUTED contradicts one);
* that **lexical overlap decides nothing**;
* that observation changes nothing the app decides with: the verdict, its
  grounding, its sources, cache eligibility, cache keys, report status;
* byte-identical behavior with the switch off, through the real pipeline
  entry point, diagnostics, and the cache;
* the diagnostics rollup, the trace snapshot, and the constructed evidence set
  and its recorded held-out result.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest

import src.orchestration.pipeline as pipeline
from src.core import api_config
from src.core.code_cycles import DEFAULT_CYCLE
from src.orchestration.diagnostics import DiagnosticsReport, record_verification_findings
from src.output.report_status import classify_status
from src.review.reviewer import Finding
from src.verification import evidence_validation as ev
from src.verification import reference_parsing as rp
from src.verification.verification_cache import (
    _SKIPPED_FIELDS,
    VerificationCache,
    cache_ineligibility_reason,
    make_cache_key,
)
from src.verification.verifier import OUTCOME_VERDICT, VerificationResult

from evals import evidence_validation as harness
from evals import evidence_validation_dataset as ds


NFPA = "https://www.nfpa.org/codes-and-standards/nfpa-13"


def _finding(**overrides) -> Finding:
    fields = dict(
        severity="HIGH",
        fileName="21 13 13 - Wet-Pipe Sprinkler Systems.docx",
        section="3.02",
        issue="The spec allows 12 in. below deflectors; NFPA 13-2022 requires 18 in.",
        actionType="EDIT",
        existingText="12 in.",
        replacementText="Maintain 18 in. below sprinkler deflectors.",
        codeReference="NFPA 13-2022 §8.15.1",
        confidence=0.8,
    )
    fields.update(overrides)
    finding = Finding(**fields)
    finding.finding_id = "rf-000000000001"
    return finding


def _result(**overrides) -> VerificationResult:
    fields = dict(
        verdict="CONFIRMED",
        explanation="Checked.",
        sources=[NFPA],
        accepted_sources=[NFPA],
        searched_sources=[NFPA],
        grounded=True,
        source_quote="NFPA 13 (2022): the clearance below the deflector shall be 18 in. or greater.",
        cache_status="miss",
        verification_mode="standard_reasoning",
        outcome=OUTCOME_VERDICT,
        native_citations=[],
    )
    fields.update(overrides)
    return VerificationResult(**fields)


def _cite(url: str, text: str, **overrides) -> dict:
    record = {
        "type": "web_search_result_location",
        "recognized": True,
        "tool": "web_search",
        "url": url,
        "title": "",
        "cited_text": text,
        "resolution": "direct",
        "retrieved": True,
        "verdict_cites_source": True,
    }
    record.update(overrides)
    return record


def _status(assessment: dict, check: str) -> str:
    return assessment["checks"][check]["status"]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestSwitch:
    @pytest.mark.parametrize("value", ["observe", "OBSERVE", " on ", "1", "true", "yes"])
    def test_on_values_mean_observe(self, monkeypatch, value):
        monkeypatch.setenv("SPEC_CRITIC_EVIDENCE_VALIDATION", value)
        assert api_config.evidence_validation_mode() == "observe"

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_off_values(self, monkeypatch, value):
        monkeypatch.setenv("SPEC_CRITIC_EVIDENCE_VALIDATION", value)
        assert api_config.evidence_validation_mode() is None

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_EVIDENCE_VALIDATION", raising=False)
        assert api_config.evidence_validation_mode() is None

    @pytest.mark.parametrize("value", ["enforce", "strict", "2"])
    def test_there_is_no_enforcing_value(self, monkeypatch, caplog, value):
        monkeypatch.setattr(api_config, "_WARNED_EX04_VALUES", set())
        monkeypatch.setenv("SPEC_CRITIC_EVIDENCE_VALIDATION", value)
        with caplog.at_level(logging.WARNING, logger="src.core.api_config"):
            assert api_config.evidence_validation_mode() is None
            assert api_config.evidence_validation_mode() is None
        warnings = [r for r in caplog.records if "SPEC_CRITIC_EVIDENCE_VALIDATION" in r.getMessage()]
        assert len(warnings) == 1  # warned once, not per call


# ---------------------------------------------------------------------------
# Reference reading
# ---------------------------------------------------------------------------


class TestReferenceParsing:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("NFPA 13-2022 §8.15.1", {"NFPA 13": {2022}}),
            ("NFPA 13 (2019)", {"NFPA 13": {2019}}),
            ("NFPA 13, 2022 Edition", {"NFPA 13": {2022}}),
            ("the 2022 edition of NFPA 13", {"NFPA 13": {2022}}),
            ("2021 IBC Section 903.2", {"IBC": {2021}}),
            ("ASCE 7-16", {"ASCE 7": {2016}}),
            ("ASCE/SEI 7-22", {"ASCE 7": {2022}}),
            ("ASTM E119-20", {"ASTM E119": {2020}}),
            ("ASHRAE 62.1-2019", {"ASHRAE 62.1": {2019}}),
            ("NEC 2023", {"NFPA 70": {2023}}),
            ("NFPA 13 requires listed hangers", {}),
        ],
    )
    def test_strict_editions(self, text, expected):
        assert rp.editions(text) == expected

    def test_dash_is_never_part_of_the_number(self):
        assert rp.designators("NFPA 13-2022") == ["NFPA 13"]
        assert rp.designators("FM Global Data Sheet 2-0") == ["FM DS 2-0"]

    def test_loose_attaches_later_years_to_the_designator(self):
        text = "NFPA 72-2019 is stale; the current edition is 2022."
        assert rp.editions(text) == {"NFPA 72": {2019}}
        assert rp.editions(text, loose=True) == {"NFPA 72": {2019, 2022}}

    def test_nec_is_nfpa_70(self):
        assert rp.designators("NEC and the National Electrical Code") == ["NFPA 70"]

    def test_section_numbers_skip_quantities_and_designators(self):
        text = "Provide 0.10 gpm/ft2 and 2.5 in. per 8.15.1 and Section 903.2; ASHRAE 62.1 §6.2"
        assert rp.section_numbers(text) == ["8.15.1", "903.2", "6.2"]

    def test_normalize_reference_drops_editions_and_order(self):
        a = rp.normalize_reference("NFPA 13-2022 §8.15.1; IBC 2021")
        b = rp.normalize_reference("IBC §8.15.1 and NFPA 13")
        assert a == b == ("IBC", "NFPA 13", "§8.15.1")

    def test_unrecognized_reference_is_empty(self):
        assert rp.normalize_reference("Owner design standard, rev. B") == ()


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------


class TestQuantities:
    def test_conversions_to_base_units(self):
        q = ev.quantities('18 in., 1-1/2", 4 ft, 25 mm, 690 kPa, 20 °C, 2-hour, 15%, 500 gpm, 0.10 gpm/ft2')
        assert q["length"] == pytest.approx([18.0, 1.5, 48.0, 25 / 25.4])
        assert q["pressure"] == pytest.approx([690 * 0.145038])
        assert q["temperature_f"] == pytest.approx([68.0])
        assert q["time"] == pytest.approx([120.0])
        assert q["percent"] == [15.0]
        assert q["flow"] == [500.0]
        assert q["density"] == [0.1]

    def test_water_gauge_is_not_a_length(self):
        assert ev.quantities("2 in. w.g.") == {"pressure_wg": [2.0]}

    def test_a_preposition_is_not_a_unit(self):
        assert ev.quantities("4 in accordance with the standard") == {}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


class TestNotApplicable:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"verdict": "UNVERIFIED"},
            {"verdict": "UNVERIFIED", "verification_failed": True},
            {"verdict": "CONFIRMED", "verification_failed": True},
            {"verdict": "UNVERIFIED", "cache_status": "local_skip", "verification_mode": "local_skip"},
        ],
    )
    def test_no_conclusive_verdict_means_nothing_to_validate(self, overrides):
        a = ev.assess_evidence(_finding(), _result(**overrides))
        assert a["assessment"] == ev.ASSESSMENT_NOT_APPLICABLE
        assert a["agrees_with_verdict"] is None and a["checks"] == {}


class TestNumbers:
    def test_changed_number_is_a_concern_for_a_supporting_verdict(self):
        a = ev.assess_evidence(_finding(), _result(source_quote="The clearance shall be 36 in. or greater."))
        assert _status(a, ev.CHECK_NUMBERS) == ev.STATUS_CONCERN
        assert a["assessment"] == ev.ASSESSMENT_CONCERNS and a["agrees_with_verdict"] is False

    def test_changed_unit_is_a_concern(self):
        f = _finding(issue="Hose valves within 4 in. of the landing.", replacementText="Within 4 in.")
        a = ev.assess_evidence(f, _result(source_quote="Hose valves shall be within 4 ft of the landing."))
        assert _status(a, ev.CHECK_NUMBERS) == ev.STATUS_CONCERN

    def test_equal_after_conversion_is_consistent(self):
        f = _finding(issue="Provide 1 ft of clearance.", replacementText="Provide 1 ft of clearance.")
        a = ev.assess_evidence(f, _result(source_quote="A clearance of 12 in. shall be provided."))
        assert _status(a, ev.CHECK_NUMBERS) == ev.STATUS_CONSISTENT

    def test_disputed_direction_is_reversed(self):
        f = _finding(issue="NFPA 13 requires 4 ft below deflectors.", replacementText="")
        differs = ev.assess_evidence(f, _result(verdict="DISPUTED", source_quote="The clearance shall be 18 in."))
        same = ev.assess_evidence(f, _result(verdict="DISPUTED", source_quote="The clearance shall be 4 ft."))
        assert _status(differs, ev.CHECK_NUMBERS) == ev.STATUS_CONSISTENT
        assert _status(same, ev.CHECK_NUMBERS) == ev.STATUS_UNKNOWN

    def test_corrected_compares_the_correction(self):
        f = _finding(issue="NFPA 13 requires 24 in.", replacementText="")
        ok = ev.assess_evidence(
            f, _result(verdict="CORRECTED", correction="It is 36 in.", source_quote="It shall be 36 in.")
        )
        bad = ev.assess_evidence(
            f, _result(verdict="CORRECTED", correction="It is 30 in.", source_quote="It shall be 36 in.")
        )
        assert ok["target"] == "correction"
        assert _status(ok, ev.CHECK_NUMBERS) == ev.STATUS_CONSISTENT
        assert _status(bad, ev.CHECK_NUMBERS) == ev.STATUS_CONCERN


class TestNegationAndExceptions:
    def test_reversed_negation_is_a_concern(self):
        f = _finding(issue="Cast iron fittings are required.", replacementText="Cast iron fittings shall be used on risers.")
        a = ev.assess_evidence(f, _result(source_quote="Cast iron fittings shall not be used on risers."))
        assert _status(a, ev.CHECK_NEGATION) == ev.STATUS_CONCERN

    def test_a_comparative_bound_is_not_a_negation(self):
        f = _finding(issue="Pressure 175 psi minimum.", replacementText="The pump shall be rated 175 psi minimum.")
        a = ev.assess_evidence(f, _result(source_quote="The rated pressure shall not be less than 175 psi."))
        assert _status(a, ev.CHECK_NEGATION) == ev.STATUS_CONSISTENT

    def test_imperative_spec_language_is_a_requirement(self):
        f = _finding(issue="Inspect quarterly.", replacementText="Inspect waterflow devices quarterly.")
        a = ev.assess_evidence(f, _result(source_quote="Waterflow devices shall be inspected quarterly."))
        assert _status(a, ev.CHECK_NEGATION) == ev.STATUS_CONSISTENT

    def test_a_negated_exception_does_not_reverse_the_main_clause(self):
        f = _finding(issue="Sprinklers in the pump room.", replacementText="Provide sprinklers in the pump room.")
        quote = "Sprinklers shall be installed throughout, except that sprinklers shall not be required in electrical rooms."
        a = ev.assess_evidence(f, _result(source_quote=quote))
        assert _status(a, ev.CHECK_NEGATION) == ev.STATUS_CONSISTENT
        assert _status(a, ev.CHECK_EXCEPTION) == ev.STATUS_CONCERN

    def test_an_exception_the_claim_accounts_for_is_consistent(self):
        f = _finding(
            issue="Sprinklers are required; the dry-type exception does not apply here.",
            replacementText="Provide sprinklers.",
        )
        a = ev.assess_evidence(f, _result(source_quote="Sprinklers shall be provided, except in dry-type rooms."))
        assert _status(a, ev.CHECK_EXCEPTION) == ev.STATUS_CONSISTENT


class TestEditionAuthorityIdentity:
    def test_wrong_edition_is_a_concern(self):
        a = ev.assess_evidence(_finding(), _result(source_quote="NFPA 13 (2016 edition): clearance 18 in."))
        assert _status(a, ev.CHECK_EDITION) == ev.STATUS_CONCERN

    def test_a_stale_edition_claim_names_the_current_edition_too(self):
        # "... the current edition is 2022" attaches 2022 to NFPA 72 on the
        # claim side (loose reading), so a source naming 2022 agrees with it.
        f = _finding(
            issue="NFPA 72-2019 is stale; the current edition is 2022.",
            replacementText="",
            codeReference="NFPA 72",
        )
        a = ev.assess_evidence(f, _result(source_quote="NFPA 72 (2022 edition) is the current edition."))
        assert _status(a, ev.CHECK_EDITION) == ev.STATUS_CONSISTENT

    def test_edition_in_an_accepted_citation_title_counts_as_evidence(self):
        cite = _cite(NFPA, "The clearance shall be 18 in.", title="NFPA 13, 2019 Edition")
        a = ev.assess_evidence(
            _finding(), _result(source_quote="The clearance shall be 18 in.", native_citations=[cite])
        )
        assert _status(a, ev.CHECK_EDITION) == ev.STATUS_CONCERN

    def test_only_low_authority_hosts_is_a_concern(self):
        a = ev.assess_evidence(
            _finding(),
            _result(accepted_sources=["https://www.reddit.com/r/x", "https://en.wikipedia.org/wiki/x"],
                    sources=["https://www.reddit.com/r/x"]),
        )
        assert _status(a, ev.CHECK_AUTHORITY) == ev.STATUS_CONCERN

    @pytest.mark.parametrize(
        "url, cls",
        [
            ("https://www.nfpa.org/x", ev.AUTHORITY_PRIMARY),
            ("https://codes.iccsafe.org/x", ev.AUTHORITY_PRIMARY),
            ("https://www.dgs.ca.gov/x", ev.AUTHORITY_PRIMARY),
            ("https://www.buildings.state.va.us/x", ev.AUTHORITY_PRIMARY),
            ("https://en.wikipedia.org/wiki/x", ev.AUTHORITY_LOW),
            ("https://www.example-vendor.com/x", ev.AUTHORITY_UNCLASSIFIED),
            ("not a url", ev.AUTHORITY_UNCLASSIFIED),
        ],
    )
    def test_authority_classes(self, url, cls):
        assert ev.authority_class(url) == cls

    def test_attributed_to_the_accepted_source_is_consistent(self):
        quote = "The clearance below the deflector shall be 18 in. or greater."
        a = ev.assess_evidence(_finding(), _result(source_quote=quote, native_citations=[_cite(NFPA, quote)]))
        assert _status(a, ev.CHECK_SOURCE_IDENTITY) == ev.STATUS_CONSISTENT

    def test_attributed_to_another_source_is_a_concern(self):
        quote = "The clearance below the deflector shall be 18 in. or greater."
        cite = _cite("https://vendor.example.com/p", quote, verdict_cites_source=False)
        a = ev.assess_evidence(_finding(), _result(source_quote=quote, native_citations=[cite]))
        assert _status(a, ev.CHECK_SOURCE_IDENTITY) == ev.STATUS_CONCERN

    def test_not_captured_is_unknown_never_a_concern(self):
        a = ev.assess_evidence(_finding(), _result(native_citations=None))
        assert _status(a, ev.CHECK_SOURCE_IDENTITY) == ev.STATUS_UNKNOWN

    def test_a_supporting_verdict_without_a_quote_is_a_concern(self):
        a = ev.assess_evidence(_finding(), _result(source_quote=""))
        assert _status(a, ev.CHECK_SOURCE_IDENTITY) == ev.STATUS_CONCERN


class TestOverallReading:
    def test_provenance_alone_never_makes_a_verdict_consistent(self):
        # A vague passage on a standards publisher's site, attributed to it:
        # identity and authority agree, and nothing the passage says does.
        quote = "Sprinkler systems shall be designed in accordance with this standard."
        f = _finding(issue="NFPA 13 requires 0.20 gpm/ft2 over 2,500 sq ft.", replacementText="", codeReference="NFPA 13")
        a = ev.assess_evidence(f, _result(source_quote=quote, native_citations=[_cite(NFPA, quote)]))
        assert _status(a, ev.CHECK_AUTHORITY) == ev.STATUS_CONSISTENT
        assert _status(a, ev.CHECK_SOURCE_IDENTITY) == ev.STATUS_CONSISTENT
        assert a["assessment"] == ev.ASSESSMENT_INSUFFICIENT and a["agrees_with_verdict"] is None

    def test_quoted_support_is_topical_and_cannot_make_a_dispute_consistent(self):
        f = _finding(issue="NFPA 13 requires 18 in.", replacementText="", codeReference="NFPA 13 §8.15.1")
        a = ev.assess_evidence(
            f, _result(verdict="DISPUTED", source_quote="Per 8.15.1 the clearance shall be 18 in.")
        )
        assert _status(a, ev.CHECK_QUOTED_SUPPORT) == ev.STATUS_CONSISTENT
        assert a["assessment"] == ev.ASSESSMENT_INSUFFICIENT

    def test_record_carries_policy_version_and_provenance(self):
        a = ev.assess_evidence(_finding(), _result(cache_status="hit"))
        assert a["policy_version"] == ev.POLICY_VERSION == "ev1"
        assert a["mode"] == "observe"
        assert a["provenance"] == "cache_replay"
        assert a["finding_id"] == "rf-000000000001"
        json.dumps(a)  # JSON-safe


class TestLexicalOverlapDecidesNothing:
    """The plan's rule: lexical similarity is a diagnostic feature only."""

    def test_a_paraphrase_with_little_overlap_raises_no_concern(self):
        f = _finding(
            issue="Hangers for 1 in. steel branch lines must be no more than 12 ft apart.",
            replacementText="Space hangers at 12 ft maximum.",
            codeReference="NFPA 13",
        )
        a = ev.assess_evidence(f, _result(source_quote="The distance between hangers shall not exceed 12 ft."))
        assert a["features"]["lexical_overlap"] < 0.5
        assert a["assessment"] == ev.ASSESSMENT_CONSISTENT

    @pytest.mark.parametrize("forced", [0.0, 1.0])
    def test_forcing_the_overlap_changes_no_status(self, monkeypatch, forced):
        finding, result = _finding(), _result(source_quote="The clearance shall be 36 in.")
        baseline = ev.assess_evidence(finding, result)
        monkeypatch.setattr(ev, "lexical_overlap", lambda *_a, **_k: forced)
        forced_run = ev.assess_evidence(finding, result)
        assert forced_run["features"]["lexical_overlap"] == forced
        for key in ("assessment", "agrees_with_verdict", "concerns", "checks"):
            assert forced_run[key] == baseline[key]


# ---------------------------------------------------------------------------
# Observation changes nothing
# ---------------------------------------------------------------------------


def _decision_fields(result: VerificationResult) -> dict:
    fields = dataclasses.asdict(result)
    fields.pop("evidence_assessment")
    return fields


class TestObservationOnly:
    @pytest.mark.parametrize("quote", ["The clearance shall be 36 in.", "The clearance shall be 18 in. (2022)."])
    def test_annotation_changes_no_other_field(self, quote):
        finding = _finding()
        finding.verification = _result(source_quote=quote)
        before = _decision_fields(copy.deepcopy(finding.verification))
        status_before = classify_status(finding)
        reason_before = cache_ineligibility_reason(finding.verification)
        key_before = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        assert ev.annotate_evidence_assessments([finding]) == 1
        assert isinstance(finding.verification.evidence_assessment, dict)
        assert _decision_fields(finding.verification) == before
        assert classify_status(finding) == status_before
        assert cache_ineligibility_reason(finding.verification) == reason_before
        assert make_cache_key(finding, cycle=DEFAULT_CYCLE) == key_before

    def test_the_assessment_is_never_cached(self, tmp_path):
        assert "evidence_assessment" in _SKIPPED_FIELDS
        finding = _finding()
        result = _result()
        result.evidence_assessment = {"assessment": "concerns"}
        cache = VerificationCache()
        cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
        path = tmp_path / "cache.json"
        cache.save_to_disk(path)
        assert "evidence_assessment" not in path.read_text(encoding="utf-8")
        loaded = VerificationCache()
        loaded.load_from_disk(path)
        hit = loaded.get(finding, cycle=DEFAULT_CYCLE)
        assert hit is not None and hit.evidence_assessment is None

    def test_a_validator_error_is_recorded_not_raised(self, monkeypatch, caplog):
        finding = _finding()
        finding.verification = _result()

        def boom(*_a, **_k):
            raise RuntimeError("validator bug")

        monkeypatch.setattr(ev, "assess_evidence", boom)
        with caplog.at_level(logging.WARNING):
            assert ev.annotate_evidence_assessments([finding]) == 1
        assert finding.verification.evidence_assessment["assessment"] == ev.ASSESSMENT_ERROR
        assert "validator bug" in finding.verification.evidence_assessment["error"]
        assert finding.verification.verdict == "CONFIRMED"

    def test_a_generator_is_counted_and_logged(self):
        findings = [_finding(), _finding()]
        for f, quote in zip(findings, ["The clearance shall be 36 in.", "The clearance shall be 18 in."]):
            f.verification = _result(source_quote=quote)
        lines = []
        count = ev.annotate_evidence_assessments(
            (f for f in findings), log=lambda msg, **_k: lines.append(msg)
        )
        assert count == 2
        assert lines == ["Evidence validation (observation only): 2 assessed, 1 disagree with their verdict."]


# ---------------------------------------------------------------------------
# The pipeline entry point
# ---------------------------------------------------------------------------


def _fake_attempts(results_by_quote):
    def fake(findings, **_kwargs):
        for finding in findings:
            finding.verification = _result(source_quote=results_by_quote[finding.issue])

    return fake


class TestPipeline:
    def _findings(self):
        a = _finding(issue="NFPA 13-2022 requires 18 in. (a)")
        b = _finding(issue="NFPA 13-2022 requires 18 in. (b)")
        return [a, b], {a.issue: "The clearance shall be 36 in.", b.issue: "The clearance shall be 18 in."}

    def test_off_records_nothing(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_EVIDENCE_VALIDATION", raising=False)
        findings, quotes = self._findings()
        monkeypatch.setattr(pipeline, "_execute_verification_attempts", _fake_attempts(quotes))
        pipeline.verify_findings_for_run(findings, transport="realtime", cache=None)
        assert all(f.verification.evidence_assessment is None for f in findings)
        diag = DiagnosticsReport()
        record_verification_findings(diag, findings, phase="verification", transport="realtime")
        assert all("evidence_assessment" not in e.data for e in diag.events)
        summary = diag.summary()
        assert "evidence_validation" not in summary
        assert "Evidence check" not in diag.to_text()

    def test_on_assesses_every_verified_finding(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_EVIDENCE_VALIDATION", "observe")
        findings, quotes = self._findings()
        monkeypatch.setattr(pipeline, "_execute_verification_attempts", _fake_attempts(quotes))
        logs = []
        pipeline.verify_findings_for_run(
            findings, transport="realtime", cache=None, log=lambda m, **_k: logs.append(m)
        )
        readings = [f.verification.evidence_assessment["agrees_with_verdict"] for f in findings]
        assert readings == [False, True]
        assert any("Evidence validation (observation only)" in line for line in logs)

        diag = DiagnosticsReport()
        record_verification_findings(diag, findings, phase="verification", transport="realtime")
        event = diag.events[0].data["evidence_assessment"]
        assert event["concerns"] == ["numbers_units"]
        assert "36" in event["concern_details"]["numbers_units"]
        rollup = diag.summary()["evidence_validation"]
        assert rollup["assessed"] == 2 and rollup["disagreements"] == 1
        assert rollup["by_concern"] == {"numbers_units": 1}
        assert rollup["disagreement_findings"][0]["finding_id"] == "rf-000000000001"
        assert "Evidence check (observation only, ev1): 2 verdict(s) assessed, 1 disagree" in diag.to_text()

    def test_the_trace_snapshot_carries_the_assessment(self):
        from src.tracing.recorder import TraceRecorder

        finding = _finding()
        finding.verification = _result()
        ev.annotate_evidence_assessments([finding])
        # The recorder's own serializer, unbound: it reads nothing off self.
        snapshot = TraceRecorder._finding_to_dict(None, finding)
        assert snapshot["verification"]["evidence_assessment"]["policy_version"] == "ev1"


# ---------------------------------------------------------------------------
# The constructed evidence set and the recorded held-out result
# ---------------------------------------------------------------------------


class TestDataset:
    def test_the_set_is_sound(self):
        assert ds.validate_cases() == []

    def test_every_passage_is_marked_constructed(self):
        assert all(case.evidence_basis == ds.EVIDENCE_CONSTRUCTED for case in ds.CASES)

    def test_categories_the_plan_names_are_in_both_splits(self):
        for category in ("paraphrase", "omitted_exception", "reversed_negation", "changed_number", "wrong_edition"):
            splits = {case.split for case in ds.CASES if case.category == category}
            assert splits == {ds.SPLIT_TUNING, ds.SPLIT_HELD_OUT}, category

    def test_validation_catches_a_bad_case(self):
        bad = dataclasses.replace(ds.CASES[0], split="dev", support="maybe")
        problems = ds.validate_cases((bad, bad))
        assert any("duplicate" in p for p in problems)
        assert any("unknown split" in p for p in problems)
        assert any("unknown support" in p for p in problems)

    def test_the_digest_moves_with_any_change(self):
        changed = dataclasses.replace(ds.CASES[0], rationale=ds.CASES[0].rationale + " ")
        assert ds.dataset_digest(ds.CASES) != ds.dataset_digest((changed,) + ds.CASES[1:])


class TestRecordedResult:
    def test_held_out_reproduces_the_recorded_result(self):
        report = harness.score_split(ds.SPLIT_HELD_OUT)
        recorded = harness.RECORDED_HELD_OUT_RESULT
        assert report["policy_version"] == recorded["policy_version"]
        assert report["dataset_digest"] == recorded["dataset_digest"]
        assert report["applicable"] == recorded["applicable"]
        for name in ("label_agreement", "flag_precision", "flag_recall", "false_concern_rate"):
            assert (report[name]["count"], report[name]["n"]) == recorded[name], name
        assert tuple(sorted({m["case_id"] for m in report["misses"]})) == recorded["missed_case_ids"]

    def test_tuning_split(self):
        report = harness.score_split(ds.SPLIT_TUNING)
        assert (report["label_agreement"]["count"], report["label_agreement"]["n"]) == (22, 23)
        assert [m["case_id"] for m in report["misses"]] == ["ev-t19"]
        assert report["false_concern_rate"]["count"] == 0
        assert report["check_expectations"]["misses"] == 0

    def test_no_false_concern_on_any_supported_case(self):
        report = harness.score_split("all")
        assert report["false_concern_rate"]["count"] == 0


class TestHarness:
    def _summary(self):
        findings = [_finding(), _finding()]
        findings[0].verification = _result(source_quote="The clearance shall be 36 in.")
        findings[1].verification = _result(source_quote="The clearance shall be 18 in.")
        ev.annotate_evidence_assessments(findings)
        diag = DiagnosticsReport()
        record_verification_findings(diag, findings, phase="verification", transport="batch")
        return diag.summary()

    def test_disagreements_table(self, tmp_path):
        path = tmp_path / "export.json"
        path.write_text(json.dumps({"summary": self._summary()}, default=str), encoding="utf-8")
        table = harness.disagreements(harness.load_summary(path))
        assert table["recorded"] and table["disagreements"] == 1
        assert table["rows"][0]["adjudication"] == ""
        md = harness.disagreements_markdown(table)
        assert "| rf-000000000001 |" in md and "numbers_units" in md

    def test_a_summary_without_the_rollup_says_so(self):
        table = harness.disagreements({})
        assert table["recorded"] is False
        assert "SPEC_CRITIC_EVIDENCE_VALIDATION=observe" in table["reason"]

    def test_load_summary_accepts_a_bare_summary(self, tmp_path):
        path = tmp_path / "bare.json"
        path.write_text(json.dumps({"evidence_validation": {"assessed": 1}}), encoding="utf-8")
        assert harness.load_summary(path)["evidence_validation"]["assessed"] == 1

    def test_protocols_are_not_run_and_criteria_are_fixed(self):
        assert harness.VALIDATION_PROTOCOL["status"] == "NOT RUN"
        assert harness.REUSE_PROTOCOL["status"] == "NOT RUN"
        assert set(harness.PROMOTION_CRITERIA) == {"validation", "source_reuse"}
        assert "never" in harness.PROMOTION_CRITERIA["validation"]

    def test_cli_score_and_protocol(self, capsys):
        assert harness.main(["score", "--split", "held_out"]) == 0
        assert json.loads(capsys.readouterr().out)["policy_version"] == "ev1"
        assert harness.main(["protocol"]) == 0
        assert "recorded_held_out_result" in json.loads(capsys.readouterr().out)
