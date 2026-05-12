"""Chunk I tests — verification modes and model routing.

Three themes:

1. :mod:`src.verification_modes` — the ``VerificationMode`` enum,
   per-mode policy bundle, and pure-function router from a finding
   (+ escalation flag, + classifier verdict) to a mode.
2. Integration — :class:`VerificationResult` carries
   ``verification_mode``, the verifier real-time and batch wave
   paths stamp it correctly, the local-skip helper stamps
   ``local_skip``, the cache and resume-state serializers round-trip
   the field, and diagnostics counts per-mode events.
3. Representative routing cases (plan Chunk I Directive 6): low-
   severity editorial issue, simple stale-code/factual issue,
   high-severity code issue, internal coordination issue, source-
   disputed issue, prior verification cache hit.
"""
from __future__ import annotations

import importlib

import pytest

from src.reviewer import Finding


pytestmark = pytest.mark.verification_modes


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _finding(
    *,
    severity: str = "MEDIUM",
    code_ref: str | None = None,
    issue: str = "Generic claim",
    existing: str | None = None,
    replacement: str | None = None,
    section: str = "2.1",
    action: str = "EDIT",
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
# 1. Per-mode policy
# ===========================================================================


class TestModePolicy:
    def test_local_skip_policy(self):
        from src.verification_modes import VerificationMode, mode_policy
        p = mode_policy(VerificationMode.LOCAL_SKIP)
        assert p.mode is VerificationMode.LOCAL_SKIP
        assert p.model == "local"
        assert p.thinking_enabled is False
        assert p.search_budget_multiplier == 0.0
        assert p.web_search_enabled is False
        assert p.allows_escalation is False

    def test_strict_structured_policy(self):
        from src.verification_modes import VerificationMode, mode_policy
        from src.api_config import MODEL_SONNET_46
        p = mode_policy(VerificationMode.STRICT_STRUCTURED)
        # Sonnet by default — the cheap mode stays on the cheaper model
        # even when the operator flips the everywhere-Opus override.
        assert p.model == MODEL_SONNET_46
        # Plan Directive 3: "Prefer no thinking" for strict structured.
        assert p.thinking_enabled is False
        assert 0 < p.search_budget_multiplier < 1
        # Web search still attaches; the floor-of-1 inside the search
        # budget helper guarantees the model can issue at least one.
        assert p.web_search_enabled is True
        # Cheap mode does not escalate — that's the whole point.
        assert p.allows_escalation is False

    def test_standard_reasoning_policy(self, monkeypatch):
        """Default initial pass — Sonnet + thinking + full profile budget."""
        # Pin Sonnet-default so the model assertion is deterministic in
        # this test regardless of the host machine's env.
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "1")
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
        import src.api_config as api_config
        importlib.reload(api_config)
        import src.verification_modes as modes_module
        importlib.reload(modes_module)
        p = modes_module.mode_policy(modes_module.VerificationMode.STANDARD_REASONING)
        assert p.model == api_config.MODEL_SONNET_46
        assert p.thinking_enabled is True
        assert p.search_budget_multiplier == 1.0
        assert p.web_search_enabled is True
        # Only the standard mode escalates.
        assert p.allows_escalation is True

    def test_deep_reasoning_policy(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", raising=False)
        import src.api_config as api_config
        importlib.reload(api_config)
        import src.verification_modes as modes_module
        importlib.reload(modes_module)
        p = modes_module.mode_policy(modes_module.VerificationMode.DEEP_REASONING)
        assert p.model == api_config.MODEL_OPUS_47
        assert p.thinking_enabled is True
        assert p.search_budget_multiplier == 1.0
        assert p.web_search_enabled is True
        # Already at the top — DEEP_REASONING does not escalate further.
        assert p.allows_escalation is False

    def test_unknown_or_empty_mode_falls_back_to_standard(self):
        """Cache entries may store ``verification_mode == ""``; policy
        lookup must handle that and string values without crashing."""
        from src.verification_modes import mode_policy
        empty = mode_policy("")
        assert empty.thinking_enabled is True
        assert empty.search_budget_multiplier == 1.0
        strict = mode_policy("strict_structured")
        assert strict.thinking_enabled is False
        assert strict.search_budget_multiplier == 0.5


# ===========================================================================
# 4. select_verification_mode — representative routing cases
# ===========================================================================


class TestSelectVerificationMode:
    """Plan Directive 6: representative routing cases.

    Each test pins one rule from :func:`select_verification_mode`'s
    priority order so a future tuning change has explicit pass/fail
    criteria for every routing decision.
    """

    def test_local_skip_routes_to_local_skip_mode(self):
        """Rule 1: ``local_skip=True`` wins outright."""
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(severity="GRIPES", issue="placeholder for INSERT")
        m = select_verification_mode(f, local_skip=True, escalated=False)
        assert m is VerificationMode.LOCAL_SKIP

    def test_escalated_routes_to_deep_reasoning(self):
        """Rule 2: ``escalated=True`` forces DEEP_REASONING regardless of severity."""
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(severity="MEDIUM", issue="ordinary claim")
        m = select_verification_mode(f, escalated=True)
        assert m is VerificationMode.DEEP_REASONING

    def test_critical_california_routes_to_deep_initially(self, monkeypatch):
        """Rule 3: CRITICAL CALIFORNIA_AHJ goes straight to Opus on the
        initial pass when Sonnet-default is on."""
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "1")
        import src.api_config as api_config
        importlib.reload(api_config)
        import src.verification_modes as modes_module
        importlib.reload(modes_module)
        f = _finding(
            severity="CRITICAL",
            code_ref="Title 24",
            issue="California amended CBC reference is stale per DSA bulletin",
        )
        m = modes_module.select_verification_mode(f, escalated=False)
        assert m is modes_module.VerificationMode.DEEP_REASONING

    def test_critical_california_stays_standard_when_sonnet_disabled(self, monkeypatch):
        """When the everywhere-Opus override is set, there is no distinct
        deep tier and CRITICAL California findings just route through
        STANDARD_REASONING (which is already on Opus)."""
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "0")
        import src.api_config as api_config
        importlib.reload(api_config)
        import src.verification_modes as modes_module
        importlib.reload(modes_module)
        f = _finding(
            severity="CRITICAL",
            code_ref="Title 24",
            issue="California amended CBC reference",
        )
        m = modes_module.select_verification_mode(f, escalated=False)
        assert m is modes_module.VerificationMode.STANDARD_REASONING

    def test_low_severity_editorial_routes_to_strict_structured(self):
        """Rule 4: GRIPES severity → STRICT_STRUCTURED.

        Plan: "Low-severity editorial issue" routing case.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(
            severity="GRIPES",
            issue="Minor formatting inconsistency in 2.1.A",
        )
        m = select_verification_mode(f, escalated=False)
        assert m is VerificationMode.STRICT_STRUCTURED

    def test_simple_stale_code_routes_to_standard_reasoning(self):
        """Plan: "Simple stale-code/factual issue" routing case.

        A MEDIUM code finding without California markers should ride
        the default STANDARD_REASONING path — Sonnet + thinking +
        full profile budget.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(
            severity="MEDIUM",
            code_ref="NFPA 13",
            issue="Cited NFPA edition appears to be one cycle behind",
        )
        m = select_verification_mode(f, escalated=False)
        assert m is VerificationMode.STANDARD_REASONING

    def test_high_severity_code_routes_to_standard_initially(self):
        """Plan: "High-severity code issue" routing case.

        High-severity findings go through STANDARD_REASONING on the
        initial pass; the escalation policy in
        :func:`should_escalate_verification` decides whether to bump to
        DEEP_REASONING after the initial verdict comes back.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(
            severity="HIGH",
            code_ref="CBC Chapter 7",
            issue="Cited section number does not exist in the 2025 CBC",
        )
        m = select_verification_mode(f, escalated=False)
        assert m is VerificationMode.STANDARD_REASONING

    def test_internal_coordination_high_routes_to_strict(self):
        """Plan: "Internal coordination issue" routing case.

        Non-GRIPES internal-coordination findings still ride the
        narrow path because web search adds little signal — they're
        verifiable from the spec text itself.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(
            severity="HIGH",
            issue="Section 2.2.B specifies 5 ft but Section 4.1.A specifies 8 ft — internal contradiction",
        )
        m = select_verification_mode(f, escalated=False)
        assert m is VerificationMode.STRICT_STRUCTURED

    def test_source_disputed_escalation_routes_to_deep(self):
        """Plan: "Source-disputed issue" routing case.

        The escalation re-run for a finding where the initial pass
        produced contradictory or insufficient evidence rides
        DEEP_REASONING. This test exercises that re-run as a router
        decision — the caller passes ``escalated=True`` to indicate
        the retry path.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(
            severity="HIGH",
            code_ref="ASHRAE 90.1",
            issue="Cited efficiency value contradicts ASHRAE's published table",
        )
        m = select_verification_mode(f, escalated=True)
        assert m is VerificationMode.DEEP_REASONING

    def test_prior_cache_hit_preserves_stored_mode(self):
        """Plan: "Prior verification cache hit" routing case.

        When the verifier honors a cached verdict, the returned record
        should carry the *original* mode tag, not be silently relabeled
        with the current routing decision. The router accepts a
        ``cached_mode`` keyword for exactly this reason.
        """
        from src.verification_modes import VerificationMode, select_verification_mode
        f = _finding(severity="HIGH", code_ref="CBC")
        m = select_verification_mode(
            f, cached_mode=VerificationMode.DEEP_REASONING
        )
        assert m is VerificationMode.DEEP_REASONING

# ===========================================================================
# 5. Mode search-budget helper
# ===========================================================================


class TestModeSearchBudget:
    def test_modes_apply_their_multipliers(self):
        from src.verification_modes import VerificationMode, mode_search_budget
        assert mode_search_budget(VerificationMode.LOCAL_SKIP, profile_ceiling=8) == 0
        assert mode_search_budget(VerificationMode.STANDARD_REASONING, profile_ceiling=7) == 7
        assert mode_search_budget(VerificationMode.DEEP_REASONING, profile_ceiling=8) == 8
        assert mode_search_budget(VerificationMode.STRICT_STRUCTURED, profile_ceiling=6) == 3

    def test_strict_structured_floor_of_one(self):
        """A 1-search ceiling under STRICT_STRUCTURED still allows one search."""
        from src.verification_modes import VerificationMode, mode_search_budget
        assert mode_search_budget(VerificationMode.STRICT_STRUCTURED, profile_ceiling=1) == 1


# ===========================================================================
# 5. Cache + resume-state round-trip
# ===========================================================================


class TestCacheRoundTripsMode:
    def test_cache_save_and_load_preserves_mode(self, tmp_path, monkeypatch):
        from src.verification_cache import VerificationCache
        from src.verifier import VerificationResult
        from src.verification_modes import VerificationMode

        f = _finding(severity="HIGH", code_ref="CBC 2025", issue="X")
        from src.code_cycles import DEFAULT_CYCLE

        result = VerificationResult(
            verdict="CONFIRMED",
            explanation="Backed by DGS",
            sources=["https://dgs.ca.gov/x"],
            grounded=True,
            model_used="claude-sonnet-4-6",
            verification_profile="california_ahj",
            verification_mode=VerificationMode.STANDARD_REASONING.value,
        )
        cache = VerificationCache()
        cache.put(f, cycle=DEFAULT_CYCLE, result=result)
        path = tmp_path / "verification_cache.json"
        cache.save_to_disk(path)

        # New cache instance — must pull the mode tag back off disk.
        fresh = VerificationCache()
        fresh.load_from_disk(path)
        hit = fresh.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.verification_mode == VerificationMode.STANDARD_REASONING.value
        # Returning a hit re-stamps cache_status="hit" but should NOT
        # overwrite the original mode.
        assert hit.cache_status == "hit"

    def test_resume_state_serialization_round_trip(self):
        from src.resume_state import (
            deserialize_verification_result,
            serialize_verification_result,
        )
        from src.verifier import VerificationResult
        from src.verification_modes import VerificationMode

        original = VerificationResult(
            verdict="UNVERIFIED",
            explanation="No external evidence",
            verification_mode=VerificationMode.STRICT_STRUCTURED.value,
            verification_profile="internal_coordination",
        )
        payload = serialize_verification_result(original)
        assert payload["verification_mode"] == "strict_structured"
        restored = deserialize_verification_result(payload)
        assert restored is not None
        assert restored.verification_mode == "strict_structured"

    def test_resume_state_legacy_payload_deserializes_with_empty_mode(self):
        """Pre-Chunk-I resume state lacks ``verification_mode``; the
        deserializer must default to ``""`` instead of raising."""
        from src.resume_state import deserialize_verification_result

        legacy_payload = {
            "verdict": "CONFIRMED",
            "explanation": "Backed by DGS",
            "sources": ["https://dgs.ca.gov/x"],
            "correction": None,
            "grounded": True,
            "model_used": "claude-sonnet-4-6",
            "escalated": False,
            "cache_status": "miss",
            "web_search_requests": 1,
            "successful_source_count": 1,
            "search_error_count": 0,
            # No verification_mode / verification_profile keys.
        }
        restored = deserialize_verification_result(legacy_payload)
        assert restored is not None
        assert restored.verification_mode == ""
        assert restored.verification_profile == ""


# ===========================================================================
# 8. Diagnostics counters
# ===========================================================================


class TestDiagnosticsCountsModes:
    def test_summary_includes_mode_breakdown(self):
        from src.diagnostics import DiagnosticsReport
        from src.verification_modes import VerificationMode

        diag = DiagnosticsReport()
        diag.log("verification", "info", "x", {
            "verdict": "CONFIRMED",
            "grounded": True,
            "verification_mode": VerificationMode.STANDARD_REASONING.value,
            "verification_profile": "code_standard",
        })
        diag.log("verification", "info", "y", {
            "verdict": "UNVERIFIED",
            "grounded": False,
            "verification_mode": VerificationMode.LOCAL_SKIP.value,
            "verification_profile": "internal_coordination",
            "cache_status": "local_skip",
        })
        diag.log("verification", "info", "z", {
            "verdict": "UNVERIFIED",
            "grounded": False,
            "verification_mode": VerificationMode.STRICT_STRUCTURED.value,
            "verification_profile": "constructability",
        })

        summary = diag.summary()
        modes = summary["verification_modes"]
        assert modes["standard_reasoning"] == 1
        assert modes["local_skip"] == 1
        assert modes["strict_structured"] == 1
        profiles = summary["verification_profiles"]
        assert profiles["code_standard"] == 1
        assert profiles["internal_coordination"] == 1
        assert profiles["constructability"] == 1

    def test_summary_buckets_missing_mode_as_unknown(self):
        """Legacy events (pre-Chunk-I) lack the mode tag; the counter
        should still see them under a stable bucket."""
        from src.diagnostics import DiagnosticsReport
        diag = DiagnosticsReport()
        diag.log("verification", "info", "x", {
            "verdict": "CONFIRMED",
            "grounded": True,
            # No verification_mode key.
        })
        summary = diag.summary()
        assert summary["verification_modes"]["unknown"] == 1

    def test_to_text_includes_modes_line_when_present(self):
        from src.diagnostics import DiagnosticsReport
        from src.verification_modes import VerificationMode
        diag = DiagnosticsReport()
        diag.log("verification", "info", "x", {
            "verdict": "CONFIRMED",
            "grounded": True,
            "verification_mode": VerificationMode.STANDARD_REASONING.value,
        })
        text = diag.to_text()
        assert "Modes:" in text
        assert "standard_reasoning" in text


# ===========================================================================
# 9. End-to-end: real-time _run_verification_call thinking + budget gating
# ===========================================================================


class TestRealTimeCallRespectsMode:
    """Drive ``_run_verification_call`` through a fake streaming client so
    we can assert the resulting request kwargs match the mode policy
    without making a real network call."""

    def _stub_client(self, *, monkeypatch, captured: dict, response_message):
        from src import verifier
        from types import SimpleNamespace

        class _Stream:
            def __init__(self, **kwargs):
                # Capture the request kwargs and the final message.
                captured.update(kwargs)
                self._msg = response_message

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_final_message(self):
                return self._msg

        class _Messages:
            def stream(self, **kwargs):
                return _Stream(**kwargs)

        class _Client:
            messages = _Messages()

        monkeypatch.setattr(verifier, "_get_client", lambda: _Client())

    def _fake_complete_message(self, *, verdict: str, sources=None):
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeToolUseBlock,
            FakeUsage,
            FakeWebSearchResultBlock,
        )
        from types import SimpleNamespace

        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://dgs.ca.gov/x",
                            "title": "DGS",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
                FakeToolUseBlock(
                    name="submit_verification_verdict",
                    input={
                        "verdict": verdict,
                        "explanation": "Backed by DGS.",
                        "sources": list(sources or ["https://dgs.ca.gov/x"]),
                        "correction": None,
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(),
        )
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        return msg

    def test_standard_reasoning_attaches_thinking_and_full_budget(
        self, monkeypatch
    ):
        from src.verifier import _run_verification_call
        from src.code_cycles import DEFAULT_CYCLE
        from src.verification_profiles import VerificationProfile, profile_max_uses

        captured: dict = {}
        self._stub_client(
            monkeypatch=monkeypatch,
            captured=captured,
            response_message=self._fake_complete_message(verdict="CONFIRMED"),
        )

        f = _finding(severity="HIGH", code_ref="NFPA 13", issue="cited section")
        result = _run_verification_call(
            f, cycle=DEFAULT_CYCLE, model="claude-sonnet-4-6", max_retries=0, escalated=False,
        )
        # STANDARD_REASONING: ``thinking`` present, web_search max_uses
        # equals the full profile/severity ceiling.
        assert "thinking" in captured
        web_tool = next(t for t in captured["tools"] if t.get("name") == "web_search")
        assert web_tool["max_uses"] == profile_max_uses(
            VerificationProfile.CODE_STANDARD, "HIGH"
        )
        # Result is stamped with the mode.
        assert result.verification_mode == "standard_reasoning"
        # And carries the profile.
        assert result.verification_profile == "code_standard"

    def test_strict_structured_omits_thinking_and_scales_budget(self, monkeypatch):
        from src.verifier import _run_verification_call
        from src.code_cycles import DEFAULT_CYCLE

        captured: dict = {}
        self._stub_client(
            monkeypatch=monkeypatch,
            captured=captured,
            response_message=self._fake_complete_message(verdict="CONFIRMED"),
        )

        # GRIPES severity routes to STRICT_STRUCTURED.
        f = _finding(
            severity="GRIPES",
            issue="formatting inconsistency",
        )
        result = _run_verification_call(
            f, cycle=DEFAULT_CYCLE, model="claude-sonnet-4-6", max_retries=0, escalated=False,
        )
        # STRICT_STRUCTURED: no ``thinking`` key, scaled budget.
        assert "thinking" not in captured
        web_tool = next(t for t in captured["tools"] if t.get("name") == "web_search")
        # Floor of 1 — internal-coordination GRIPES profile budget is 1,
        # 1 * 0.5 = 0.5 rounds to 0 then floored to 1.
        assert web_tool["max_uses"] >= 1
        assert result.verification_mode == "strict_structured"

    def test_escalated_call_stamps_deep_reasoning_mode(self, monkeypatch):
        from src.verifier import _run_verification_call
        from src.code_cycles import DEFAULT_CYCLE

        captured: dict = {}
        self._stub_client(
            monkeypatch=monkeypatch,
            captured=captured,
            response_message=self._fake_complete_message(verdict="CONFIRMED"),
        )

        f = _finding(severity="HIGH", code_ref="ASHRAE 90.1")
        result = _run_verification_call(
            f,
            cycle=DEFAULT_CYCLE,
            model="claude-opus-4-7",
            max_retries=0,
            escalated=True,
        )
        assert result.verification_mode == "deep_reasoning"
        # Thinking is enabled in deep mode.
        assert "thinking" in captured


# ===========================================================================
# 10. End-to-end: batch wave path stamps the mode
# ===========================================================================


class _FakeBatchResult:
    def __init__(self, message):
        from types import SimpleNamespace
        self.result = SimpleNamespace(type="succeeded", message=message, error=None)


class TestBatchWavePathStampsMode:
    def _build_message(self, *, verdict: str, sources=None):
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeToolUseBlock,
            FakeUsage,
            FakeWebSearchResultBlock,
        )
        from types import SimpleNamespace

        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://dgs.ca.gov/x",
                            "title": "DGS",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
                FakeToolUseBlock(
                    name="submit_verification_verdict",
                    input={
                        "verdict": verdict,
                        "explanation": "Backed by DGS.",
                        "sources": list(sources or ["https://dgs.ca.gov/x"]),
                        "correction": None,
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(),
        )
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        return msg

    def test_wave_stamps_standard_reasoning_for_high_code_finding(
        self, monkeypatch
    ):
        from src import verifier
        from src.batch import BatchJob
        from src.verifier import _classify_wave_results

        msg = self._build_message(verdict="CONFIRMED")
        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(msg)},
        )

        finding = _finding(severity="HIGH", code_ref="CBC", issue="cited section")
        job = BatchJob(
            batch_id="bid",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        contexts = {
            "verify__0": {
                "finding_idx": 0,
                "original_prompt": "p",
                "model": "claude-sonnet-4-6",
                "escalated": False,
            }
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.verification_mode == "standard_reasoning"

    def test_wave_stamps_deep_reasoning_for_escalated_finding(self, monkeypatch):
        from src import verifier
        from src.batch import BatchJob
        from src.verifier import _classify_wave_results

        msg = self._build_message(verdict="CONFIRMED")
        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(msg)},
        )

        finding = _finding(severity="HIGH", code_ref="CBC")
        job = BatchJob(
            batch_id="bid",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        contexts = {
            "verify__0": {
                "finding_idx": 0,
                "original_prompt": "p",
                "model": "claude-opus-4-7",
                # This is the retry wave — caller marked it escalated.
                "escalated": True,
            }
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.verification_mode == "deep_reasoning"


# ===========================================================================
# 11. Cache hit does not bypass routing (Chunk I Directive 7)
# ===========================================================================


class TestCacheDoesNotBypassRouting:
    def test_cache_hit_returns_cached_record_with_its_mode(self, monkeypatch):
        """A cached hit should return the record as-is — the routing
        decision recorded at miss time is preserved.

        This is the inverse of "routing changes do not bypass the cache":
        when the cache *does* answer, the result should carry the
        original routing tag so reports show what the original run did.
        """
        from src.code_cycles import DEFAULT_CYCLE
        from src.verification_cache import VerificationCache
        from src.verifier import VerificationResult, verify_finding

        f = _finding(severity="HIGH", code_ref="NFPA 13", issue="x")
        cached = VerificationResult(
            verdict="CONFIRMED",
            explanation="cached",
            sources=["https://nfpa.org/x"],
            grounded=True,
            model_used="claude-sonnet-4-6",
            verification_profile="code_standard",
            verification_mode="standard_reasoning",
        )
        cache = VerificationCache()
        cache.put(f, cycle=DEFAULT_CYCLE, result=cached)

        # If we hit the cache, we should NOT hit the network — patch
        # ``_run_verification_call`` so any miss would explode the test.
        from src import verifier as v_mod
        def _exploding_call(*_a, **_k):  # pragma: no cover - defensive
            raise AssertionError("Cache miss path should not be hit")
        monkeypatch.setattr(v_mod, "_run_verification_call", _exploding_call)

        result = verify_finding(f, cycle=DEFAULT_CYCLE, cache=cache)
        assert result.verdict == "CONFIRMED"
        assert result.verification_mode == "standard_reasoning"
        assert result.cache_status == "hit"
