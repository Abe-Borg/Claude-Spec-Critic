"""Tests for the source_quote schema field.

The ``source_quote`` field is a verbatim quote on
every grounded verdict. The contract has five surfaces:

* ``VERIFICATION_VERDICT_SCHEMA`` requires ``source_quote`` (nullable for
  UNVERIFIED / DISPUTED, non-empty for CONFIRMED / CORRECTED).
* ``VerificationResult`` carries ``source_quote: str`` (defaults to empty).
* ``_verdict_from_tool_use`` and ``_parse_verification_response`` populate
  it from the model payload and demote CONFIRMED / CORRECTED to UNVERIFIED
  when it is empty.
* ``_apply_source_grounding`` and ``_enforce_grounding_invariant`` preserve
  the field through downgrade paths.
* The verification cache and resume state both serialize / deserialize
  the field; the cache schema bumped to v3 so v2 entries that predate
  the field cannot bypass the invariant on load.

These tests pin down the contract directly. Together they cover the
acceptance criteria in the plan:

* "A single verification call produces a ``source_quote`` field on the result."
* "Cache reload after a schema-v3 save preserves the quote."
* "A CONFIRMED verdict that arrives without a ``source_quote`` is downgraded
  to UNVERIFIED with an explanation pointing at the missing field."
"""
from __future__ import annotations

import json
import time
from pathlib import Path


from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.review.structured_schemas import VERIFICATION_VERDICT_SCHEMA
from src.verification.source_grounding import SearchedSource
from src.verification.verifier import (
    VerificationResult,
    _apply_source_grounding,
    _demote_if_missing_source_quote,
    _enforce_grounding_invariant,
    _get_verification_system_prompt,
    _parse_verification_response,
    _verdict_from_tool_use,
)
from src.verification.verification_cache import VerificationCache, _CACHE_SCHEMA_VERSION
from tests.fixtures.fake_anthropic import (
    sample_verification_verdict_payload,
    verification_tool_use_response,
)


def _finding(
    *,
    severity: str = "HIGH",
    issue: str = "code-cycle staleness",
    code_ref: str | None = "CBC 2025 §1004",
    existing: str | None = "per CBC 2019",
    replacement: str | None = "per CBC 2025",
    action: str = "EDIT",
    section: str = "2.1",
    filename: str = "23 21 13 - Hydronic.docx",
) -> Finding:
    return Finding(
        severity=severity,
        fileName=filename,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=0.6,
    )


# ===========================================================================
# 1. Schema shape
# ===========================================================================


class TestVerificationSchemaShape:
    def test_schema_lists_source_quote_required(self):
        assert "source_quote" in VERIFICATION_VERDICT_SCHEMA["required"]

    def test_schema_source_quote_is_nullable_string(self):
        prop = VERIFICATION_VERDICT_SCHEMA["properties"]["source_quote"]
        assert prop["type"] == ["string", "null"]


# ===========================================================================
# 3. Tool-use parser populates source_quote and demotes empty quotes
# ===========================================================================


class TestVerdictFromToolUse:
    def test_populates_source_quote_from_tool_input(self, fake_anthropic):
        message = fake_anthropic.verification_tool_use_response(
            payload=sample_verification_verdict_payload(
                source_quote="snippet text from search result"
            )
        )
        result = _verdict_from_tool_use(message)
        assert result is not None
        assert result.verdict == "CONFIRMED"
        assert result.source_quote == "snippet text from search result"

    def test_confirmed_with_empty_source_quote_demotes_to_unverified(self):
        # Build the tool-use message manually with empty source_quote so
        # we exercise the demotion path (the fixture defaults to a
        # non-empty quote).
        message = verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "The 2025 CPC is current.",
                "sources": ["https://www.dgs.ca.gov/"],
                "correction": None,
                "source_quote": "",
            }
        )
        result = _verdict_from_tool_use(message)
        assert result is not None
        assert result.verdict == "UNVERIFIED"
        assert "source_quote was empty" in result.explanation

    def test_unverified_with_empty_source_quote_stays_unverified(self):
        message = verification_tool_use_response(
            payload={
                "verdict": "UNVERIFIED",
                "explanation": "Insufficient evidence.",
                "sources": [],
                "correction": None,
                "source_quote": None,
            }
        )
        result = _verdict_from_tool_use(message)
        assert result is not None
        assert result.verdict == "UNVERIFIED"
        # No demotion suffix — the verdict was already UNVERIFIED.
        assert "source_quote was empty" not in result.explanation

    def test_disputed_with_empty_source_quote_stays_disputed(self):
        message = verification_tool_use_response(
            payload={
                "verdict": "DISPUTED",
                "explanation": "Conflicting sources.",
                "sources": ["https://example.com/"],
                "correction": None,
                "source_quote": "",
            }
        )
        result = _verdict_from_tool_use(message)
        assert result is not None
        assert result.verdict == "DISPUTED"

    def test_whitespace_only_quote_counts_as_empty(self):
        message = verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "Per the cited source.",
                "sources": ["https://www.dgs.ca.gov/"],
                "correction": None,
                "source_quote": "   \n  \t  ",
            }
        )
        result = _verdict_from_tool_use(message)
        assert result is not None
        # Whitespace-only should be treated as empty and demote.
        assert result.verdict == "UNVERIFIED"
        assert "source_quote was empty" in result.explanation


# ===========================================================================
# 4. Text-fallback parser also populates and demotes
# ===========================================================================


class TestTextFallbackSourceQuote:
    def test_populates_source_quote_from_json_text(self):
        body = json.dumps({
            "verdict": "CONFIRMED",
            "explanation": "Per the cited source.",
            "sources": ["https://nfpa.org/13"],
            "correction": None,
            "source_quote": "verbatim quote from search snippet",
        })
        result = _parse_verification_response(body)
        assert result.verdict == "CONFIRMED"
        assert result.source_quote == "verbatim quote from search snippet"

    def test_confirmed_with_missing_source_quote_demotes(self):
        # Field omitted entirely — coerces to empty and demotes.
        body = json.dumps({
            "verdict": "CONFIRMED",
            "explanation": "Per the cited source.",
            "sources": ["https://nfpa.org/13"],
            "correction": None,
        })
        result = _parse_verification_response(body)
        assert result.verdict == "UNVERIFIED"
        assert "source_quote was empty" in result.explanation


# ===========================================================================
# 5. _demote_if_missing_source_quote unit tests
# ===========================================================================


class TestDemoteIfMissingSourceQuote:
    def test_explanation_preserves_existing_text(self):
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://nfpa.org/13"],
            explanation="original explanation",
            source_quote="",
        )
        out = _demote_if_missing_source_quote(r)
        assert out.verdict == "UNVERIFIED"
        assert out.explanation.startswith("original explanation")
        assert "source_quote was empty" in out.explanation

    def test_demote_does_not_re_append_suffix(self):
        # Re-invoking on an already-demoted result should not double the suffix.
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://nfpa.org/13"],
            source_quote="",
        )
        once = _demote_if_missing_source_quote(r)
        # The demotion changes verdict to UNVERIFIED, so a second call
        # is a no-op (the demotion only fires for CONFIRMED / CORRECTED).
        twice = _demote_if_missing_source_quote(once)
        assert twice.explanation.count("source_quote was empty") == 1


# ===========================================================================
# 6. Grounding helpers preserve source_quote
# ===========================================================================


class TestGroundingHelpersPreserveQuote:
    def test_apply_source_grounding_keeps_quote_on_pass(self):
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://dgs.ca.gov/page"],
            source_quote="snippet from dgs",
        )
        out = _apply_source_grounding(
            r, searched=[SearchedSource(url="https://dgs.ca.gov/page", title="DGS")]
        )
        assert out.source_quote == "snippet from dgs"

    def test_apply_source_grounding_keeps_quote_on_downgrade(self):
        # Cited but not searched → downgrades to UNVERIFIED but quote stays.
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://invented.example.com"],
            source_quote="quote model claimed",
        )
        out = _apply_source_grounding(
            r, searched=[SearchedSource(url="https://dgs.ca.gov/page", title="DGS")]
        )
        assert out.verdict == "UNVERIFIED"
        # Quote is preserved through the downgrade so the report still
        # shows what the model said it relied on (even though that
        # citation didn't match a real search result).
        assert out.source_quote == "quote model claimed"

    def test_enforce_grounding_invariant_keeps_quote(self):
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=False,  # triggers downgrade
            sources=[],
            source_quote="quote text",
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"
        assert out.source_quote == "quote text"


# ===========================================================================
# 7. Cache persistence: schema v3 + source_quote round-trips
# ===========================================================================


class TestCacheSchemaV3:
    def test_schema_version_bumped_to_v3(self):
        assert _CACHE_SCHEMA_VERSION == 3

    def test_v2_files_are_dropped_on_load(self, tmp_path: Path):
        cache_path = tmp_path / "cache.json"
        v2_payload = {
            "version": 2,
            "saved_at": time.time(),
            "entries": {
                "legacy_key": {
                    "created_ts": time.time(),
                    "result": {
                        "verdict": "CONFIRMED",
                        "grounded": True,
                        "sources": ["https://nfpa.org/"],
                        "accepted_sources": ["https://nfpa.org/"],
                        "explanation": "stale",
                        "model_used": "claude-sonnet-4-6",
                        "escalated": False,
                        "web_search_requests": 1,
                        "successful_source_count": 1,
                        "search_error_count": 0,
                        "correction": None,
                    },
                },
            },
        }
        cache_path.write_text(json.dumps(v2_payload), encoding="utf-8")
        cache = VerificationCache()
        loaded = cache.load_from_disk(path=cache_path)
        assert loaded == 0
        assert cache.stats()["size"] == 0

    def test_v3_entries_round_trip_source_quote(self, tmp_path: Path):
        cache_path = tmp_path / "cache.json"
        cache = VerificationCache()
        f = _finding()
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                accepted_sources=["https://nfpa.org/"],
                sources=["https://nfpa.org/"],
                source_quote="snippet text from the source",
            ),
        )
        cache.save_to_disk(path=cache_path)

        # Reload into a fresh cache and confirm the quote came through.
        reloaded = VerificationCache()
        count = reloaded.load_from_disk(path=cache_path)
        assert count == 1
        hit = reloaded.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.source_quote == "snippet text from the source"
        assert hit.cache_status == "hit"

    def test_put_clones_source_quote_so_cache_is_immune_to_mutation(
        self, tmp_path: Path
    ):
        cache = VerificationCache()
        f = _finding()
        result = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            accepted_sources=["https://nfpa.org/"],
            sources=["https://nfpa.org/"],
            source_quote="original snippet",
        )
        cache.put(f, cycle=DEFAULT_CYCLE, result=result)
        # Mutating the original after put must not change the cached entry.
        result.source_quote = "mutated text"
        hit = cache.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.source_quote == "original snippet"


# ===========================================================================
# 9. Verifier system prompt instructs the model about source_quote
# ===========================================================================


class TestVerifierPromptMentionsSourceQuote:
    def test_prompt_mentions_source_quote_with_verdict_tool(self):
        prompt = _get_verification_system_prompt(
            DEFAULT_CYCLE, include_verdict_tool=True
        )
        assert "source_quote" in prompt
        # The instruction must be explicit about extracting verbatim text.
        assert "verbatim" in prompt.lower()
