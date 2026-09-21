"""The verifier's few-shot verdict examples obey the contracts they illustrate.

The review-module registry already enforces that every action type has a
worked example, on the documented rationale that output follows the example
set's shape. The verifier's ``<source_quote_requirements>`` block showed one
CONFIRMED example and nothing else — so the model had never seen ``correction``
populated (CORRECTED is the one verdict with an extra required field) and had
never seen DISPUTED demonstrated with the grounded citation the invariant
requires of it. These assertions apply the registry rule here, and pin the
three-way agreement the 2026-09 prompt review surfaced: the prompt, the verdict
schema, and the verification cache all ask a DISPUTED to carry the
contradicting passage (the parser tolerates its absence; the cache never
persists one without it).
"""
from __future__ import annotations

import json
import re

import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.structured_schemas import VERIFICATION_VERDICT_SCHEMA
from src.verification.verifier import _get_verification_system_prompt

QUOTE_BEARING_VERDICTS = {"CONFIRMED", "CORRECTED", "DISPUTED"}


def _section(prompt: str) -> str:
    assert "<source_quote_requirements>" in prompt
    return prompt.split("<source_quote_requirements>", 1)[1].split(
        "</source_quote_requirements>", 1
    )[0]


def _examples(prompt: str) -> list[dict]:
    blocks = re.findall(r"^\{.*?^\}", _section(prompt), re.S | re.M)
    return [json.loads(b) for b in blocks]


@pytest.fixture(params=[True, False], ids=["with_verdict_tool", "without_verdict_tool"])
def prompt(request) -> str:
    return _get_verification_system_prompt(
        DEFAULT_CYCLE, include_verdict_tool=request.param
    )


class TestVerdictExamplesMatchContracts:
    def test_one_example_per_quote_bearing_verdict(self, prompt):
        verdicts = [obj["verdict"] for obj in _examples(prompt)]
        assert sorted(verdicts) == sorted(QUOTE_BEARING_VERDICTS)

    def test_example_keys_match_the_verdict_schema(self, prompt):
        required = set(VERIFICATION_VERDICT_SCHEMA["required"])
        for obj in _examples(prompt):
            assert set(obj) == required

    def test_every_example_carries_a_verbatim_quote(self, prompt):
        # The cache refuses to persist any of these three without one.
        for obj in _examples(prompt):
            assert obj["source_quote"] and obj["source_quote"].strip()

    def test_only_corrected_populates_correction(self, prompt):
        by_verdict = {obj["verdict"]: obj for obj in _examples(prompt)}
        assert by_verdict["CORRECTED"]["correction"]
        assert by_verdict["CONFIRMED"]["correction"] is None
        assert by_verdict["DISPUTED"]["correction"] is None

    def test_every_example_cites_an_https_source(self, prompt):
        for obj in _examples(prompt):
            assert obj["sources"]
            assert all(url.startswith("https://") for url in obj["sources"])

    def test_examples_are_labeled_illustrative(self, prompt):
        section = _section(prompt)
        assert "do not copy their content" in section
        assert "placeholders" in section


class TestDisputedQuoteRuleAgreesAcrossSurfaces:
    def test_prompt_asks_disputed_for_the_contradicting_passage(self, prompt):
        section = _section(prompt)
        assert "Required whenever you render CONFIRMED, CORRECTED, or DISPUTED." in section
        assert "DISPUTED verdicts, source_quote may be null" not in section

    def test_schema_description_matches_the_prompt(self):
        desc = VERIFICATION_VERDICT_SCHEMA["properties"]["source_quote"]["description"]
        assert "for DISPUTED, the retrieved passage that contradicts" in desc
        assert "null for UNVERIFIED" in desc
        assert "optional/null for UNVERIFIED and DISPUTED" not in desc

    def test_cache_gate_names_the_same_three_verdicts(self):
        from src.verification.verification_cache import _CITATION_GATED_VERDICTS

        assert set(_CITATION_GATED_VERDICTS) == QUOTE_BEARING_VERDICTS
