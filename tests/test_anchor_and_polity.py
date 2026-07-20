"""WS-4c: deterministic anchor validation (D-16) + wrong-polity tokens (D-15).

Hermetic. The anchor validator turns anchor hallucination into a
deterministic impossibility; the polity detector flags country-mismatched
tokens with zero model calls — and BOTH must leave profile-less runs
byte-identical.
"""
from __future__ import annotations

import dataclasses

import pytest

from src.input.preprocessor import (
    DETERMINISTIC_RULE_WRONG_POLITY,
    detect_wrong_polity_tokens,
    preprocess_spec,
)
from src.modules import DEFAULT_MODULE, PolityTokenRule, validate_module_registry
from src.review.reviewer import Finding, validate_finding_anchors


# ---------------------------------------------------------------------------
# Anchor validation (D-16)
# ---------------------------------------------------------------------------


_SPEC_TEXT = (
    "PART 1 - GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Comply with the 2015 IBC,   Chapter 9 throughout.\n"
    "B. Provide wet-pipe sprinkler systems per NFPA 13.\n"
)
_TEXTS = {"21 13 13 Wet-Pipe.docx": _SPEC_TEXT}


def _finding(action: str, **overrides) -> Finding:
    defaults = dict(
        severity="HIGH",
        fileName="21 13 13 Wet-Pipe.docx",
        section="1.01",
        issue="Stale code edition.",
        actionType=action,
        existingText="Comply with the 2015 IBC,   Chapter 9 throughout."
        if action in ("EDIT", "DELETE")
        else None,
        replacementText="Comply with the 2024 IBC." if action != "DELETE" else None,
        codeReference="IBC",
        confidence=0.9,
        anchorText="PART 1 - GENERAL" if action == "ADD" else None,
        insertPosition="after" if action == "ADD" else None,
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestAnchorValidation:
    def test_verbatim_hit_passes(self):
        finding = _finding("EDIT")
        assert validate_finding_anchors([finding], _TEXTS) == 0
        assert finding.actionType == "EDIT"
        assert finding.demotion_reason is None

    def test_whitespace_collapsed_hit_passes(self):
        finding = _finding(
            "EDIT", existingText="Comply with the 2015 IBC, Chapter 9 throughout."
        )
        assert validate_finding_anchors([finding], _TEXTS) == 0
        assert finding.actionType == "EDIT"

    def test_add_anchor_hit_passes(self):
        finding = _finding("ADD")
        assert validate_finding_anchors([finding], _TEXTS) == 0
        assert finding.actionType == "ADD"

    def test_miss_demotes_to_report_only_never_dropped(self):
        finding = _finding("EDIT", existingText="Text that exists nowhere at all.")
        findings = [finding]
        assert validate_finding_anchors(findings, _TEXTS) == 1
        assert findings == [finding]  # never dropped (invariant 8)
        assert finding.actionType == "REPORT_ONLY"
        assert finding.demotion_reason == (
            "existing text not found in 21 13 13 Wet-Pipe.docx"
        )
        assert finding.existingText is None
        assert finding.as_edit_proposal() is None

    def test_add_anchor_miss_demotes(self):
        finding = _finding("ADD", anchorText="PART 9 - IMAGINARY")
        assert validate_finding_anchors([finding], _TEXTS) == 1
        assert finding.actionType == "REPORT_ONLY"
        assert "anchor text not found" in finding.demotion_reason

    def test_delete_existing_miss_demotes(self):
        finding = _finding("DELETE", existingText="No such clause.")
        assert validate_finding_anchors([finding], _TEXTS) == 1
        assert finding.actionType == "REPORT_ONLY"

    def test_unknown_file_skips_check(self):
        finding = _finding(
            "EDIT",
            fileName="99 99 99 Unextracted.docx",
            existingText="Text that exists nowhere at all.",
        )
        assert validate_finding_anchors([finding], _TEXTS) == 0
        assert finding.actionType == "EDIT"

    def test_report_only_findings_untouched(self):
        finding = _finding(
            "REPORT_ONLY", existingText=None, replacementText=None, anchorText=None
        )
        assert validate_finding_anchors([finding], _TEXTS) == 0
        assert finding.actionType == "REPORT_ONLY"

    def test_occurrence_originals_checked_against_their_own_files(self):
        texts = {
            "a.docx": "The clause exists here.",
            "b.docx": "Entirely different content.",
        }
        original_a = _finding("EDIT", fileName="a.docx", existingText="The clause exists here.")
        original_b = _finding("EDIT", fileName="b.docx", existingText="The clause exists here.")
        representative = _finding(
            "EDIT",
            fileName="a.docx",
            existingText="The clause exists here.",
            affected_files=["a.docx", "b.docx"],
            occurrence_originals=[original_a, original_b],
        )
        validate_finding_anchors([representative], texts)
        # The representative and file a's original pass; file b's original —
        # whose text does not contain the clause — demotes.
        assert representative.actionType == "EDIT"
        assert original_a.actionType == "EDIT"
        assert original_b.actionType == "REPORT_ONLY"
        assert "not found in b.docx" in original_b.demotion_reason


# ---------------------------------------------------------------------------
# Wrong-polity token detector (D-15)
# ---------------------------------------------------------------------------


_CA_RULES = (
    PolityTokenRule(
        country="CA",
        pattern=r"\bNFPA\s*70\b|\bNEC\b",
        note="NFPA 70/NEC is the US electrical code; Canadian projects are governed by CSA C22.1.",
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bU\.?L\.?[- ]listed\b",
        note="Bare UL listing may not be recognized in Canada; cULus/ULC is required.",
    ),
    PolityTokenRule(
        country="US",
        pattern=r"O\. ?Reg\.",
        note="'O. Reg.' cites an Ontario regulation; US projects are not governed by it.",
    ),
)

_CANADIAN_RUN_TEXT = (
    "Wiring shall comply with NFPA 70. Provide U.L. listed solenoid valves "
    "per O. Reg. 213/07."
)


class TestPolityTokenDetector:
    def test_country_filtered_rules_fire(self):
        alerts = detect_wrong_polity_tokens(
            _CANADIAN_RUN_TEXT, "a.docx", rules=_CA_RULES, country="CA"
        )
        matches = {a["match"] for a in alerts}
        assert "NFPA 70" in matches
        assert "U.L. listed" in matches
        # The US-only rule must NOT fire on a CA run.
        assert not any("O. Reg." in m for m in matches)
        for alert in alerts:
            assert alert["deterministic_rule"] == DETERMINISTIC_RULE_WRONG_POLITY
            assert alert["note"]
            assert alert["note"] in alert["context"]

    def test_us_run_fires_us_rules_only(self):
        alerts = detect_wrong_polity_tokens(
            _CANADIAN_RUN_TEXT, "a.docx", rules=_CA_RULES, country="US"
        )
        assert [a["match"] for a in alerts] == ["O. Reg."]

    def test_preprocess_profile_less_run_is_byte_identical(self):
        with_none = preprocess_spec(_CANADIAN_RUN_TEXT, "a.docx", cycle=None)
        assert with_none.polity_alerts == []
        # Explicit None country: identical result shape.
        explicit = preprocess_spec(
            _CANADIAN_RUN_TEXT, "a.docx", cycle=None, profile_country=None
        )
        assert dataclasses.asdict(explicit) == dataclasses.asdict(with_none)

    def test_preprocess_with_country_but_module_without_rules_is_silent(self):
        # The registered modules ship no polity rules yet (WS-5 adds the DC
        # seed sets), so even a country-bearing call emits nothing.
        result = preprocess_spec(
            _CANADIAN_RUN_TEXT, "a.docx", cycle=None, profile_country="CA"
        )
        assert result.polity_alerts == []

    def test_enabled_module_with_rules_validates(self):
        module = dataclasses.replace(
            DEFAULT_MODULE,
            project_profile_enabled=True,
            research_persona="You research.",
            research_dimensions=(
                __import__("src.modules", fromlist=["ResearchDimension"]).ResearchDimension(
                    dimension_id="governing_codes",
                    title="Codes",
                    prompt_template="Codes for {city}.",
                ),
            ),
            compliance_persona="You evaluate.",
            compliance_severity_definitions="- CRITICAL — blocker.",
            polity_suspect_tokens=_CA_RULES,
        )
        validate_module_registry([module])

    def test_real_datacenter_module_rules_behavior(self):
        """Pin the SHIPPED datacenter_fire polity rules (WS-5).

        Regression guard for two case/collision bugs the review caught: the
        UL rule must catch title-case "UL Listed", and the seismic rule must
        NOT fire on "Safety Data Sheets (SDS)". These are the real rules the
        profile-enabled Canadian review depends on, not synthetic ones.
        """
        from src.modules import DATACENTER_FIRE

        rules = DATACENTER_FIRE.polity_suspect_tokens

        def _matches(text: str, country: str) -> set[str]:
            return {
                a["match"]
                for a in detect_wrong_polity_tokens(
                    text, "a.docx", rules=rules, country=country
                )
            }

        # UL rule is case-insensitive on "listed" — the common title-case form
        # must be caught, while cULus/ULC (no word boundary before U) must not.
        assert _matches("Provide UL Listed control valves.", "CA")
        assert _matches("Provide U.L. Listed solenoids.", "CA")
        assert not _matches("Provide cULus-listed control valves.", "CA")
        assert not _matches("Provide ULC Listed devices.", "CA")

        # Seismic rule: real seismic notation fires; a Safety Data Sheet does not.
        assert _matches("Design to Seismic Design Category B (SDC B).", "CA")
        assert _matches("Provide S_DS and S_D1 design spectral values.", "CA")
        assert _matches("bulk", "CA") == set()  # sanity: unrelated text is silent
        assert not any(
            "SDS" in m
            for m in _matches(
                "Submit Safety Data Sheets (SDS) for all products.", "CA"
            )
        )

        # Life-safety code: spaced and hyphenated compounds both fire.
        assert _matches("Comply with the Life Safety Code.", "CA")
        assert _matches("Provide life-safety code egress signage.", "CA")

        # DOT-proximity vessel words are word-bounded — no match inside
        # 'tankage' / 'transceiver', but a real DOT-rated cylinder fires.
        assert _matches("Nitrogen cylinder shall be DOT rated.", "CA")
        assert not _matches(
            "Provide DOT radios and a transceiver in the tankage area.", "CA"
        )

        # US-only rules fire on US runs; the CA-only UL rule does not.
        assert "O. Reg." in _matches("Comply with O. Reg. 213/07.", "US")
        assert not _matches("Provide UL Listed valves.", "US")

    def test_real_datacenter_architectural_module_rules_behavior(self):
        """Pin the SHIPPED datacenter_architectural polity rules.

        The arch module assembles the shared jurisdiction-generic rules (from
        ``modules._datacenter_shared``) plus its own accessibility / fire-test
        / energy-code additions. Pins: the shared rules behave identically
        under the arch tuple (the shared-file refactor's behavior proof from
        the consuming side), the arch additions fire on the right country, and
        the case-sensitivity that keeps ``\\bADA\\b`` from matching inside
        "Canada" holds.
        """
        from src.modules import DATACENTER_ARCHITECTURAL

        rules = DATACENTER_ARCHITECTURAL.polity_suspect_tokens

        def _matches(text: str, country: str) -> set[str]:
            return {
                a["match"]
                for a in detect_wrong_polity_tokens(
                    text, "a.docx", rules=rules, country=country
                )
            }

        # Shared rules ride along unchanged: UL-listed case handling and the
        # SDS / Safety-Data-Sheet collision guard behave exactly as under fire.
        assert _matches("Provide UL Listed door hardware.", "CA")
        assert not _matches("Provide cULus-listed hardware.", "CA")
        assert not any(
            "SDS" in m
            for m in _matches(
                "Submit Safety Data Sheets (SDS) for all products.", "CA"
            )
        )

        # Arch CA-run additions: US accessibility / energy / fire-test regimes.
        assert _matches("Comply with ADA requirements at all entrances.", "CA")
        assert _matches("Meet ADAAG clearances at doors.", "CA")
        # \bADA\b is case-sensitive — it must never fire inside "Canada".
        assert not _matches("Projects across Canada use this section.", "CA")
        assert _matches("Comply with ICC A117.1 for accessible routes.", "CA")
        assert _matches("Meet ANSI A117.1 reach ranges.", "CA")
        assert _matches("Envelope shall comply with the IECC.", "CA")
        assert _matches("Fire-resistance ratings established per ASTM E119.", "CA")
        assert _matches("Fire door assemblies tested per UL 10C.", "CA")

        # Arch US-run additions: Canadian standards/regimes flag on US runs.
        assert _matches("Rated per CAN/ULC-S101 for two hours.", "US")
        assert _matches("Comply with the NECB for envelope performance.", "US")
        assert _matches("Ratings per the Ontario Building Code apply.", "US")
        # Bare "OBC" is deliberately NOT flagged — on US runs it is also the
        # Ohio Building Code, a legitimate governing-code citation there.
        assert not _matches("Comply with OBC 1301.1 for rated assemblies.", "US")
        # ...and stay silent on the matching country's own runs.
        assert not any(
            "NECB" in m
            for m in _matches("Comply with the NECB for envelope performance.", "CA")
        )
        assert not any(
            "ADA" in m
            for m in _matches("Comply with ADA requirements at entrances.", "US")
        )

    def test_rule_validation_rejects_bad_country_and_pattern(self):
        def _module(**overrides):
            from src.modules import ResearchDimension

            defaults = dict(
                project_profile_enabled=True,
                research_persona="You research.",
                research_dimensions=(
                    ResearchDimension(
                        dimension_id="governing_codes",
                        title="Codes",
                        prompt_template="Codes for {city}.",
                    ),
                ),
                compliance_persona="You evaluate.",
                compliance_severity_definitions="- CRITICAL — blocker.",
            )
            defaults.update(overrides)
            return dataclasses.replace(DEFAULT_MODULE, **defaults)

        with pytest.raises(ValueError, match="country must be"):
            validate_module_registry(
                [_module(polity_suspect_tokens=(
                    PolityTokenRule(country="UK", pattern=r"x", note="n"),
                ))]
            )
        with pytest.raises(ValueError, match="does not compile"):
            validate_module_registry(
                [_module(polity_suspect_tokens=(
                    PolityTokenRule(country="CA", pattern=r"[unclosed", note="n"),
                ))]
            )
        with pytest.raises(ValueError, match="must be empty"):
            validate_module_registry(
                [dataclasses.replace(
                    DEFAULT_MODULE,
                    polity_suspect_tokens=(
                        PolityTokenRule(country="CA", pattern=r"x", note="n"),
                    ),
                )]
            )
