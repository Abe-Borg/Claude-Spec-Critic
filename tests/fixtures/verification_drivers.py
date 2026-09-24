"""Drive both verification transports with scripted responses (hermetic).

Plan WP-10 asks that "the same malformed response fails in batch and
real-time modes", so its tests feed one scripted message through each
transport's real code path and compare the results:

* :func:`run_realtime` — ``verifier.verify_finding`` with ``_get_client``
  patched to a streaming client that answers from a route;
* :func:`run_batch` — ``verifier.collect_verification_batch_results`` with the
  batch primitives (poll / retrieve / follow-up submit) patched, so the wave
  loop, the wave parser, and the terminal stamping all run for real.

The message builders produce SDK-shaped fakes (``tests/fixtures/
fake_anthropic.py``) for the shapes the contract distinguishes: search
evidence, fetch evidence, verdict tool calls (well-formed or not), text
bodies, and stop reasons. Nothing here touches the network or a real key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import src.verification.verifier as V
from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
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
)

SEARCHED_URL = "https://www.nfpa.org/codes-and-standards/nfpa-13"
FETCHED_URL = "https://codes.example.gov/fire/sprinkler-amendments"
QUOTE = "Sprinklers shall be spaced not more than 15 ft apart for light hazard."

#: The usage every scripted message reports, so a test can check that a
#: failure keeps it (plan WP-10: a failure is not free).
INPUT_TOKENS = 1_234
OUTPUT_TOKENS = 567
CACHE_WRITE_TOKENS = 2_048
CACHE_READ_TOKENS = 4_096


def medium_finding(**overrides) -> Finding:
    """A MEDIUM finding with a code reference.

    The reference keeps the keyword classifier from local-skipping it (so the
    real-time path calls the API), and MEDIUM keeps it out of the escalation
    tier on both transports, so each makes exactly one verification call.
    """
    fields = dict(
        severity="MEDIUM",
        fileName="21 13 13 - Wet-Pipe Sprinkler.docx",
        section="3.2",
        issue="Sprinkler spacing cited exceeds the maximum for the listed hazard.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §10.2.4",
        confidence=0.5,
    )
    fields.update(overrides)
    return Finding(**fields)


def usage(*, searches: int = 2, fetches: int = 0) -> FakeUsage:
    return FakeUsage(
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
        cache_creation_input_tokens=CACHE_WRITE_TOKENS,
        cache_read_input_tokens=CACHE_READ_TOKENS,
        server_tool_use=FakeServerToolUsage(
            web_search_requests=searches, web_fetch_requests=fetches
        ),
    )


def search_blocks(url: str = SEARCHED_URL) -> list:
    """One web_search call and its successful result."""
    return [
        FakeServerToolUseBlock(name="web_search", input={"query": "NFPA 13 spacing"}),
        FakeWebSearchResultBlock(
            content=[
                {
                    "type": "web_search_result",
                    "url": url,
                    "title": "NFPA 13",
                    "encrypted_content": "fake-encrypted-blob",
                }
            ]
        ),
    ]


def failed_search_blocks() -> list:
    """One web_search call whose result is an error."""
    return [
        FakeServerToolUseBlock(name="web_search", input={"query": "NFPA 13 spacing"}),
        FakeWebSearchResultBlock(
            content=[{"type": "web_search_tool_result_error", "error_code": "unavailable"}]
        ),
    ]


@dataclass
class FakeFetchResultBlock:
    """Mimic the ``web_fetch_tool_result`` block (a single document object)."""

    content: dict = field(default_factory=dict)
    tool_use_id: str = "srvtoolu_fetch_1"
    type: str = "web_fetch_tool_result"


def fetch_blocks(url: str = FETCHED_URL) -> list:
    """One web_fetch call and its successful result — and no search at all."""
    return [
        FakeServerToolUseBlock(name="web_fetch", input={"url": url}, id="srvtoolu_fetch_1"),
        FakeFetchResultBlock(
            content={
                "type": "web_fetch_result",
                "url": url,
                "content": {"type": "document", "source": {"type": "text", "data": "…"}},
            }
        ),
    ]


def verdict_payload(verdict: Any = "CONFIRMED", **overrides) -> dict:
    payload = {
        "verdict": verdict,
        "explanation": "Checked against the retrieved standard.",
        "sources": [SEARCHED_URL],
        "correction": None,
        "source_quote": QUOTE,
    }
    payload.update(overrides)
    return payload


def verdict_call(tool_input: Any, *, block_id: str = "toolu_verdict_1") -> FakeToolUseBlock:
    return FakeToolUseBlock(name="submit_verification_verdict", input=tool_input, id=block_id)


def message(
    blocks: list,
    *,
    stop_reason: Any = "tool_use",
    searches: int = 2,
    fetches: int = 0,
    stop_details: Any = None,
) -> FakeMessage:
    msg = FakeMessage(content=list(blocks), stop_reason=stop_reason, usage=usage(searches=searches, fetches=fetches))
    if stop_details is not None:
        msg.stop_details = stop_details
    return msg


# ---------------------------------------------------------------------------
# Real-time transport
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, outcome):
        self._outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class ScriptedStreamClient:
    """``client.messages.stream`` answering from ``route(kwargs)``.

    ``route`` returns a message, or an exception instance to raise from the
    stream (a transport failure). Every call's kwargs are recorded.
    """

    def __init__(self, route: Callable[[dict], Any]):
        self._route = route
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._route(kwargs))


def run_realtime(
    monkeypatch,
    route: Callable[[dict], Any] | Any,
    *,
    finding: Finding | None = None,
    cache=None,
    max_retries: int = 0,
    cycle=DEFAULT_CYCLE,
) -> tuple[VerificationResult, ScriptedStreamClient]:
    """Verify ``finding`` in real time; ``route`` may be a bare message."""
    if not callable(route):
        scripted = route
        route = lambda _kwargs: scripted  # noqa: E731
    client = ScriptedStreamClient(route)
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    # A retry must not actually sleep in a test.
    monkeypatch.setattr(V.time, "sleep", lambda _s: None)
    result = V.verify_finding(
        finding or medium_finding(), max_retries=max_retries, cycle=cycle, cache=cache
    )
    return result, client


# ---------------------------------------------------------------------------
# Batch transport
# ---------------------------------------------------------------------------


def run_batch(
    monkeypatch,
    route: Callable[[str], Any] | Any,
    *,
    finding: Finding | None = None,
    cache=None,
    max_waves: int = 1,
    cycle=DEFAULT_CYCLE,
) -> Finding:
    """Collect one finding through the batch wave loop.

    ``route(custom_id)`` returns each wave's message (or a bare message for
    every wave). The real-time fallback is disabled, so the result is the
    batch path's own.
    """
    if not callable(route):
        scripted = route
        route = lambda _cid: scripted  # noqa: E731

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

    counter = {"n": 0}

    def fake_submit(requests, request_map, *, extra_headers=None):
        counter["n"] += 1
        return SimpleNamespace(
            batch_id=f"wave{counter['n'] + 1}-batch", request_map=request_map, job_type="verify"
        )

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)

    target = finding or medium_finding()
    job = SimpleNamespace(
        batch_id="init-batch",
        request_map={"verify__0": {"finding_idx": 0}},
        job_type="verify",
    )
    collect_verification_batch_results(
        job,
        [target],
        log=lambda *_a, **_k: None,
        progress=lambda _p, _m: None,
        cycle=cycle,
        poll_policy=DEFAULT_VERIFICATION_POLL_POLICY,
        max_waves=max_waves,
        cache=cache,
        realtime_fallback_threshold=0,
    )
    assert target.verification is not None
    return target
