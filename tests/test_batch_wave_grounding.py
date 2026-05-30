"""Batch-wave grounding parity (TRUST_AUDIT P0-5).

The real-time verification path runs every parsed verdict through
``_apply_source_grounding`` + ``_enforce_grounding_invariant``, so a
``CONFIRMED`` / ``CORRECTED`` that cites a URL the ``web_search`` tool never
actually retrieved is downgraded to ``UNVERIFIED``. Batch verification is the
**default, highest-volume route** for verification, and
``_classify_wave_results`` is its parser.

``tests/test_source_grounding_invariant.py::TestBatchAndRealtimePathParity``
already proves the two grounding *helpers* are deterministic — but it calls
them directly. It does **not** drive ``_classify_wave_results``, so a refactor
that dropped the grounding calls from the batch wave parser would not fail
that test. These tests close that gap: they feed a fake batch verdict through
the real ``_classify_wave_results`` and assert that batch-path grounding is
byte-for-byte equivalent to the real-time gate — an ungrounded verified
verdict is downgraded, a grounded one survives.

The key distinction these tests exercise is *searched* URL vs. *cited* URL:
the fake batch message's ``web_search_tool_result`` block retrieves
:data:`SEARCHED_URL`, while the structured verdict payload cites whatever the
test passes. Only a cited URL that also appears in the searched pool may
ground a verified verdict.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.batch.batch import BatchJob
from src.core.code_cycles import DEFAULT_CYCLE  # noqa: F401  (kept for parity with sibling tests)
from src.review.reviewer import Finding
from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeServerToolUseBlock,
    FakeToolUseBlock,
    FakeWebSearchResultBlock,
    batch_verification_result,
    sample_verification_verdict_payload,
)

INIT_MODEL = "claude-sonnet-4-6"

# The URL the fake batch message's web_search tool actually "retrieved".
# A verified verdict may only ground on a cited URL that appears here.
SEARCHED_URL = "https://www.dgs.ca.gov/DSA/"
# A plausible-looking URL the model could cite but that was never searched —
# the canonical "model invented a real-looking source" case.
INVENTED_URL = "https://invented.example.com/fake-bulletin"


def _finding() -> Finding:
    """A substantive HIGH finding that routes to a web-search mode.

    Severity drives the search budget (HIGH → 7), comfortably above the two
    searches the fake message reports, so the ``budget_exhausted`` sentinel
    never fires and can't confound the grounding assertions.
    """
    return Finding(
        severity="HIGH",
        fileName="23 21 13 - Hydronic.docx",
        section="2.1",
        issue="Cited California Plumbing Code edition is outdated for the 2025 cycle.",
        actionType="EDIT",
        existingText="per CPC 2022",
        replacementText="per CPC 2025",
        codeReference="CPC 2025",
        confidence=0.6,
    )


def _usage_with_search(n: int = 2):
    """Usage object whose ``server_tool_use.web_search_requests`` passes the gate.

    ``_search_gate_failure`` requires BOTH a successful
    ``web_search_tool_result`` block AND ``usage.server_tool_use``
    ``.web_search_requests > 0``; the default ``FakeUsage`` omits the latter,
    so every wave message in this module attaches this.
    """
    return SimpleNamespace(
        input_tokens=120,
        output_tokens=60,
        server_tool_use=SimpleNamespace(web_search_requests=n, web_fetch_requests=0),
    )


def _wave_message(*, verdict: str, cited_sources: list[str], searched_url: str = SEARCHED_URL):
    """Build a batch verification message: search retrieved ``searched_url``,
    verdict cites ``cited_sources``."""
    payload = sample_verification_verdict_payload(
        verdict=verdict, grounded_sources=cited_sources
    )
    content = [
        FakeServerToolUseBlock(
            name="web_search",
            input={"query": "California Plumbing Code 2025 effective date"},
        ),
        FakeWebSearchResultBlock(
            content=[
                {
                    "type": "web_search_result",
                    "url": searched_url,
                    "title": "DSA — California Code Adoptions",
                    "encrypted_content": "fake-encrypted-blob",
                }
            ]
        ),
        FakeToolUseBlock(name="submit_verification_verdict", input=dict(payload)),
    ]
    msg = FakeMessage(content=content, stop_reason="tool_use")
    msg.usage = _usage_with_search()
    return msg


def _rejected_urls(result) -> set[str]:
    """Normalize ``rejected_sources`` to a set of URLs.

    The grounding partition records rejects as ``{"url": ..., "reason": ...}``
    dicts; tolerate a bare-string shape too so the assertion is robust to
    whichever serialization the result carries.
    """
    urls: set[str] = set()
    for r in result.rejected_sources or []:
        urls.add(r.get("url") if isinstance(r, dict) else r)
    return urls


def _classify_one(monkeypatch, message, *, finding: Finding | None = None):
    """Drive the REAL ``_classify_wave_results`` for a single finding/message.

    Patches only the batch-retrieval primitive (no network); everything else
    — gate, parse, grounding partition, invariant — runs as in production.
    Returns the single ``VerificationItemOutcome``.
    """
    import src.verification.verifier as V

    finding = finding or _finding()
    custom_id = "verify__0"
    job = BatchJob(
        batch_id="grounding-test",
        job_type="verify",
        request_map={custom_id: {"model": INIT_MODEL}},
        created_at=0.0,
    )
    contexts = {custom_id: {"finding_idx": 0, "model": INIT_MODEL, "escalated": False}}

    def fake_retrieve(_job):
        return {custom_id: batch_verification_result(custom_id, message=message)}

    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    outcomes = V._classify_wave_results(
        job=job, findings=[finding], request_contexts=contexts
    )
    assert len(outcomes) == 1
    return outcomes[0]


# ---------------------------------------------------------------------------
# 1. Grounded verified verdicts survive the batch wave parser
# ---------------------------------------------------------------------------


class TestGroundedVerdictSurvives:
    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_cited_searched_url_stays_verified(self, monkeypatch, verdict):
        """A verdict that cites the URL the search actually retrieved survives."""
        msg = _wave_message(verdict=verdict, cited_sources=[SEARCHED_URL])
        outcome = _classify_one(monkeypatch, msg)

        assert outcome.classification == "success"
        result = outcome.parsed_verification
        assert result is not None
        assert result.verdict == verdict
        assert result.grounded is True
        # The accepted pool is the cited∩searched intersection: exactly the
        # one URL that was both cited and retrieved.
        assert result.accepted_sources == [SEARCHED_URL]
        assert result.sources == [SEARCHED_URL]
        assert result.rejected_sources == []
        # Search budget (HIGH=7) not exhausted by 2 searches.
        assert result.budget_exhausted is False


# ---------------------------------------------------------------------------
# 2. Ungrounded verified verdicts are DOWNGRADED on the batch path
#    (the core trust property — identical to the real-time gate)
# ---------------------------------------------------------------------------


class TestUngroundedVerdictDowngraded:
    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_cited_but_unsearched_url_downgrades(self, monkeypatch, verdict):
        """Verdict cites a real-looking URL the search never retrieved → UNVERIFIED.

        This is the canonical "model invented a source" case. It must be
        caught on the batch wave path exactly as on real-time
        (``test_confirmed_with_only_invented_source_downgrades``).
        """
        msg = _wave_message(verdict=verdict, cited_sources=[INVENTED_URL])
        outcome = _classify_one(monkeypatch, msg)

        assert outcome.classification == "success"
        result = outcome.parsed_verification
        assert result is not None
        # Downgraded — the verified verdict did not survive ungrounded.
        assert result.verdict == "UNVERIFIED"
        assert result.grounded is False
        # No cited URL was retrieved, so nothing is accepted and the invented
        # URL is recorded as rejected (audit trail for the downgrade).
        assert result.accepted_sources == []
        assert result.sources == []
        assert INVENTED_URL in _rejected_urls(result)

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_no_citations_with_search_downgrades_via_invariant(self, monkeypatch, verdict):
        """Search ran successfully but the verdict cites nothing → UNVERIFIED.

        ``_apply_source_grounding`` has no cited URL to reject, so the
        downgrade here is driven by ``_enforce_grounding_invariant``. Proving
        it fires on the wave path confirms the invariant call at
        ``verifier.py`` is actually reached, not just present.
        """
        msg = _wave_message(verdict=verdict, cited_sources=[])
        outcome = _classify_one(monkeypatch, msg)

        result = outcome.parsed_verification
        assert result is not None
        assert result.verdict == "UNVERIFIED"
        assert result.accepted_sources == []
        # The successful search is still recorded for diagnostics even though
        # the verdict was downgraded.
        assert result.searched_sources == [SEARCHED_URL]


# ---------------------------------------------------------------------------
# 3. A mixed citation list keeps only the grounded URL
# ---------------------------------------------------------------------------


class TestMixedCitationsPartitioned:
    def test_one_grounded_one_invented_stays_verified_with_grounded_only(
        self, monkeypatch
    ):
        """One real + one invented citation → verdict survives, invented dropped.

        At least one accepted citation satisfies the grounding invariant, so
        the verdict stands; the report/cache must still never carry the
        invented URL.
        """
        msg = _wave_message(
            verdict="CONFIRMED", cited_sources=[SEARCHED_URL, INVENTED_URL]
        )
        outcome = _classify_one(monkeypatch, msg)

        result = outcome.parsed_verification
        assert result is not None
        assert result.verdict == "CONFIRMED"
        assert result.grounded is True
        assert result.accepted_sources == [SEARCHED_URL]
        # The invented URL is partitioned out of the trusted source list.
        assert SEARCHED_URL not in _rejected_urls(result)
        assert INVENTED_URL in _rejected_urls(result)
        assert INVENTED_URL not in result.sources
