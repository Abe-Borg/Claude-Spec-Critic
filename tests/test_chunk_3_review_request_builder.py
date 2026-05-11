"""Chunk 3 — central review request builder and exact token preflight.

Verifies the user-visible contracts laid out in the plan:

* The same builder backs real-time review, batch review, and preflight,
  so the shape we count is the shape we send.
* ``pre_detected_alerts`` flow through the request shape used for token
  preflight — a spec with a small body but a large alert block is
  counted correctly.
* The cache key changes when the pre-detected alerts change, so a
  cached count is never reused across an alert-set change.
* Reordering files does not let a smaller raw spec bypass the exact-
  count selection when its alert block makes the real request larger.
* When ``pre_detected_alerts`` is empty / None, behavior is byte-stable
  with the legacy message shape (no regression on the no-alerts path).
"""
from __future__ import annotations

from typing import Any

import pytest

from src.api_config import MODEL_OPUS_47
from src.code_cycles import DEFAULT_CYCLE
from src.extractor import ExtractedSpec
from src.review_modes import DEFAULT_REVIEW_MODE
from src.review_request_builder import (
    ReviewRequestSpec,
    build_review_request,
    build_token_count_request,
    build_user_message,
    estimate_local_request_tokens,
    review_request_cache_key,
)


# ---------------------------------------------------------------------------
# Hermetic fixtures: stub the tiktoken-backed counter so tests stay offline.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_count_tokens(monkeypatch):
    def _fake(text: str | None) -> int:
        return len((text or "").split()) * 2

    monkeypatch.setattr("src.tokenizer.count_tokens", _fake)
    monkeypatch.setattr(
        "src.review_request_builder.count_tokens", _fake, raising=False
    )
    monkeypatch.setattr("src.pipeline.count_tokens", _fake, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(content: str = "Spec body.", filename: str = "23 21 13.docx") -> ExtractedSpec:
    return ExtractedSpec(
        filename=filename,
        content=content,
        word_count=len(content.split()),
        source_path="",
        source_format="docx",
        paragraph_map=None,
    )


def _alert(filename: str, rule: str, match: str) -> dict[str, Any]:
    """Minimal alert dict shaped like the ones from :mod:`src.preprocessor`.

    The renderer reads ``deterministic_rule`` to group entries and
    ``match`` for the example text — must match those keys, not the
    free-form names used in some other tests.
    """
    return {
        "filename": filename,
        "type": rule.replace("_", " "),
        "match": match,
        "context": match,
        "position": 0,
        "deterministic_rule": rule,
    }


def _request(spec: ExtractedSpec, **overrides) -> ReviewRequestSpec:
    base = dict(
        spec_content=spec.content,
        filename=spec.filename,
        model=MODEL_OPUS_47,
        cycle=DEFAULT_CYCLE,
        mode=DEFAULT_REVIEW_MODE,
        paragraph_map=spec.paragraph_map,
    )
    base.update(overrides)
    return ReviewRequestSpec(**base)


# ---------------------------------------------------------------------------
# Pre-detected alerts flow into the request shape
# ---------------------------------------------------------------------------


class TestPreDetectedAlertsAreCounted:
    def test_user_message_includes_pre_detected_block(self):
        spec = _spec(content="LEED Gold project body.", filename="a.docx")
        alerts = [_alert("a.docx", "leed_reference", "LEED Gold")]
        rs = _request(spec, pre_detected_alerts=alerts)
        msg = build_user_message(rs)
        # The alert text appears in the body so the model is told what
        # was already detected locally. The exact wrapper tag is verified
        # in the Chunk D4.1 tests; here we just need to confirm the
        # contents flow through the builder.
        assert "LEED Gold" in msg
        assert "<pre_detected>" in msg

    def test_alerts_increase_local_estimate_over_same_body(self):
        """Adding pre-detected alerts must increase the local estimate
        for the same spec body. Before Chunk 3 the preflight ignored
        the alert block entirely and the two estimates were identical.
        """
        spec = _spec(content="Spec body.", filename="x.docx")
        bare = _request(spec, pre_detected_alerts=None)
        # The renderer groups by ``deterministic_rule``, so distinct
        # rule names enlarge the block linearly. Use a small variety —
        # all rules listed are real ids from ``preprocessor.py``.
        alerts = [
            _alert("x.docx", "leed_reference", "LEED Gold"),
            _alert("x.docx", "placeholder", "[INSERT NAME]"),
            _alert("x.docx", "stale_code_cycle", "2019 CBC"),
            _alert("x.docx", "template_marker", "TODO: update"),
            _alert("x.docx", "duplicate_paragraph", "duplicated body"),
        ]
        with_alerts = _request(spec, pre_detected_alerts=alerts)

        bare_count = estimate_local_request_tokens(bare)
        with_count = estimate_local_request_tokens(with_alerts)
        assert with_count > bare_count, (
            f"Adding {len(alerts)} pre_detected alerts did not change the "
            f"local estimate (bare={bare_count}, with_alerts={with_count}). "
            f"The preflight is still missing the alert block — the original "
            f"Chunk 3 bug."
        )

    def test_small_body_with_alerts_can_outrank_larger_raw_body(self):
        """Plan task 7: reordering files must not cause a smaller raw
        spec to bypass exact-count when its alert block makes the real
        request larger.
        """
        small_body = "Tiny spec body."
        # A diverse alert mix: every distinct ``deterministic_rule`` adds
        # its own line in the rendered block (the renderer groups by
        # rule). Combined this overtakes a moderately larger raw body.
        many_alerts = [
            _alert("alerts.docx", "leed_reference", "LEED Gold target"),
            _alert("alerts.docx", "placeholder", "[INSERT VALUE A]"),
            _alert("alerts.docx", "stale_code_cycle", "2019 CBC reference"),
            _alert("alerts.docx", "template_marker", "TODO: replace section"),
            _alert("alerts.docx", "duplicate_paragraph", "duplicate paragraph A"),
            _alert("alerts.docx", "empty_section", "section heading only"),
            _alert("alerts.docx", "stale_asce7", "ASCE 7-10 reference"),
            _alert("alerts.docx", "invalid_code_cycle", "2018 CBC reference"),
            _alert("alerts.docx", "inconsistent_filename", "naming mismatch"),
            _alert("alerts.docx", "duplicate_heading", "duplicate heading"),
        ]
        small_with_alerts = _request(
            _spec(content=small_body, filename="alerts.docx"),
            pre_detected_alerts=many_alerts,
        )
        # The "larger" raw spec has a body just big enough that raw-body
        # ranking would pick it but full-shape ranking should not.
        larger_body = "moderate body text " * 8
        large_no_alerts = _request(
            _spec(content=larger_body, filename="no_alerts.docx"),
            pre_detected_alerts=None,
        )

        small_count = estimate_local_request_tokens(small_with_alerts)
        large_count = estimate_local_request_tokens(large_no_alerts)
        assert small_count > large_count, (
            f"Alert-heavy spec ranked below larger raw spec "
            f"(alerts={small_count}, no_alerts={large_count}); "
            f"preflight would exact-count the wrong candidate."
        )

    def test_no_alerts_path_is_byte_stable_with_legacy_message(self):
        """Passing ``None`` or ``[]`` for alerts produces the legacy shape."""
        spec = _spec(content="Body.", filename="x.docx")
        msg_none = build_user_message(_request(spec, pre_detected_alerts=None))
        msg_empty = build_user_message(_request(spec, pre_detected_alerts=[]))
        # No pre_detected block when the alerts list is empty / None.
        assert "<pre_detected>" not in msg_none
        assert "<pre_detected>" not in msg_empty
        # The two no-alerts shapes are identical, so a caller migrating
        # from None to [] (or vice versa) cannot bust the prompt cache.
        assert msg_none == msg_empty


# ---------------------------------------------------------------------------
# Cache key invariants
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_key_changes_when_pre_detected_alerts_change(self):
        spec = _spec(content="Body.", filename="x.docx")
        no_alerts = _request(spec, pre_detected_alerts=None)
        with_alert = _request(
            spec,
            pre_detected_alerts=[_alert("x.docx", "placeholder", "[INSERT]")],
        )
        assert review_request_cache_key(no_alerts) != review_request_cache_key(with_alert), (
            "Cache key must change when pre_detected_alerts change so a "
            "cached exact count cannot be reused after the alert set shifts."
        )

    def test_key_changes_when_alert_text_changes(self):
        spec = _spec(content="Body.", filename="x.docx")
        a = _request(
            spec,
            pre_detected_alerts=[_alert("x.docx", "placeholder", "[INSERT]")],
        )
        b = _request(
            spec,
            pre_detected_alerts=[_alert("x.docx", "placeholder", "[DIFFERENT]")],
        )
        assert review_request_cache_key(a) != review_request_cache_key(b)

    def test_key_changes_when_model_changes(self):
        spec = _spec(content="Body.", filename="x.docx")
        opus = _request(spec, model="claude-opus-4-7")
        sonnet = _request(spec, model="claude-sonnet-4-6")
        assert review_request_cache_key(opus) != review_request_cache_key(sonnet)

    def test_key_changes_for_batch_vs_realtime(self):
        spec = _spec(content="Body.", filename="x.docx")
        realtime = _request(spec, batch=False)
        batched = _request(spec, batch=True)
        assert review_request_cache_key(realtime) != review_request_cache_key(batched), (
            "Real-time and batch can use different output caps and "
            "different beta paths; their counts should not collide in cache."
        )

    def test_key_stable_for_identical_inputs(self):
        spec = _spec(content="Body.", filename="x.docx")
        a = _request(spec, pre_detected_alerts=None)
        b = _request(spec, pre_detected_alerts=None)
        assert review_request_cache_key(a) == review_request_cache_key(b)


# ---------------------------------------------------------------------------
# Built request shape: batch and preflight count the same thing
# ---------------------------------------------------------------------------


class TestBatchAndPreflightShareShape:
    def test_token_count_request_messages_match_built_request(self):
        spec = _spec(content="Body.", filename="x.docx")
        rs = _request(
            spec,
            pre_detected_alerts=[_alert("x.docx", "leed_reference", "LEED")],
            batch=True,
        )
        built = build_review_request(rs)
        _, count_kwargs = build_token_count_request(rs)
        assert count_kwargs["messages"] == built.params["messages"], (
            "Token-count request must use the same messages array as the "
            "submission request — drift here is the original Chunk 3 bug."
        )

    def test_token_count_request_strips_cache_control_from_system(self):
        """``count_tokens`` accepts the raw prompt; cache_control is a
        pricing hint that the count endpoint does not need."""
        rs = _request(_spec(), batch=True)
        built = build_review_request(rs)
        _, count_kwargs = build_token_count_request(rs)
        # The submission shape may carry a cache-wrapped system list; the
        # count shape uses the raw string so the request shape is portable
        # across SDK versions.
        assert isinstance(count_kwargs["system"], str)
        assert count_kwargs["system"] == built.system_prompt

    def test_token_count_request_carries_tools_when_structured_enabled(self):
        rs = _request(_spec(), batch=True)
        _, count_kwargs = build_token_count_request(rs)
        # With structured tool output on (default), the count must include
        # the tool schema so the count reflects what we will send.
        assert "tools" in count_kwargs
        assert any(
            (t.get("name") or "") == "submit_review_findings"
            for t in count_kwargs["tools"]
        )

    def test_batch_request_has_max_tokens(self):
        rs = _request(_spec(), batch=True)
        built = build_review_request(rs)
        # The builder owns the per-call output cap. A missing field here
        # used to surface as a 400 deep in the request lifecycle.
        assert built.params["max_tokens"] > 0

    def test_realtime_request_never_has_service_tier(self):
        """Service tier is batch-only; the streaming path does not set it."""
        rs = _request(_spec(), batch=False)
        built = build_review_request(rs)
        assert "service_tier" not in built.params

    def test_realtime_request_never_extended_output(self):
        """Real-time cannot use the 300k beta header (not honored on stream)."""
        rs = _request(_spec(), batch=False, force_allow_extended_output=True)
        built = build_review_request(rs)
        assert built.allow_extended_output is False


# ---------------------------------------------------------------------------
# Preflight ranking: full local estimate, not raw body length
# ---------------------------------------------------------------------------


class TestPipelinePreflightUsesSharedShape:
    """End-to-end: pipeline._prepare_specs uses the same shape the batch
    path will submit. The fix for the original Chunk 3 bug is that
    ``pre_detected_alerts`` flow into the count_tokens request.
    """

    def _stub_pipeline(self, monkeypatch, *, specs, return_tokens: int = 100):
        """Wire up a stub Anthropic client + bypass extraction."""
        from pathlib import Path

        from src.extraction_cache import clear_token_cache

        clear_token_cache()

        captured: list[dict] = []

        class _Result:
            def __init__(self, total):
                self.input_tokens = total

        class _Messages:
            def count_tokens(self, **kwargs):
                captured.append(kwargs)
                return _Result(return_tokens)

        class _Client:
            messages = _Messages()

        client = _Client()
        monkeypatch.setattr("src.reviewer._get_client", lambda: client)
        monkeypatch.setattr(
            "src.pipeline.extract_multiple_specs_cached", lambda paths: specs
        )
        files = [Path(f"/tmp/{s.filename}") for s in specs]
        return captured, files

    def test_preflight_messages_include_pre_detected_block(self, monkeypatch):
        """Plan task 7: preflight counts the *real* message including the
        ``<pre_detected>`` alert block. Before Chunk 3 the alerts were
        re-derived inside ``submit_review_batch`` and ignored by preflight,
        so a small body with a heavy alert block could slip through."""
        from src import pipeline

        marker_filename = "leed.docx"
        # Force preprocess_spec to emit a recognizable alert so we can
        # find its match text in the count_tokens kwargs.
        spec = _spec(content="LEED Gold construction project.", filename=marker_filename)
        captured, files = self._stub_pipeline(monkeypatch, specs=[spec])

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        monkeypatch.setenv("SPEC_CRITIC_PRE_DETECTED_ALERTS", "1")
        monkeypatch.setattr(
            "src.pipeline.get_cached_token_count", lambda key: None
        )
        monkeypatch.setattr(
            "src.pipeline.cache_token_count", lambda key, value: None
        )

        pipeline._prepare_specs(
            input_dir=files[0].parent,
            files=files,
            model=MODEL_OPUS_47,
        )

        assert captured, "preflight made no count_tokens call"
        # The user message in the count_tokens call must include the
        # pre_detected block — the body contains ``LEED`` and the
        # preprocessor's leed_reference rule will fire, so the block
        # should appear in the user message we count.
        user_msg = captured[0]["messages"][0]["content"]
        assert "<pre_detected>" in user_msg, (
            "Preflight count did not include the <pre_detected> block. "
            "The original Chunk 3 bug: preflight measured a different "
            "shape than the batch path submits."
        )

    def test_preflight_carries_tools_when_structured_enabled(self, monkeypatch):
        """The structured submit_review_findings tool schema adds real
        input tokens; preflight must include it so the count is not an
        underestimate."""
        from src import pipeline

        spec = _spec(content="Body.", filename="x.docx")
        captured, files = self._stub_pipeline(monkeypatch, specs=[spec])

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", "1")
        monkeypatch.setattr(
            "src.pipeline.get_cached_token_count", lambda key: None
        )
        monkeypatch.setattr(
            "src.pipeline.cache_token_count", lambda key, value: None
        )

        pipeline._prepare_specs(
            input_dir=files[0].parent,
            files=files,
            model=MODEL_OPUS_47,
        )

        assert captured
        tools = captured[0].get("tools") or []
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        assert "submit_review_findings" in names, (
            "Preflight did not pass the structured-tool schema; the count "
            "is missing the tool definition's tokens."
        )


class TestPreflightRanking:
    def test_reordering_files_does_not_skip_alert_heavy_spec(self):
        """Plan task 7: a smaller raw spec must not bypass exact-count
        when its alert block makes the real request larger."""
        from src.pipeline import _PREFLIGHT_EXACT_COUNT_TOP_K

        # Build N specs where one has a small body but many alerts, and
        # the rest have moderately larger bodies with no alerts. With
        # raw-body ranking the alert-heavy spec would never be picked.
        n = _PREFLIGHT_EXACT_COUNT_TOP_K * 3
        request_specs = []
        for i in range(n - 1):
            request_specs.append(
                _request(
                    _spec(
                        content="moderate body " * 15,
                        filename=f"plain_{i}.docx",
                    ),
                    pre_detected_alerts=None,
                    batch=True,
                )
            )
        alert_heavy = _request(
            _spec(content="tiny body", filename="alert_heavy.docx"),
            pre_detected_alerts=[
                _alert("alert_heavy.docx", "placeholder", f"[VAL{i:03d}]")
                for i in range(40)
            ],
            batch=True,
        )
        request_specs.append(alert_heavy)

        # Now rank by full local estimate the same way preflight does.
        ranked = sorted(
            request_specs,
            key=estimate_local_request_tokens,
            reverse=True,
        )
        # The alert-heavy spec must be in the top-K candidates.
        top = ranked[:_PREFLIGHT_EXACT_COUNT_TOP_K]
        assert alert_heavy in top, (
            "Alert-heavy spec was not selected for exact counting; "
            "the preflight rank is still going by raw body length."
        )
