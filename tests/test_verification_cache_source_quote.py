"""The verification cache enforces the ``source_quote`` invariant (B-13a).

The v3 cache shape carries ``source_quote`` — the verbatim snippet the model
said it relied on — so a cached CONFIRMED / CORRECTED can always render its
audit trail. Until now the only enforcement was the verifier's parse-time
demotion (``_demote_if_missing_source_quote``); ``put`` and ``load_from_disk``
never checked, so a direct ``put`` or a hand-edited row could resurrect a
quote-less grounded verdict for the TTL window. Both now refuse.

DISPUTED is deliberately *not* quote-gated: the verifier lets a quote-less
DISPUTED stand (the evidence conflicts rather than supports), so the cache
mirrors that exact rule rather than inventing a stricter one.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_cache import (
    VerificationCache,
    _CACHE_SCHEMA_VERSION,
    _CITATION_GATED_VERDICTS,
    _QUOTE_GATED_VERDICTS,
)
from src.verification.verifier import VerificationResult

_URL = "https://dgs.ca.gov/page"


def _finding() -> Finding:
    return Finding(
        severity="HIGH",
        fileName="Section_22_1000.docx",
        section="2.1",
        issue="Stale code reference",
        actionType="EDIT",
        existingText="2019 CBC",
        replacementText="2025 CBC",
        codeReference="CBC 1234",
    )


def _result(verdict: str, *, quote: str) -> VerificationResult:
    return VerificationResult(
        verdict=verdict,
        explanation="grounded",
        grounded=True,
        sources=[_URL],
        accepted_sources=[_URL],
        correction="It is 2025." if verdict == "CORRECTED" else None,
        source_quote=quote,
    )


def _row(verdict: str, *, quote: str) -> dict:
    return {
        "created_ts": time.time(),
        "result": {
            "verdict": verdict,
            "explanation": "hand-written",
            "grounded": True,
            "sources": [_URL],
            "accepted_sources": [_URL],
            "model_used": "unit-test-verifier",
            "escalated": False,
            "web_search_requests": 1,
            "successful_source_count": 1,
            "search_error_count": 0,
            "correction": "It is 2025." if verdict == "CORRECTED" else None,
            "source_quote": quote,
        },
    }


def test_quote_gate_mirrors_the_verifier_parse_rule():
    assert _QUOTE_GATED_VERDICTS == ("CONFIRMED", "CORRECTED")
    assert set(_QUOTE_GATED_VERDICTS) < set(_CITATION_GATED_VERDICTS)


@pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
@pytest.mark.parametrize("quote", ["", "   \n\t "])
def test_put_refuses_a_quote_less_grounded_verdict(verdict: str, quote: str):
    cache = VerificationCache()
    f = _finding()
    cache.put(f, cycle=DEFAULT_CYCLE, result=_result(verdict, quote=quote))
    assert cache.stats()["size"] == 0
    assert cache.get(f, cycle=DEFAULT_CYCLE) is None


@pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
def test_put_accepts_a_quoted_grounded_verdict(verdict: str):
    cache = VerificationCache()
    f = _finding()
    cache.put(f, cycle=DEFAULT_CYCLE, result=_result(verdict, quote="Section 1234 applies."))
    hit = cache.get(f, cycle=DEFAULT_CYCLE)
    assert hit is not None
    assert hit.verdict == verdict
    assert hit.source_quote == "Section 1234 applies."
    assert hit.cache_status == "hit"


def test_disputed_without_a_quote_is_still_cacheable():
    """Documents the deliberate exclusion — same rule as the verifier."""
    cache = VerificationCache()
    f = _finding()
    cache.put(f, cycle=DEFAULT_CYCLE, result=_result("DISPUTED", quote=""))
    hit = cache.get(f, cycle=DEFAULT_CYCLE)
    assert hit is not None and hit.verdict == "DISPUTED"


def test_load_drops_a_hand_written_quote_less_row(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    payload = {
        "version": _CACHE_SCHEMA_VERSION,
        "saved_at": time.time(),
        "entries": {
            "quote_less_confirmed": _row("CONFIRMED", quote=""),
            "quote_less_corrected": _row("CORRECTED", quote=""),
            "quoted_confirmed": _row("CONFIRMED", quote="Section 1234 applies."),
            "quote_less_disputed": _row("DISPUTED", quote=""),
        },
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    cache = VerificationCache()
    loaded = cache.load_from_disk(path=cache_path)

    # The compliant CONFIRMED and the (not quote-gated) DISPUTED survive.
    assert loaded == 2
    assert cache.stats()["size"] == 2
    assert "quote_less_confirmed" not in cache._entries
    assert "quote_less_corrected" not in cache._entries
    assert "quoted_confirmed" in cache._entries
    assert "quote_less_disputed" in cache._entries


def test_round_trip_keeps_a_compliant_row(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    cache = VerificationCache()
    f = _finding()
    cache.put(f, cycle=DEFAULT_CYCLE, result=_result("CONFIRMED", quote="verbatim snippet"))
    cache.save_to_disk(path=cache_path)

    reloaded = VerificationCache()
    assert reloaded.load_from_disk(path=cache_path) == 1
    hit = reloaded.get(f, cycle=DEFAULT_CYCLE)
    assert hit is not None and hit.source_quote == "verbatim snippet"
