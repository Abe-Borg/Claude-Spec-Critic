"""Opt-in live-API smoke tests (``@pytest.mark.network``).

The hermetic suite proves the *shape* of every request the app builds, but it
cannot prove the live API still *accepts* that shape under the current account,
model ids, tool versions, and beta gates. These smoke tests close that gap. They
are skipped by default (``tests/conftest.py`` only runs ``network`` tests when a
real ``ANTHROPIC_API_KEY`` is set) and are meant to be run by hand before a
release or after bumping the Anthropic SDK / model ids / web-tool versions:

    ANTHROPIC_API_KEY=sk-... python -m pytest -m network -q

**They make real, billable API calls.** Each is intentionally tiny, but the
verification smoke tests do enable the web_search / web_fetch server tools, so a
run will perform a small number of real searches.

Every request is built through the *production* builders
(``build_token_count_request`` / ``count_tokens_via_api`` /
``select_routing`` + ``build_verification_request``) rather than hand-rolled
payloads, so what the smoke test sends is byte-for-byte what the app sends.
"""
from __future__ import annotations

import pytest

from src.core.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_48,
    MODEL_SONNET_46,
    PHASE_VERIFICATION,
    REVIEW_MODEL_DEFAULT,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.tokenizer import count_tokens_via_api
from src.review.review_request_builder import (
    ReviewRequestSpec,
    build_token_count_request,
)
from src.review.reviewer import Finding, _get_client
from src.verification.verification_routing import (
    build_verification_request,
    select_routing,
)
from src.verification.verifier import (
    _build_verification_prompt,
    _get_verification_system_prompt,
)

pytestmark = pytest.mark.network


# ---------------------------------------------------------------------------
# Shared fixtures: realistic-but-tiny inputs built through real builders
# ---------------------------------------------------------------------------


def _review_spec() -> ReviewRequestSpec:
    return ReviewRequestSpec(
        spec_content=(
            "PART 2 - PRODUCTS\n2.1 PIPING\n"
            "A. Provide hydronic piping per NFPA 13, 2019 edition.\n"
        ),
        filename="23 21 13 Hydronic Piping.docx",
        model=REVIEW_MODEL_DEFAULT,
        cycle=DEFAULT_CYCLE,
    )


def _standard_reasoning_finding() -> Finding:
    # MEDIUM + a code reference + a substantive technical claim routes to the
    # default STANDARD_REASONING mode — the common path that carries web_search,
    # web_fetch, adaptive thinking, effort, and the verdict tool together.
    return Finding(
        severity="MEDIUM",
        fileName="23 21 13 Hydronic Piping.docx",
        section="2.1",
        issue="Spec cites NFPA 13 2019 edition; the current California cycle pins a newer edition.",
        actionType="EDIT",
        existingText="NFPA 13, 2019 edition",
        replacementText="NFPA 13, current adopted edition",
        codeReference="NFPA 13",
    )


def _verification_request(*, include_service_tier: bool):
    finding = _standard_reasoning_finding()
    decision = select_routing(
        finding,
        escalated=False,
        local_skip=False,
        cache_phase=PHASE_VERIFICATION,
    )
    assert not decision.local_skip, "smoke finding unexpectedly routed to local_skip"
    return build_verification_request(
        decision,
        prompt=_build_verification_prompt(finding, cycle=DEFAULT_CYCLE),
        system_prompt=_get_verification_system_prompt(
            DEFAULT_CYCLE, include_verdict_tool=True
        ),
        include_service_tier=include_service_tier,
    ), decision


# ---------------------------------------------------------------------------
# 1. count_tokens — the preflight path
# ---------------------------------------------------------------------------


def test_count_tokens_smoke():
    """The exact-count preflight shape is accepted and returns a positive total."""
    _built, count_kwargs = build_token_count_request(_review_spec())
    total = count_tokens_via_api(**count_kwargs)
    assert total is not None, "count_tokens_via_api returned None (API rejected the shape?)"
    assert total > 0


# ---------------------------------------------------------------------------
# 2. Verification tool-shape — web_search + web_fetch + thinking + effort + verdict tool
# ---------------------------------------------------------------------------


def test_verification_tool_shape_smoke(monkeypatch):
    """A STANDARD_REASONING verification request is accepted live (lenient shape).

    This is the single most API-evolution-sensitive request the app makes: it
    combines the ``web_search_20260209`` and ``web_fetch_20260209`` server tools
    with adaptive thinking, output effort, and the structured verdict tool. If
    any of those is retired or mutually incompatible under the account, this
    raises at ``create`` time. Strict tool use is explicitly disabled here so
    this test pins the ``SPEC_CRITIC_STRICT_TOOL_USE=0`` rollback shape; the
    default (strict) shape is smoke test #3.
    """
    monkeypatch.setenv("SPEC_CRITIC_STRICT_TOOL_USE", "0")
    vr, _decision = _verification_request(include_service_tier=False)
    client = _get_client()
    resp = client.messages.create(**vr.params, extra_headers=vr.extra_headers or None)
    assert resp.id
    assert resp.stop_reason is not None


# ---------------------------------------------------------------------------
# 3. Strict tool use — strict:true + adaptive thinking + tools (the default shape)
# ---------------------------------------------------------------------------


def test_strict_tool_use_smoke(monkeypatch):
    """Same shape as #2 but with ``strict: true`` on the tool schemas — the default.

    ``SPEC_CRITIC_STRICT_TOOL_USE`` defaults ON, so this is the exact shape
    every production verification call sends out of the box. If it ever 400s
    after an SDK / model / account change, set ``SPEC_CRITIC_STRICT_TOOL_USE=0``
    (the lenient shape #2 pins) and investigate before re-enabling.
    """
    monkeypatch.delenv("SPEC_CRITIC_STRICT_TOOL_USE", raising=False)
    vr, _decision = _verification_request(include_service_tier=False)
    client = _get_client()
    resp = client.messages.create(**vr.params, extra_headers=vr.extra_headers or None)
    assert resp.id
    assert resp.stop_reason is not None


# ---------------------------------------------------------------------------
# 4. Message Batches — service_tier + the same per-item params shape
# ---------------------------------------------------------------------------


def test_batch_submit_smoke():
    """A one-item Message Batch with the production per-item params is accepted."""
    vr, _decision = _verification_request(include_service_tier=True)
    client = _get_client()
    create_kwargs = {
        "requests": [{"custom_id": "smoke__0", "params": vr.params}],
    }
    if vr.extra_headers:
        create_kwargs["extra_headers"] = vr.extra_headers
    mb = client.messages.batches.create(**create_kwargs)
    assert mb.id
    # Don't wait for processing — cancel best-effort so the smoke run is cheap.
    try:
        client.messages.batches.cancel(mb.id)
    except Exception:  # noqa: BLE001 - cancellation is best-effort cleanup
        pass


# ---------------------------------------------------------------------------
# 5. Model capability — the configured ids still exist on the account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", [MODEL_OPUS_48, MODEL_SONNET_46, MODEL_HAIKU_45])
def test_model_ids_exist_smoke(model_id):
    """Every configured model id resolves via the Models API.

    Catches a model retirement (the id stops resolving) before a real batch run
    discovers it deep in the request lifecycle.
    """
    client = _get_client()
    info = client.models.retrieve(model_id)
    assert getattr(info, "id", None)
