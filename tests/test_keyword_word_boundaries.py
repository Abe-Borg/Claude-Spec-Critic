"""Whole-word keyword routing (A-11 / B-19).

The local-skip prescreen and the verification-profile classifier used to
test their keyword lists with bare substring membership, so a keyword fired
whenever it appeared as the tail or head of a longer word: ``"leed"`` on
"bleed valve" (which sent a bleed-valve finding down the never-verified
local-skip / internal-coordination path), ``"watts"`` on "kilowatts",
``"abb"`` on "abbreviation". Both surfaces now share
:func:`verification_profiles.matches_any_keyword`, which compiles each
keyword to a whole-word regex, and ``"formatting"`` — already removed from
the prescreen as unsafe — has left every module's ``internal_coordination``
vocabulary too.

These tests pin the matcher's edge rules, the two motivating scenarios, and
— parametrized over the module registry and both prescreen lists — that
every keyword currently declared still matches itself in a sentence, so a
future keyword with an awkward edge cannot silently stop matching.
"""
from __future__ import annotations

import pytest

from src.modules.registry import AVAILABLE_MODULES
from src.review.reviewer import Finding
from src.verification.verification_modes import (
    VerificationMode,
    select_verification_mode,
)
from src.verification.verification_prescreen import (
    _LOCAL_SKIP_KEYWORDS,
    _LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED,
    classify_finding_for_verification,
    local_skip_requires_elevated_confidence,
)
from src.verification.verification_profiles import (
    KEYWORD_PREFIX_MARKER,
    VerificationProfile,
    classify_finding_profile,
    compile_keyword_pattern,
    compile_keyword_patterns,
    matches_any_keyword,
)

_FIELDS = ("jurisdictional", "manufacturer", "code_standard", "internal_coordination")


def _modules():
    registry = AVAILABLE_MODULES
    return list(registry.values()) if isinstance(registry, dict) else list(registry)


def _stem(keyword: str) -> str:
    return keyword[:-1] if keyword.endswith(KEYWORD_PREFIX_MARKER) else keyword


def _finding(
    *,
    severity: str = "MEDIUM",
    issue: str = "",
    code_reference: str | None = None,
    existing: str | None = None,
    replacement: str | None = None,
) -> Finding:
    return Finding(
        severity=severity,
        fileName="23 05 00 - Common Work for HVAC.docx",
        section="",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=existing,
        replacementText=replacement,
        codeReference=code_reference,
    )


def _matches(keyword: str, text: str) -> bool:
    return bool(compile_keyword_pattern(keyword).search(text))


# ---------------------------------------------------------------------------
# Matcher edge rules
# ---------------------------------------------------------------------------


class TestMatcherEdgeRules:
    @pytest.mark.parametrize(
        "keyword, text",
        [
            ("leed", "bleed valve"),
            ("leed", "the bleed line drains to a floor sink"),
            ("watts", "the unit draws 12 kilowatts"),
            ("abb", "the abbreviation list is incomplete"),
            ("sel-", "diesel-driven fire pump"),
            ("ul-", "full-height partition"),
            ("pex", "the apex of the roof"),
            ("cec ", "cecil street elevation"),
            ("permit", "not permitted by the code"),
        ],
        ids=lambda v: v if len(v) < 20 else v[:20],
    )
    def test_word_edges_block_tail_and_head_matches(self, keyword, text):
        assert _matches(keyword, text) is False

    @pytest.mark.parametrize(
        "keyword, text",
        [
            ("leed", "leed silver certification"),
            ("leed", "pursue leed."),
            ("leed", "(leed) credits"),
            ("watts", "watts backflow preventer"),
            ("abb", "abb switchgear"),
            ("sel-", "sel-451 relay"),
            ("ul-", "ul-1479 assembly"),
            ("permit", "permit set"),
            ("permit", "building permits"),
        ],
    )
    def test_whole_words_still_match(self, keyword, text):
        assert _matches(keyword, text) is True

    @pytest.mark.parametrize(
        "keyword, text",
        [
            ("[select]", "placeholder [select] left in 2.01"),
            ("[verify]", "[verify] with owner"),
            ("[insert", "[insert manufacturer]"),
            ("[insert", "[insert]"),
            ("???", "capacity is ??? gpm"),
            ("???", "????"),
            ("calif.", "calif. building code"),
            ("no.", "model no. 123"),
            ("§", "per § 1203.1"),
            ("ul-", "ul-listed assembly"),
            ("bsc.ca.gov", "see www.bsc.ca.gov/codes"),
            ("ca.gov", "see www.bsc.ca.gov/codes"),
            ("icc-es", "icc-es esr-1234"),
            ("can/ulc-s524", "install per can/ulc-s524."),
        ],
    )
    def test_non_word_edges_stay_literal(self, keyword, text):
        assert _matches(keyword, text) is True

    def test_trailing_whitespace_requires_a_separator(self):
        assert _matches("cec ", "per cec 120.1(c)") is True
        assert _matches("cec ", "per cec\n120.1(c)") is True
        assert _matches("cec ", "cecil") is False
        assert _matches("cec ", "per cec") is False
        assert _matches("ul ", "ul 300 listed hood") is True
        assert _matches("ul ", "bulk storage") is False
        assert _matches("ul ", "schedule 40") is False

    @pytest.mark.parametrize(
        "text",
        [
            "internal contradiction",
            "internal\ncontradiction",
            "internal   contradiction",
            "internal\t contradiction",
            "an internal\r\ncontradiction between 1.02 and 3.01",
        ],
    )
    def test_internal_whitespace_matches_any_run(self, text):
        assert _matches("internal contradiction", text) is True

    def test_internal_whitespace_does_not_match_absence(self):
        assert _matches("internal contradiction", "internalcontradiction") is False
        assert _matches("internal contradiction", "internal-contradiction") is False

    @pytest.mark.parametrize(
        "keyword, text, expected",
        [
            ("asme a13.1", "label valves per asme a13.1 color", True),
            ("asme a13.1", "asme a13x1", False),
            ("title 24", "title 24, part 6", True),
            ("title 24", "title 245", False),
            ("ul listed", "ul listed device", True),
            ("ashrae 90.1", "ashrae 90.1-2019", True),
        ],
    )
    def test_internal_punctuation_is_escaped(self, keyword, text, expected):
        assert _matches(keyword, text) is expected

    @pytest.mark.parametrize(
        "keyword, text, expected",
        [
            ("standard", "comply with applicable standards", True),
            ("standard", "the standard's edition", True),
            ("standard", "standardized fittings", False),
            ("placeholder", "placeholders remain in 2.01", True),
            ("submittal", "submittals were not listed", True),
            ("manufacturer", "manufacturer's data", True),
            ("manufacturer", "manufacturers' data", True),
            ("fire marshal", "fire marshals in both jurisdictions", True),
            ("ahj", "the ahj's ruling", True),
        ],
    )
    def test_trailing_letter_edge_tolerates_plural(self, keyword, text, expected):
        assert _matches(keyword, text) is expected

    @pytest.mark.parametrize(
        "keyword, text, expected",
        [
            ("nfpa 70", "nfpa 70-2023", True),
            ("nfpa 70", "nfpa 70 article 250", True),
            ("nfpa 70", "nfpa 70b", False),
            ("est4", "est4 panel", True),
            ("est4", "est45", False),
            ("title-24", "title-24 compliance", True),
        ],
    )
    def test_trailing_digit_edge_takes_plain_boundary(self, keyword, text, expected):
        assert _matches(keyword, text) is expected

    @pytest.mark.parametrize(
        "keyword, text, expected",
        [
            ("self-referen*", "the clause is self-referential", True),
            ("self-referen*", "paragraph 2.01 self-references 2.01", True),
            ("self-referen*", "self-referencing note", True),
            ("self-referen*", "unself-referential", False),
            ("typo*", "typo in 2.01", True),
            ("typo*", "typos throughout", True),
            ("typo*", "typographical error", True),
            ("typo*", "prototype", False),
        ],
    )
    def test_prefix_marker_is_open_ended(self, keyword, text, expected):
        assert _matches(keyword, text) is expected

    def test_case_insensitive(self):
        assert _matches("leed", "LEED Silver") is True
        assert _matches("title 24", "Title 24 Part 6") is True

    @pytest.mark.parametrize("keyword", ["", "   ", "*", " \t*"])
    def test_empty_keyword_rejected(self, keyword):
        with pytest.raises(ValueError):
            compile_keyword_pattern(keyword)

    def test_non_string_keyword_rejected(self):
        with pytest.raises(TypeError):
            compile_keyword_pattern(None)  # type: ignore[arg-type]

    def test_patterns_cached_per_tuple_and_per_keyword(self):
        keywords = ("leed", "internal contradiction")
        assert compile_keyword_patterns(keywords) is compile_keyword_patterns(keywords)
        assert compile_keyword_pattern("leed") is compile_keyword_pattern("leed")
        # Registry tuples are the hot-path inputs — they must hit the cache too.
        for module in _modules():
            for field in _FIELDS:
                terms = getattr(module.profile_keywords, field)
                assert compile_keyword_patterns(terms) is compile_keyword_patterns(terms)

    def test_matches_any_keyword_contract(self):
        assert matches_any_keyword("", ("leed",)) is False
        assert matches_any_keyword("bleed valve", ("leed", "placeholder")) is False
        assert matches_any_keyword("leed valve", ("leed", "placeholder")) is True
        # Non-tuple iterables are accepted (coerced for the cache key).
        assert matches_any_keyword("leed valve", ["leed"]) is True


# ---------------------------------------------------------------------------
# A-11 — "bleed" must not match "leed"
# ---------------------------------------------------------------------------


_BLEED_ISSUES = [
    "Bleed valve on the hydronic loop has no size called out.",
    "Bleed line drains to a floor sink with no air gap.",
]


class TestBleedDoesNotMatchLeed:
    @pytest.mark.parametrize("issue", _BLEED_ISSUES)
    def test_gripes_bleed_is_web_required(self, issue):
        finding = _finding(severity="GRIPES", issue=issue)
        assert classify_finding_for_verification(finding) == "web_required"
        assert local_skip_requires_elevated_confidence(finding) is False

    @pytest.mark.parametrize("issue", _BLEED_ISSUES)
    def test_medium_bleed_is_web_required(self, issue):
        finding = _finding(severity="MEDIUM", issue=issue)
        assert classify_finding_for_verification(finding) == "web_required"

    @pytest.mark.parametrize("module", _modules(), ids=lambda m: m.module_id)
    @pytest.mark.parametrize("issue", _BLEED_ISSUES)
    def test_bleed_never_routes_internal_coordination(self, module, issue):
        finding = _finding(severity="MEDIUM", issue=issue)
        profile = classify_finding_profile(finding, keywords=module.profile_keywords)
        assert profile is not VerificationProfile.INTERNAL_COORDINATION
        assert (
            select_verification_mode(finding, keywords=module.profile_keywords)
            is VerificationMode.STANDARD_REASONING
        )

    def test_bleed_in_existing_text_is_not_a_leed_signal(self):
        finding = _finding(
            severity="GRIPES",
            issue="Spelling: 'seperate'.",
            existing="Provide a bleed valve at each high point.",
        )
        assert classify_finding_for_verification(finding) == "web_required"

    def test_leed_silver_still_local_skip_with_elevated_flag(self):
        finding = _finding(
            severity="GRIPES",
            issue="LEED Silver certification is inappropriate for this project type.",
        )
        assert classify_finding_for_verification(finding) == "local_skip"
        assert local_skip_requires_elevated_confidence(finding) is True
        # The default (California) vocabulary lists "leed" as internal noise.
        assert (
            classify_finding_profile(finding)
            is VerificationProfile.INTERNAL_COORDINATION
        )

    def test_regular_keyword_still_beats_elevated_keyword(self):
        finding = _finding(
            severity="GRIPES", issue="LEED placeholder needs to be replaced."
        )
        assert classify_finding_for_verification(finding) == "local_skip"
        assert local_skip_requires_elevated_confidence(finding) is False

    def test_code_reference_and_severity_still_force_web_required(self):
        with_ref = _finding(
            severity="GRIPES",
            issue="LEED Silver certification is inappropriate.",
            code_reference="CALGreen 5.106",
        )
        assert classify_finding_for_verification(with_ref) == "web_required"
        assert local_skip_requires_elevated_confidence(with_ref) is False
        high = _finding(severity="HIGH", issue="LEED Silver certification is inappropriate.")
        assert classify_finding_for_verification(high) == "web_required"
        assert local_skip_requires_elevated_confidence(high) is False


# ---------------------------------------------------------------------------
# B-19 — "formatting" leaves internal_coordination
# ---------------------------------------------------------------------------


_ASME_FORMATTING = "Label valves per ASME A13.1 color formatting."


class TestFormattingLeavesInternalCoordination:
    @pytest.mark.parametrize("module", _modules(), ids=lambda m: m.module_id)
    def test_formatting_absent_from_every_module(self, module):
        terms = module.profile_keywords.internal_coordination
        assert "formatting" not in terms
        assert matches_any_keyword(_ASME_FORMATTING.lower(), terms) is False

    @pytest.mark.parametrize("module", _modules(), ids=lambda m: m.module_id)
    def test_asme_formatting_never_internal_coordination(self, module):
        bare = _finding(severity="MEDIUM", issue=_ASME_FORMATTING)
        with_ref = _finding(
            severity="MEDIUM", issue=_ASME_FORMATTING, code_reference="ASME A13.1"
        )
        for finding in (bare, with_ref):
            profile = classify_finding_profile(finding, keywords=module.profile_keywords)
            assert profile in {
                VerificationProfile.CODE_STANDARD,
                VerificationProfile.CONSTRUCTABILITY,
            }
        # A code reference is a code claim regardless of vocabulary.
        assert (
            classify_finding_profile(with_ref, keywords=module.profile_keywords)
            is VerificationProfile.CODE_STANDARD
        )

    def test_asme_formatting_is_code_standard_under_california(self):
        bare = _finding(severity="MEDIUM", issue=_ASME_FORMATTING)
        assert classify_finding_profile(bare) is VerificationProfile.CODE_STANDARD
        assert (
            select_verification_mode(bare) is VerificationMode.STANDARD_REASONING
        )

    def test_prescreen_formatting_is_web_required(self):
        finding = _finding(
            severity="GRIPES", issue="Color formatting label requirements may be wrong."
        )
        assert classify_finding_for_verification(finding) == "web_required"


# ---------------------------------------------------------------------------
# Every declared keyword still matches itself (registry-parametrized)
# ---------------------------------------------------------------------------


def _registry_keyword_cases():
    cases = []
    for module in _modules():
        for field in _FIELDS:
            for keyword in getattr(module.profile_keywords, field):
                cases.append(
                    pytest.param(
                        module, field, keyword,
                        id=f"{module.module_id}-{field}-{keyword!r}",
                    )
                )
    return cases


def _prescreen_keyword_cases():
    cases = []
    for keyword in _LOCAL_SKIP_KEYWORDS:
        cases.append(pytest.param(keyword, False, id=f"regular-{keyword!r}"))
    for keyword in _LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED:
        cases.append(pytest.param(keyword, True, id=f"elevated-{keyword!r}"))
    return cases


class TestEveryDeclaredKeywordStillMatches:
    @pytest.mark.parametrize("module, field, keyword", _registry_keyword_cases())
    def test_module_keyword_matches_itself_in_a_sentence(self, module, field, keyword):
        sentence = f"The reviewer noted {_stem(keyword)} in paragraph 2.01."
        terms = getattr(module.profile_keywords, field)
        assert matches_any_keyword(sentence.lower(), (keyword,)) is True
        assert matches_any_keyword(sentence.lower(), terms) is True
        # Through the real classifier: something fired (precedence may pick
        # a higher-priority field when vocabularies overlap, e.g. "cec ").
        finding = _finding(severity="MEDIUM", issue=sentence)
        profile = classify_finding_profile(finding, keywords=module.profile_keywords)
        assert profile is not VerificationProfile.CONSTRUCTABILITY

    @pytest.mark.parametrize("module, field, keyword", _registry_keyword_cases())
    def test_module_keyword_compiles_once(self, module, field, keyword):
        del module, field
        assert compile_keyword_pattern(keyword) is compile_keyword_pattern(keyword)

    @pytest.mark.parametrize("keyword, elevated", _prescreen_keyword_cases())
    def test_prescreen_keyword_matches_itself_in_a_gripe(self, keyword, elevated):
        sentence = f"The reviewer noted {_stem(keyword)} in paragraph 2.01."
        finding = _finding(severity="GRIPES", issue=sentence)
        assert classify_finding_for_verification(finding) == "local_skip"
        assert local_skip_requires_elevated_confidence(finding) is elevated

    @pytest.mark.parametrize("module", _modules(), ids=lambda m: m.module_id)
    def test_self_referen_stem_still_covers_real_forms(self, module):
        terms = module.profile_keywords.internal_coordination
        assert matches_any_keyword("the clause is self-referential.", terms) is True
        assert matches_any_keyword("paragraph 2.01 self-references 2.01.", terms) is True

    def test_typographical_still_local_skip(self):
        finding = _finding(
            severity="GRIPES",
            issue="Typographical error: 'seperate' should be 'separate'.",
        )
        assert classify_finding_for_verification(finding) == "local_skip"
        assert local_skip_requires_elevated_confidence(finding) is False

    @pytest.mark.parametrize(
        "issue",
        [
            "Duplicate paragraph detected in 2.01 and 2.03.",
            "Template marker TODO: left in 3.02.",
            "Lorem ipsum boilerplate remains in 1.05.",
            "Empty section heading with no body.",
            "Inconsistent filename versus CSI number.",
            "Invalid code cycle year cited.",
            "Placeholder text remains.",
        ],
    )
    def test_deterministic_rule_phrases_still_local_skip(self, issue):
        finding = _finding(severity="GRIPES", issue=issue)
        assert classify_finding_for_verification(finding) == "local_skip"
        assert local_skip_requires_elevated_confidence(finding) is False


# ---------------------------------------------------------------------------
# Precedence is unchanged for whole-word matches
# ---------------------------------------------------------------------------


class TestPrecedenceUnchanged:
    def test_internal_coordination_precedes_jurisdictional_across_line_break(self):
        finding = _finding(
            severity="HIGH",
            issue="Internal\ncontradiction: Title 24 ventilation rates differ.",
        )
        assert (
            classify_finding_profile(finding)
            is VerificationProfile.INTERNAL_COORDINATION
        )
        assert select_verification_mode(finding) is VerificationMode.STRICT_STRUCTURED

    def test_jurisdictional_precedes_code_standard(self):
        finding = _finding(
            severity="CRITICAL",
            issue="DSA amendment to the CBC differs from the spec language.",
            code_reference="CBC 1616A",
        )
        assert classify_finding_profile(finding) is VerificationProfile.JURISDICTIONAL
        assert select_verification_mode(finding) is VerificationMode.DEEP_REASONING

    def test_manufacturer_precedes_code_standard_keyword(self):
        finding = _finding(
            severity="MEDIUM",
            issue="Greenheck catalog lists a different NFPA rating for this fan.",
        )
        assert classify_finding_profile(finding) is VerificationProfile.MANUFACTURER

    def test_gripes_with_code_reference_stays_strict(self):
        finding = _finding(
            severity="GRIPES",
            issue="Valve label color formatting is inconsistent.",
            code_reference="ASME A13.1",
        )
        assert classify_finding_for_verification(finding) == "web_required"
        assert select_verification_mode(finding) is VerificationMode.STRICT_STRUCTURED
