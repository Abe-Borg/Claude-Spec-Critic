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
ground a verified verdict. DISPUTED sits under the same gate as CONFIRMED /
CORRECTED (A-4): it is the verdict that tells a reviewer to discard a
finding, so an uncited one is downgraded exactly like an uncited CONFIRMED.

Section 4 covers the multi-wave case (A-3): a finding that does its
searching in wave 1, ``pause_turn``s, and emits the verdict in wave 2 must be
judged on the WHOLE conversation — the wave loop carries every prior wave's
blocks and counters into the continuation context, and
``_classify_wave_results`` runs the gate / collectors / counters over them —
matching the real-time loop's ``all_responses`` semantics.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src.batch.batch import BatchJob
from src.core.api_config import web_search_max_uses_for_severity
from src.core.code_cycles import DEFAULT_CYCLE
from src.output.report_status import ReportStatus, classify_status, is_budget_exhausted
from src.review.reviewer import Finding
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    collect_verification_batch_results,
)
from tests.fixtures.fake_anthropic import (
    FakeBatchResult,
    FakeBatchResultEnvelope,
    FakeMessage,
    FakeServerToolUsage,
    FakeServerToolUseBlock,
    FakeToolUseBlock,
    FakeUsage,
    FakeWebSearchResultBlock,
    batch_verification_result,
    pause_turn_response,
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


def _classify_with_contexts(monkeypatch, contexts: dict, message, *, finding: Finding | None = None):
    """Drive the REAL ``_classify_wave_results`` for one finding under the
    given request context(s) — every context id resolves to ``message``.

    Patches only the batch-retrieval primitive (no network); everything else
    — gate, parse, grounding partition, invariant — runs as in production.
    Returns the single ``VerificationItemOutcome``.
    """
    import src.verification.verifier as V

    finding = finding or _finding()
    job = BatchJob(
        batch_id="grounding-test",
        job_type="verify",
        request_map={cid: {"model": INIT_MODEL} for cid in contexts},
        created_at=0.0,
    )

    def fake_retrieve(_job):
        return {cid: batch_verification_result(cid, message=message) for cid in contexts}

    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    outcomes = V._classify_wave_results(
        job=job, findings=[finding], request_contexts=contexts
    )
    assert len(outcomes) == 1
    return outcomes[0]


def _classify_one(monkeypatch, message, *, finding: Finding | None = None):
    """Single-wave driver: a first-wave context with no prior-wave state."""
    contexts = {"verify__0": {"finding_idx": 0, "model": INIT_MODEL, "escalated": False}}
    return _classify_with_contexts(monkeypatch, contexts, message, finding=finding)


# ---------------------------------------------------------------------------
# 1. Grounded verified verdicts survive the batch wave parser
# ---------------------------------------------------------------------------


class TestGroundedVerdictSurvives:
    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED", "DISPUTED"])
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
    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED", "DISPUTED"])
    def test_cited_but_unsearched_url_downgrades(self, monkeypatch, verdict):
        """Verdict cites a real-looking URL the search never retrieved → UNVERIFIED.

        This is the canonical "model invented a source" case. It must be
        caught on the batch wave path exactly as on real-time
        (``test_confirmed_with_only_invented_source_downgrades``). DISPUTED
        is under the same gate (A-4): an invented citation must not be
        allowed to discard a real finding.
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

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED", "DISPUTED"])
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


# ---------------------------------------------------------------------------
# 4. Continuation waves carry the prior waves' search evidence (A-3)
# ---------------------------------------------------------------------------
#
# A finding that ``pause_turn``s in wave 1 (after doing its searching) and
# emits the verdict in wave 2 (citing a wave-1 URL, with no new searches)
# used to be judged on wave 2's message alone: the search gate saw zero
# searches ("did not perform web search" → terminal / PARSE_ERROR) or the
# accepted-URL pool was empty and the grounded verdict was downgraded. The
# wave loop now carries every prior wave's plain-dict content blocks and
# server-tool counters (``prior_blocks`` / ``prior_usage``) in the
# continuation context, ``_classify_wave_results`` runs the gate, both
# evidence collectors, and the counters over prior + current, and the
# continuation request re-sends the whole accumulated assistant turn.

SECOND_SEARCHED_URL = "https://www.cbsc.ca.gov/adoption-matrix"


def _medium_finding() -> Finding:
    """MEDIUM (search budget 5) keeps the finding out of the post-loop
    escalation wave, so the loop tests exercise ONLY wave accounting."""
    return Finding(
        severity="MEDIUM",
        fileName="22 11 16 - Domestic Water.docx",
        section="2.3",
        issue="Cited California Plumbing Code edition is outdated for the 2025 cycle.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="CPC 2025",
        confidence=0.5,
    )


def _pause_with_search(url: str, *, searches: int):
    """Wave message: searched ``url`` (``searches`` requests), then paused."""
    return pause_turn_response(searched_urls=[url], web_search_requests=searches)


def _verdict_only(*, verdict: str, cited_sources: list[str]):
    """Wave message: the verdict tool call and NOTHING else — no search
    blocks, ``web_search_requests=0``. Judged alone it fails the search
    gate; judged with its prior waves it grounds on their URLs."""
    payload = sample_verification_verdict_payload(
        verdict=verdict, grounded_sources=cited_sources
    )
    return FakeMessage(
        content=[FakeToolUseBlock(name="submit_verification_verdict", input=dict(payload))],
        stop_reason="tool_use",
        usage=FakeUsage(
            server_tool_use=FakeServerToolUsage(web_search_requests=0, web_fetch_requests=0)
        ),
    )


def _plain(blocks) -> list[dict]:
    """The plain-dict form the wave parser stores in continuation state."""
    import src.verification.verifier as V

    return [b for b in (V._content_block_to_plain(rb) for rb in blocks) if b is not None]


def _search_result_urls(blocks) -> list[str]:
    """URLs inside the ``web_search_tool_result`` blocks of a plain block list."""
    urls: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "web_search_tool_result":
            for item in block.get("content") or []:
                if isinstance(item, dict) and item.get("url"):
                    urls.append(item["url"])
    return urls


def _usage(*, searches: int, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    return {
        "web_search_requests": searches,
        "web_fetch_requests": 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _continuation_context(*, prior_message, prior_searches: int) -> dict:
    """A wave-2 continuation context exactly as the wave loop stores it."""
    return {
        "verify_cont_1__verify__0": {
            "finding_idx": 0,
            "model": INIT_MODEL,
            "escalated": False,
            "original_custom_id": "verify__0",
            "prior_blocks": _plain(prior_message.content),
            "prior_usage": _usage(searches=prior_searches),
        }
    }


class TestClassifyWaveResultsWithPriorState:
    """The wave parser's accumulation contract, independent of the loop."""

    def test_no_prior_state_is_the_bare_message(self):
        """First wave / retry / escalation: the merged view is the message
        itself — the common path is byte-identical to before."""
        import src.verification.verifier as V

        msg = _wave_message(verdict="CONFIRMED", cited_sources=[SEARCHED_URL])
        assert V._conversation_view(msg, prior_blocks=[], prior_usage={}) is msg

    def test_verdict_only_wave_without_prior_state_fails_the_gate(self, monkeypatch):
        """Control: judged alone, a verdict-only wave is what the old code
        saw for every continuation — and it is (correctly) gated."""
        outcome = _classify_one(
            monkeypatch, _verdict_only(verdict="CONFIRMED", cited_sources=[SEARCHED_URL])
        )
        assert outcome.classification == "terminal_unverified"
        assert "did not perform web search" in (outcome.unverified_reason or "")
        # Message-derived terminal outcomes carry the (zero) counters.
        assert outcome.accumulated_usage == _usage(searches=0)

    def test_verdict_only_wave_grounds_on_prior_blocks(self, monkeypatch):
        contexts = _continuation_context(
            prior_message=_pause_with_search(SEARCHED_URL, searches=3), prior_searches=3
        )
        outcome = _classify_with_contexts(
            monkeypatch,
            contexts,
            _verdict_only(verdict="CONFIRMED", cited_sources=[SEARCHED_URL]),
            finding=_medium_finding(),
        )
        assert outcome.classification == "success"
        result = outcome.parsed_verification
        assert result.verdict == "CONFIRMED"
        assert result.grounded is True
        assert result.sources == [SEARCHED_URL]
        assert result.searched_sources == [SEARCHED_URL]
        # Counters are the running sum: 3 prior + 0 in this wave.
        assert result.web_search_requests == 3
        # Token usage sums prior (100/50) + the FakeUsage defaults (100/50).
        assert (result.input_tokens, result.output_tokens) == (200, 100)

    def test_pause_outcome_carries_accumulated_blocks_and_counters(self, monkeypatch):
        """A second pause hands the NEXT wave prior + current, not just current."""
        contexts = _continuation_context(
            prior_message=_pause_with_search(SEARCHED_URL, searches=2), prior_searches=2
        )
        outcome = _classify_with_contexts(
            monkeypatch,
            contexts,
            _pause_with_search(SECOND_SEARCHED_URL, searches=1),
            finding=_medium_finding(),
        )
        assert outcome.classification == "continue"
        assert _search_result_urls(outcome.assistant_content_blocks) == [
            SEARCHED_URL,
            SECOND_SEARCHED_URL,
        ]
        assert outcome.accumulated_usage == _usage(
            searches=3, input_tokens=200, output_tokens=100
        )


def _install_wave_router(monkeypatch, route):
    """Patch the batch primitives; ``route(custom_id) -> message`` decides
    each wave's response for that request id.

    Records every follow-up submission's request list so a test can inspect
    the continuation payload actually sent. ``verify_finding`` raises: the
    continuation must resolve inside the wave loop, never via real-time.
    """
    import src.verification.verifier as V

    recorded = {"submissions": []}
    lock = threading.Lock()
    counter = {"n": 0}

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(detached=False, poll_failed=False)

    def fake_retrieve(job):
        return {
            cid: FakeBatchResult(
                custom_id=cid,
                result=FakeBatchResultEnvelope(type="succeeded", message=route(cid)),
            )
            for cid in job.request_map
        }

    def fake_submit(requests, request_map, *, extra_headers=None):
        with lock:
            counter["n"] += 1
            n = counter["n"]
            recorded["submissions"].append(requests)
        return SimpleNamespace(
            batch_id=f"wave{n + 1}-batch", request_map=request_map, job_type="verify"
        )

    def fake_verify_finding(finding, **_kwargs):
        raise AssertionError("real-time path must not run: the continuation resolves in-batch")

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "verify_finding", fake_verify_finding)
    return recorded


def _collect_one(finding: Finding, *, max_waves: int = 3):
    job = SimpleNamespace(
        batch_id="init-batch",
        request_map={"verify__0": {"finding_idx": 0, "model": INIT_MODEL}},
        job_type="verify",
    )
    return collect_verification_batch_results(
        job,
        [finding],
        log=lambda *_a, **_k: None,
        progress=lambda _p, _m: None,
        cycle=DEFAULT_CYCLE,
        poll_policy=DEFAULT_VERIFICATION_POLL_POLICY,
        max_waves=max_waves,
        cache=None,
        realtime_fallback_threshold=0,
    )


def _wave_of(custom_id: str) -> int:
    """0 for the initial wave's id, N for the Nth continuation re-stamp
    (``verify_cont_2__verify_cont_1__verify__0`` → 2)."""
    return custom_id.count("verify_cont_")


def _continuation_assistant_blocks(submission) -> list:
    """The assistant turn re-sent in a follow-up wave's continuation request."""
    assert len(submission) == 1
    messages = submission[0]["params"]["messages"]
    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant) == 1, messages
    return list(assistant[0]["content"])


class TestContinuationWaveEvidenceCarried:
    """End-to-end through the REAL wave loop."""

    def test_verdict_after_pause_grounds_on_prior_wave_url(self, monkeypatch):
        """Wave 1: 3 searches, pause. Wave 2: verdict citing a wave-1 URL,
        no new searches → CONFIRMED with that URL, counters from wave 1,
        no gate failure."""
        finding = _medium_finding()

        def route(cid):
            if _wave_of(cid) == 0:
                return _pause_with_search(SEARCHED_URL, searches=3)
            return _verdict_only(verdict="CONFIRMED", cited_sources=[SEARCHED_URL])

        recorded = _install_wave_router(monkeypatch, route)
        _collect_one(finding)

        v = finding.verification
        assert v is not None
        assert v.verdict == "CONFIRMED"
        assert v.grounded is True
        assert v.verification_failed is False
        assert "did not perform web search" not in (v.explanation or "")
        assert v.sources == [SEARCHED_URL]
        assert v.accepted_sources == [SEARCHED_URL]
        assert v.searched_sources == [SEARCHED_URL]
        assert v.web_search_requests == 3
        assert classify_status(finding) is ReportStatus.VERIFIED_SUPPORTED
        assert is_budget_exhausted(finding) is False
        # Exactly one follow-up wave, whose continuation re-sent wave 1's
        # blocks (the API needs the prior turn to resume).
        assert len(recorded["submissions"]) == 1
        blocks = _continuation_assistant_blocks(recorded["submissions"][0])
        assert _search_result_urls(blocks) == [SEARCHED_URL]

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED", "DISPUTED"])
    def test_verdict_citing_url_no_wave_searched_still_downgrades(
        self, monkeypatch, verdict
    ):
        """Negative twin: the whole-conversation pool is still a *gate* —
        a citation no wave retrieved is rejected and the verdict downgraded."""
        finding = _medium_finding()

        def route(cid):
            if _wave_of(cid) == 0:
                return _pause_with_search(SEARCHED_URL, searches=3)
            return _verdict_only(verdict=verdict, cited_sources=[INVENTED_URL])

        _install_wave_router(monkeypatch, route)
        _collect_one(finding)

        v = finding.verification
        assert v is not None
        assert v.verdict == "UNVERIFIED"
        assert v.grounded is False
        # A clean grounding downgrade, not an operational failure.
        assert v.verification_failed is False
        assert v.sources == []
        assert v.accepted_sources == []
        assert INVENTED_URL in _rejected_urls(v)
        # Evidence / counters from wave 1 are still recorded for diagnostics.
        assert v.searched_sources == [SEARCHED_URL]
        assert v.web_search_requests == 3
        assert classify_status(finding) is ReportStatus.INSUFFICIENT_EVIDENCE

    def test_evidence_accumulates_across_multiple_continuations(self, monkeypatch):
        """Pause, pause, verdict: wave 3 sees BOTH prior waves (not just
        wave 2) — in its grounding pool, its counters, and the assistant
        turn re-sent in its continuation request."""
        finding = _medium_finding()

        def route(cid):
            wave = _wave_of(cid)
            if wave == 0:
                return _pause_with_search(SEARCHED_URL, searches=2)
            if wave == 1:
                return _pause_with_search(SECOND_SEARCHED_URL, searches=2)
            return _verdict_only(verdict="CONFIRMED", cited_sources=[SEARCHED_URL])

        recorded = _install_wave_router(monkeypatch, route)
        _collect_one(finding, max_waves=3)

        v = finding.verification
        assert v is not None
        assert v.verdict == "CONFIRMED"
        assert v.sources == [SEARCHED_URL]
        assert v.searched_sources == [SEARCHED_URL, SECOND_SEARCHED_URL]
        assert v.successful_source_count == 2
        assert v.web_search_requests == 4
        assert len(recorded["submissions"]) == 2
        wave2_blocks = _continuation_assistant_blocks(recorded["submissions"][0])
        wave3_blocks = _continuation_assistant_blocks(recorded["submissions"][1])
        assert _search_result_urls(wave2_blocks) == [SEARCHED_URL]
        assert _search_result_urls(wave3_blocks) == [SEARCHED_URL, SECOND_SEARCHED_URL]

    def test_budget_exhaustion_sees_the_whole_conversation(self, monkeypatch):
        """Wave 1 spends the full MEDIUM budget and pauses; wave 2 emits an
        UNVERIFIED with zero new searches → the accumulated count trips
        the ``>=`` budget check. Judged on wave 2 alone (0 searches) the
        sentinel could never fire."""
        budget = web_search_max_uses_for_severity("MEDIUM")
        assert budget > 0
        finding = _medium_finding()

        def route(cid):
            if _wave_of(cid) == 0:
                return _pause_with_search(SEARCHED_URL, searches=budget)
            return _verdict_only(verdict="UNVERIFIED", cited_sources=[])

        _install_wave_router(monkeypatch, route)
        _collect_one(finding)

        v = finding.verification
        assert v is not None
        assert v.verdict == "UNVERIFIED"
        assert v.verification_failed is False
        assert v.web_search_requests == budget
        assert v.budget_exhausted is True
        assert is_budget_exhausted(finding) is True
        assert classify_status(finding) is ReportStatus.INSUFFICIENT_EVIDENCE
