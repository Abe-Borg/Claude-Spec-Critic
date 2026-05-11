"""Chunk H tests — source grounding + verification profiles.

Two themes:

1. ``src.source_grounding`` — URL normalization and cited-source
   validation against actual web_search results.
2. ``src.verification_profiles`` — keyword-based classification of a
   finding into a verification profile, and profile-aware web_search
   budgets that subordinate severity to the profile.

The integration surfaces:

- ``VerificationResult`` carries ``searched_sources`` /
  ``cited_sources`` / ``accepted_sources`` / ``rejected_sources`` /
  ``verification_profile``.
- The verifier real-time and batch wave paths route through
  ``_apply_source_grounding`` so ungrounded citations are detected and
  ``CONFIRMED`` / ``CORRECTED`` is downgraded to ``UNVERIFIED`` when
  every citation missed.
- The batch initial path and retry / continuation builders accept a
  profile keyword and use the profile-aware tool builder.
- ``verification_cache`` and ``resume_state`` round-trip the new fields.
"""
from __future__ import annotations

import importlib

import pytest

from src.code_cycles import DEFAULT_CYCLE
from src.reviewer import Finding


pytestmark = pytest.mark.source_grounding


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
# 1. URL normalization
# ===========================================================================


class TestNormalizeUrl:
    def test_empty_and_none(self):
        from src.source_grounding import normalize_url
        assert normalize_url("") == ""
        assert normalize_url(None) == ""  # type: ignore[arg-type]
        assert normalize_url("   ") == ""

    def test_http_and_https_fold(self):
        """http and https should compare equal for the same host+path."""
        from src.source_grounding import normalize_url
        assert normalize_url("http://dgs.ca.gov/x") == normalize_url(
            "https://dgs.ca.gov/x"
        )

    def test_trailing_slash_dropped(self):
        from src.source_grounding import normalize_url
        assert normalize_url("https://dgs.ca.gov/x/") == normalize_url(
            "https://dgs.ca.gov/x"
        )

    def test_root_trailing_slash_preserved_only_for_match(self):
        """``https://host/`` and ``https://host`` should match too."""
        from src.source_grounding import normalize_url
        # Both canonicalize to the same form.
        a = normalize_url("https://dgs.ca.gov/")
        b = normalize_url("https://dgs.ca.gov")
        assert a == b

    def test_host_lowercased(self):
        from src.source_grounding import normalize_url
        assert normalize_url("https://DGS.CA.GOV/foo") == normalize_url(
            "https://dgs.ca.gov/foo"
        )

    def test_default_port_stripped(self):
        from src.source_grounding import normalize_url
        assert normalize_url("http://example.com:80/foo") == normalize_url(
            "http://example.com/foo"
        )
        assert normalize_url("https://example.com:443/foo") == normalize_url(
            "https://example.com/foo"
        )

    def test_non_default_port_preserved(self):
        from src.source_grounding import normalize_url
        # Non-default ports are semantically meaningful and must NOT collapse.
        assert normalize_url("https://example.com:8443/foo") != normalize_url(
            "https://example.com/foo"
        )

    def test_fragment_dropped(self):
        from src.source_grounding import normalize_url
        assert normalize_url("https://x/y#anchor") == normalize_url("https://x/y")

    def test_tracking_query_params_dropped(self):
        from src.source_grounding import normalize_url
        a = normalize_url("https://x/y?utm_source=goog&page=2")
        b = normalize_url("https://x/y?page=2")
        assert a == b

    def test_non_tracking_query_param_preserved(self):
        """``?page=2`` is semantically meaningful and must NOT collapse to ``?page=3``."""
        from src.source_grounding import normalize_url
        assert normalize_url("https://x/y?page=2") != normalize_url(
            "https://x/y?page=3"
        )

    def test_query_param_order_normalized(self):
        from src.source_grounding import normalize_url
        a = normalize_url("https://x/y?b=2&a=1")
        b = normalize_url("https://x/y?a=1&b=2")
        assert a == b

    def test_strips_angle_brackets(self):
        from src.source_grounding import normalize_url
        a = normalize_url("<https://dgs.ca.gov/x>")
        b = normalize_url("https://dgs.ca.gov/x")
        assert a == b

    def test_strips_trailing_punctuation(self):
        from src.source_grounding import normalize_url
        a = normalize_url("https://dgs.ca.gov/x).")
        b = normalize_url("https://dgs.ca.gov/x")
        assert a == b

    def test_credentials_stripped(self):
        from src.source_grounding import normalize_url
        a = normalize_url("https://user:pass@dgs.ca.gov/x")
        b = normalize_url("https://dgs.ca.gov/x")
        assert a == b

    def test_handles_malformed_input(self):
        from src.source_grounding import normalize_url
        # Whitespace-only / garbage strings normalize to empty rather than
        # crashing.
        assert normalize_url("not a url at all") != ""  # bare host -> https://...
        # An obviously broken URL still doesn't raise.
        normalize_url("http://[badly-formed")

    def test_bare_host_path_recovered_as_https(self):
        from src.source_grounding import normalize_url
        a = normalize_url("dgs.ca.gov/page")
        b = normalize_url("https://dgs.ca.gov/page")
        assert a == b


# ===========================================================================
# 2. validate_cited_sources
# ===========================================================================


class TestValidateCitedSources:
    def test_valid_cited_url(self):
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(
            cited=["https://dgs.ca.gov/foo"],
            searched=["https://dgs.ca.gov/foo"],
        )
        assert out.has_any_grounded_citation()
        assert out.accepted == ("https://dgs.ca.gov/foo",)
        assert out.rejected == ()

    def test_unknown_cited_url(self):
        from src.source_grounding import REJECT_UNGROUNDED, validate_cited_sources
        out = validate_cited_sources(
            cited=["https://invented.example.com"],
            searched=["https://dgs.ca.gov/foo"],
        )
        assert not out.has_any_grounded_citation()
        assert out.accepted == ()
        assert out.rejected == (
            {"url": "https://invented.example.com", "reason": REJECT_UNGROUNDED},
        )

    def test_trailing_slash_difference_accepts(self):
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(
            cited=["https://dgs.ca.gov/foo/"],
            searched=["https://dgs.ca.gov/foo"],
        )
        assert out.has_any_grounded_citation()

    def test_query_string_difference_accepts_when_tracking(self):
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(
            cited=["https://dgs.ca.gov/foo?utm_source=anyone"],
            searched=["https://dgs.ca.gov/foo"],
        )
        assert out.has_any_grounded_citation()

    def test_query_string_difference_rejects_when_semantic(self):
        """``?page=2`` is real — does NOT match ``?page=3``."""
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(
            cited=["https://dgs.ca.gov/foo?page=2"],
            searched=["https://dgs.ca.gov/foo?page=3"],
        )
        assert not out.has_any_grounded_citation()

    def test_no_web_search_used(self):
        """Empty searched set -> every cited URL is rejected."""
        from src.source_grounding import REJECT_UNGROUNDED, validate_cited_sources
        out = validate_cited_sources(
            cited=["https://dgs.ca.gov/foo"],
            searched=[],
        )
        assert not out.has_any_grounded_citation()
        assert len(out.rejected) == 1
        assert out.rejected[0]["reason"] == REJECT_UNGROUNDED

    def test_insufficient_evidence_no_citations(self):
        """No citations + no searched -> nothing accepted, nothing rejected."""
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(cited=[], searched=[])
        assert out.accepted == ()
        assert out.rejected == ()
        assert not out.has_any_grounded_citation()

    def test_empty_string_citation_marked_empty(self):
        from src.source_grounding import REJECT_EMPTY, validate_cited_sources
        out = validate_cited_sources(cited=["", "  "], searched=["https://x/y"])
        assert all(r["reason"] == REJECT_EMPTY for r in out.rejected)

    def test_duplicate_cited_urls_collapse(self):
        """Two cosmetically different forms of the same URL should appear once."""
        from src.source_grounding import validate_cited_sources
        out = validate_cited_sources(
            cited=[
                "https://dgs.ca.gov/x/",
                "http://DGS.CA.GOV/x",  # same after normalization
            ],
            searched=["https://dgs.ca.gov/x"],
        )
        assert len(out.accepted) == 1


# ===========================================================================
# 3. VerificationResult evidence model
# ===========================================================================


class TestVerificationResultEvidenceFields:
    def test_new_fields_default_safely(self):
        from src.verifier import VerificationResult
        r = VerificationResult(verdict="UNVERIFIED")
        assert r.searched_sources == []
        assert r.cited_sources == []
        assert r.accepted_sources == []
        assert r.rejected_sources == []
        assert r.verification_profile == ""


# ===========================================================================
# 4. _apply_source_grounding behavior
# ===========================================================================


class TestApplySourceGrounding:
    def test_accepted_and_rejected_partition(self):
        from src.source_grounding import SearchedSource
        from src.verifier import VerificationResult, _apply_source_grounding
        r = VerificationResult(
            verdict="CONFIRMED",
            sources=["https://dgs.ca.gov/page", "https://invented.example.com"],
            grounded=True,
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page", title="DGS")]
        out = _apply_source_grounding(r, searched=searched)
        assert out.accepted_sources == ["https://dgs.ca.gov/page"]
        assert out.rejected_sources == [
            {"url": "https://invented.example.com", "reason": "ungrounded"}
        ]
        # public ``sources`` is replaced with accepted only.
        assert out.sources == ["https://dgs.ca.gov/page"]
        # ``cited_sources`` preserves the model's original list.
        assert out.cited_sources == [
            "https://dgs.ca.gov/page",
            "https://invented.example.com",
        ]
        # At least one citation grounded -> verdict stays CONFIRMED.
        assert out.verdict == "CONFIRMED"

    def test_all_citations_rejected_downgrades_confirmed(self):
        """If every cited URL is ungrounded, CONFIRMED -> UNVERIFIED."""
        from src.source_grounding import SearchedSource
        from src.verifier import VerificationResult, _apply_source_grounding
        r = VerificationResult(
            verdict="CONFIRMED",
            sources=["https://invented.example.com"],
            grounded=True,
            explanation="DGS says it's fine.",
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page")]
        out = _apply_source_grounding(r, searched=searched)
        assert out.verdict == "UNVERIFIED"
        assert "downgraded" in out.explanation.lower()
        assert out.grounded is False  # downgrade implies no longer grounded
        assert out.accepted_sources == []
        assert len(out.rejected_sources) == 1

    def test_all_citations_rejected_downgrades_corrected(self):
        from src.source_grounding import SearchedSource
        from src.verifier import VerificationResult, _apply_source_grounding
        r = VerificationResult(
            verdict="CORRECTED",
            sources=["https://invented.example.com"],
            grounded=True,
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page")]
        out = _apply_source_grounding(r, searched=searched)
        assert out.verdict == "UNVERIFIED"

    def test_no_citations_leaves_verdict_untouched(self):
        """No citations + grounded by search counts -> verdict stays as-is."""
        from src.source_grounding import SearchedSource
        from src.verifier import VerificationResult, _apply_source_grounding
        r = VerificationResult(
            verdict="CONFIRMED",
            sources=[],
            grounded=True,
        )
        out = _apply_source_grounding(
            r, searched=[SearchedSource(url="https://dgs.ca.gov/page")]
        )
        # No citations supplied -> nothing to validate; verdict stays.
        # The other grounding invariant
        # (:func:`_enforce_grounding_invariant`) handles the
        # ungrounded-with-no-citations case.
        assert out.verdict == "CONFIRMED"
        assert out.searched_sources == ["https://dgs.ca.gov/page"]

    def test_does_not_touch_unverified(self):
        """UNVERIFIED + ungrounded citations is fine; nothing to downgrade from."""
        from src.source_grounding import SearchedSource
        from src.verifier import VerificationResult, _apply_source_grounding
        r = VerificationResult(
            verdict="UNVERIFIED",
            sources=["https://invented.example.com"],
            grounded=False,
        )
        out = _apply_source_grounding(
            r, searched=[SearchedSource(url="https://dgs.ca.gov/page")]
        )
        assert out.verdict == "UNVERIFIED"
        # Rejected list still populated for diagnostics.
        assert len(out.rejected_sources) == 1


# ===========================================================================
# 5. Verification profiles
# ===========================================================================


class TestVerificationProfiles:
    def test_california_finding_routes_to_california_ahj(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            code_ref="CBC 2022 §1011",
            issue="Cited CBC section conflicts with California Title 24 amendment for DSA project.",
        )
        assert classify_finding_profile(f) == VerificationProfile.CALIFORNIA_AHJ

    def test_code_standard_finding_routes_to_code_standard(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            code_ref="NFPA 13 §6.2",
            issue="Generic NFPA citation needs to be updated for current edition.",
        )
        assert classify_finding_profile(f) == VerificationProfile.CODE_STANDARD

    def test_manufacturer_finding_routes_to_manufacturer(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            issue="Trane RTAC model number does not appear in the manufacturer's current catalog.",
            existing="Trane RTAC-001",
        )
        assert classify_finding_profile(f) == VerificationProfile.MANUFACTURER

    def test_internal_coordination_routes_to_internal(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            severity="GRIPES",
            issue="Internal contradiction within section 2.1: lists pipe spacing as both 5 ft and 8 ft.",
        )
        assert classify_finding_profile(f) == VerificationProfile.INTERNAL_COORDINATION

    def test_internal_coordination_for_placeholder(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            severity="GRIPES",
            issue="Unresolved placeholder [SELECT FAN MAKE] still present.",
        )
        assert classify_finding_profile(f) == VerificationProfile.INTERNAL_COORDINATION

    def test_default_constructability(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(issue="Pipe support spacing seems aggressive for this run length.")
        assert classify_finding_profile(f) == VerificationProfile.CONSTRUCTABILITY

    def test_california_takes_precedence_over_code_standard(self):
        """When both 'California' and 'CBC' appear, CALIFORNIA_AHJ wins."""
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            code_ref="CBC §1011",
            issue="California-amended CBC section is cited with the model code value.",
        )
        assert classify_finding_profile(f) == VerificationProfile.CALIFORNIA_AHJ

    def test_internal_takes_precedence_over_code(self):
        from src.verification_profiles import (
            VerificationProfile,
            classify_finding_profile,
        )
        f = _finding(
            code_ref="CBC §1011",
            issue="Duplicate paragraph in CBC reference block (formatting issue).",
        )
        # Even though codeReference is set, the keyword still routes to internal.
        assert classify_finding_profile(f) == VerificationProfile.INTERNAL_COORDINATION


class TestProfileMaxUses:
    def test_internal_coordination_is_smallest(self):
        from src.verification_profiles import VerificationProfile, profile_max_uses
        ic = profile_max_uses(VerificationProfile.INTERNAL_COORDINATION, "HIGH")
        code = profile_max_uses(VerificationProfile.CODE_STANDARD, "HIGH")
        ca = profile_max_uses(VerificationProfile.CALIFORNIA_AHJ, "HIGH")
        manuf = profile_max_uses(VerificationProfile.MANUFACTURER, "HIGH")
        const = profile_max_uses(VerificationProfile.CONSTRUCTABILITY, "HIGH")
        assert ic < min(code, ca, manuf, const)

    def test_california_ahj_largest_for_critical(self):
        from src.verification_profiles import VerificationProfile, profile_max_uses
        ca = profile_max_uses(VerificationProfile.CALIFORNIA_AHJ, "CRITICAL")
        code = profile_max_uses(VerificationProfile.CODE_STANDARD, "CRITICAL")
        assert ca >= code

    def test_severity_monotonic_within_profile(self):
        from src.verification_profiles import VerificationProfile, profile_max_uses
        for p in VerificationProfile:
            c = profile_max_uses(p, "CRITICAL")
            h = profile_max_uses(p, "HIGH")
            m = profile_max_uses(p, "MEDIUM")
            g = profile_max_uses(p, "GRIPES")
            assert c >= h >= m >= g

    def test_unknown_severity_falls_back_to_medium(self):
        from src.verification_profiles import VerificationProfile, profile_max_uses
        medium = profile_max_uses(VerificationProfile.CODE_STANDARD, "MEDIUM")
        weird = profile_max_uses(VerificationProfile.CODE_STANDARD, "WEIRD")
        assert weird == medium

    def test_unknown_profile_falls_back_constructability(self):
        from src.verification_profiles import VerificationProfile, profile_max_uses
        const = profile_max_uses(VerificationProfile.CONSTRUCTABILITY, "HIGH")
        unknown = profile_max_uses("not_a_real_profile", "HIGH")
        assert unknown == const


class TestProfilePromptGuidance:
    def test_each_profile_has_distinct_guidance(self):
        from src.verification_profiles import (
            VerificationProfile,
            profile_priority_domains,
        )
        seen = set()
        for p in VerificationProfile:
            g = profile_priority_domains(p)
            assert g  # non-empty
            assert g not in seen
            seen.add(g)

    def test_unknown_profile_returns_empty(self):
        from src.verification_profiles import profile_priority_domains
        assert profile_priority_domains("nope") == ""


class TestProfileLabel:
    def test_pretty_label_for_each_profile(self):
        from src.verification_profiles import VerificationProfile, profile_label
        for p in VerificationProfile:
            label = profile_label(p)
            assert label and label != p.value  # not the raw enum value

    def test_none_returns_empty(self):
        from src.verification_profiles import profile_label
        assert profile_label(None) == ""

    def test_string_round_trip(self):
        from src.verification_profiles import profile_label, VerificationProfile
        assert profile_label("code_standard") == profile_label(
            VerificationProfile.CODE_STANDARD
        )


# ===========================================================================
# 6. build_verification_tools_for_profile
# ===========================================================================


class TestBuildVerificationToolsForProfile:
    def test_uses_profile_max_uses_over_severity(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", raising=False)
        from src.batch import build_verification_tools_for_profile
        from src.verification_profiles import VerificationProfile, profile_max_uses

        tools = build_verification_tools_for_profile(
            VerificationProfile.INTERNAL_COORDINATION, "HIGH"
        )
        web = next(t for t in tools if t.get("name") == "web_search")
        assert web["max_uses"] == profile_max_uses(
            VerificationProfile.INTERNAL_COORDINATION, "HIGH"
        )

    def test_includes_verdict_tool_when_structured_outputs_enabled(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "1")
        from src.batch import build_verification_tools_for_profile

        tools = build_verification_tools_for_profile("code_standard", "HIGH")
        names = [t.get("name") for t in tools]
        assert "submit_verification_verdict" in names

    def test_omits_verdict_tool_when_structured_outputs_disabled(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "0")
        from src.batch import build_verification_tools_for_profile

        tools = build_verification_tools_for_profile("code_standard", "HIGH")
        names = [t.get("name") for t in tools]
        assert "submit_verification_verdict" not in names
        assert "web_search" in names

    def test_string_profile_name_accepted(self):
        from src.batch import build_verification_tools_for_profile
        tools = build_verification_tools_for_profile("california_ahj", "CRITICAL")
        web = next(t for t in tools if t.get("name") == "web_search")
        assert web["max_uses"] >= 1

    def test_none_profile_falls_back_constructability(self):
        from src.batch import build_verification_tools_for_profile
        from src.verification_profiles import VerificationProfile, profile_max_uses

        tools = build_verification_tools_for_profile(None, "MEDIUM")
        web = next(t for t in tools if t.get("name") == "web_search")
        assert web["max_uses"] == profile_max_uses(
            VerificationProfile.CONSTRUCTABILITY, "MEDIUM"
        )


# ===========================================================================
# 7. dedupe_searched_sources
# ===========================================================================


class TestDedupeSearchedSources:
    def test_collapses_trailing_slash(self):
        from src.source_grounding import (
            SearchedSource,
            dedupe_searched_sources,
        )
        out = dedupe_searched_sources(
            [
                SearchedSource(url="https://dgs.ca.gov/foo"),
                SearchedSource(url="https://dgs.ca.gov/foo/"),
            ]
        )
        assert len(out) == 1

    def test_preserves_order_first_wins(self):
        from src.source_grounding import SearchedSource, dedupe_searched_sources
        out = dedupe_searched_sources(
            [
                SearchedSource(url="https://dgs.ca.gov/foo", title="DGS first"),
                SearchedSource(url="https://dgs.ca.gov/foo", title="DGS second"),
                SearchedSource(url="https://nfpa.org/foo"),
            ]
        )
        urls = [s.url for s in out]
        titles = [s.title for s in out]
        assert urls == ["https://dgs.ca.gov/foo", "https://nfpa.org/foo"]
        assert titles[0] == "DGS first"

    def test_accepts_mixed_inputs(self):
        from src.source_grounding import SearchedSource, dedupe_searched_sources
        out = dedupe_searched_sources(
            [
                "https://dgs.ca.gov/a",
                {"url": "https://nfpa.org/b", "title": "NFPA"},
                SearchedSource(url="https://iccsafe.org/c"),
                None,
                "",
                {"url": ""},
            ]
        )
        urls = {s.url for s in out}
        assert urls == {
            "https://dgs.ca.gov/a",
            "https://nfpa.org/b",
            "https://iccsafe.org/c",
        }


# ===========================================================================
# 8. _local_skip_result stamps profile
# ===========================================================================


class TestLocalSkipStampsProfile:
    def test_local_skip_result_has_internal_coordination_profile(self):
        from src.verifier import _local_skip_result
        from src.verification_profiles import VerificationProfile

        r = _local_skip_result()
        assert r.verification_profile == VerificationProfile.INTERNAL_COORDINATION.value


# ===========================================================================
# 9. Resume state round-trip
# ===========================================================================


class TestResumeStateRoundTrip:
    def test_round_trip_preserves_chunk_h_fields(self):
        from src.resume_state import (
            deserialize_verification_result,
            serialize_verification_result,
        )
        from src.verifier import VerificationResult

        original = VerificationResult(
            verdict="CONFIRMED",
            sources=["https://dgs.ca.gov/x"],
            grounded=True,
            searched_sources=[
                "https://dgs.ca.gov/x",
                "https://nfpa.org/y",
            ],
            cited_sources=[
                "https://dgs.ca.gov/x",
                "https://invented.example.com",
            ],
            accepted_sources=["https://dgs.ca.gov/x"],
            rejected_sources=[
                {"url": "https://invented.example.com", "reason": "ungrounded"}
            ],
            verification_profile="california_ahj",
        )
        payload = serialize_verification_result(original)
        restored = deserialize_verification_result(payload)
        assert restored is not None
        assert restored.searched_sources == [
            "https://dgs.ca.gov/x",
            "https://nfpa.org/y",
        ]
        assert restored.cited_sources == [
            "https://dgs.ca.gov/x",
            "https://invented.example.com",
        ]
        assert restored.accepted_sources == ["https://dgs.ca.gov/x"]
        assert restored.rejected_sources == [
            {"url": "https://invented.example.com", "reason": "ungrounded"}
        ]
        assert restored.verification_profile == "california_ahj"

    def test_legacy_payload_defaults_safely(self):
        from src.resume_state import deserialize_verification_result

        legacy = {
            "verdict": "CONFIRMED",
            "explanation": "",
            "sources": ["https://dgs.ca.gov/x"],
            "correction": None,
        }
        restored = deserialize_verification_result(legacy)
        assert restored is not None
        # New fields default to empty / unset; pre-Chunk-H runs still load.
        assert restored.searched_sources == []
        assert restored.cited_sources == []
        assert restored.accepted_sources == []
        assert restored.rejected_sources == []
        assert restored.verification_profile == ""


# ===========================================================================
# 10. Verification cache round-trip
# ===========================================================================


class TestVerificationCacheRoundTrip:
    def test_cache_save_load_preserves_chunk_h_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", raising=False)

        from src.verification_cache import VerificationCache
        from src.verifier import VerificationResult

        cache = VerificationCache()
        f = _finding(severity="HIGH", code_ref="CBC 2025 §1004")
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                sources=["https://dgs.ca.gov/x"],
                searched_sources=["https://dgs.ca.gov/x", "https://nfpa.org/y"],
                cited_sources=["https://dgs.ca.gov/x"],
                accepted_sources=["https://dgs.ca.gov/x"],
                rejected_sources=[
                    {"url": "https://bogus.example", "reason": "ungrounded"}
                ],
                verification_profile="california_ahj",
            ),
        )
        cache.save_to_disk(tmp_path / "cache.json")

        loaded = VerificationCache()
        loaded.load_from_disk(tmp_path / "cache.json")
        hit = loaded.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.searched_sources == [
            "https://dgs.ca.gov/x",
            "https://nfpa.org/y",
        ]
        assert hit.cited_sources == ["https://dgs.ca.gov/x"]
        assert hit.accepted_sources == ["https://dgs.ca.gov/x"]
        assert hit.rejected_sources == [
            {"url": "https://bogus.example", "reason": "ungrounded"}
        ]
        assert hit.verification_profile == "california_ahj"


# ===========================================================================
# 11. Batch and retry/continuation wire up profile
# ===========================================================================


class TestBatchInitialUsesProfileAwareBudget:
    def test_internal_coordination_finding_gets_small_budget(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.batch import _build_verification_request_params
        from src.verification_profiles import (
            VerificationProfile,
            profile_max_uses,
        )

        params = _build_verification_request_params(
            prompt="verify",
            system_prompt="system",
            severity="HIGH",
            profile=VerificationProfile.INTERNAL_COORDINATION.value,
        )
        web = next(t for t in params["tools"] if t.get("name") == "web_search")
        assert web["max_uses"] == profile_max_uses(
            VerificationProfile.INTERNAL_COORDINATION, "HIGH"
        )

    def test_no_profile_keyword_falls_back_to_severity_only(self, monkeypatch):
        """Existing severity-only callers (Chunk C/Phase 10 tests) still work."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.api_config import web_search_max_uses_for_severity
        from src.batch import _build_verification_request_params

        params = _build_verification_request_params(
            prompt="verify",
            system_prompt="system",
            severity="HIGH",
        )
        web = next(t for t in params["tools"] if t.get("name") == "web_search")
        assert web["max_uses"] == web_search_max_uses_for_severity("HIGH")


class TestRetryAndContinuationAcceptProfile:
    def test_retry_request_uses_profile_max_uses(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.verifier import _build_retry_request
        from src.verification_profiles import (
            VerificationProfile,
            profile_max_uses,
        )

        req = _build_retry_request(
            "prompt",
            cycle=DEFAULT_CYCLE,
            severity="MEDIUM",
            profile=VerificationProfile.MANUFACTURER.value,
        )
        web = next(t for t in req["tools"] if t.get("name") == "web_search")
        assert web["max_uses"] == profile_max_uses(
            VerificationProfile.MANUFACTURER, "MEDIUM"
        )

    def test_continuation_request_uses_profile_max_uses(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.verifier import _build_continuation_request
        from src.verification_profiles import (
            VerificationProfile,
            profile_max_uses,
        )

        req = _build_continuation_request(
            "prompt",
            [],
            cycle=DEFAULT_CYCLE,
            severity="HIGH",
            profile=VerificationProfile.CALIFORNIA_AHJ.value,
        )
        web = next(t for t in req["tools"] if t.get("name") == "web_search")
        assert web["max_uses"] == profile_max_uses(
            VerificationProfile.CALIFORNIA_AHJ, "HIGH"
        )


# ===========================================================================
# 12. Detailed search-evidence collection
# ===========================================================================


class TestCollectSearchEvidenceDetailed:
    def test_collects_url_and_title(self):
        from src.verifier import _collect_search_evidence_detailed
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeWebSearchResultBlock,
        )

        message = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://dgs.ca.gov/x",
                            "title": "DGS — Title 24",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
            ],
        )
        detailed, success, error = _collect_search_evidence_detailed(message)
        assert success == 1
        assert error == 0
        assert len(detailed) == 1
        assert detailed[0].url == "https://dgs.ca.gov/x"
        assert detailed[0].title == "DGS — Title 24"

    def test_backward_compatible_url_only_helper(self):
        from src.verifier import _collect_search_evidence
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeWebSearchResultBlock,
        )

        message = FakeMessage(
            content=[
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://nfpa.org/y",
                            "title": "NFPA 13",
                        }
                    ]
                ),
            ],
        )
        urls, success, error = _collect_search_evidence(message)
        assert urls == ["https://nfpa.org/y"]
        assert success == 1

    def test_handles_dict_search_items(self):
        """Batch path returns plain dicts; the helper must accept either."""
        from src.verifier import _collect_search_evidence_detailed
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeWebSearchResultBlock,
        )

        message = FakeMessage(
            content=[
                FakeWebSearchResultBlock(
                    content=[
                        # dict shape
                        {
                            "type": "web_search_result",
                            "url": "https://iccsafe.org/z",
                            "title": "ICC",
                        },
                    ]
                ),
            ],
        )
        detailed, _success, _error = _collect_search_evidence_detailed(message)
        assert detailed[0].url == "https://iccsafe.org/z"
        assert detailed[0].title == "ICC"


# ===========================================================================
# 13. End-to-end batch wave integration
# ===========================================================================


class _FakeBatchResult:
    def __init__(self, message):
        from types import SimpleNamespace
        self.result = SimpleNamespace(type="succeeded", message=message, error=None)


class TestBatchWaveIntegration:
    """Drive `_classify_wave_results` end-to-end with fake responses so we
    cover the integration of source-grounding into the batch wave path."""

    def _patch_retrieve(self, monkeypatch, message):
        from src import verifier
        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(message)},
        )

    def test_wave_accepts_grounded_citation(self, monkeypatch, fake_anthropic):
        from types import SimpleNamespace
        from src.batch import BatchJob
        from src.verifier import _classify_wave_results

        # Build a tool-use response with web_search blocks AND a matching
        # cited source.
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeWebSearchResultBlock,
            FakeToolUseBlock,
            FakeUsage,
        )
        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://dgs.ca.gov/page",
                            "title": "DGS",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
                FakeToolUseBlock(
                    name="submit_verification_verdict",
                    input={
                        "verdict": "CONFIRMED",
                        "explanation": "Backed by DGS.",
                        "sources": ["https://dgs.ca.gov/page"],
                        "correction": None,
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(),
        )
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        self._patch_retrieve(monkeypatch, msg)

        finding = _finding(severity="HIGH", code_ref="CBC 2025", issue="California amended CBC value")
        job = BatchJob(
            batch_id="bid",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        assert len(outcomes) == 1
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.verdict == "CONFIRMED"
        assert parsed.accepted_sources == ["https://dgs.ca.gov/page"]
        assert parsed.rejected_sources == []
        assert parsed.verification_profile  # set
        # Sources public list = accepted only.
        assert parsed.sources == ["https://dgs.ca.gov/page"]

    def test_wave_downgrades_ungrounded_citation(self, monkeypatch, fake_anthropic):
        from types import SimpleNamespace
        from src.batch import BatchJob
        from src.verifier import _classify_wave_results

        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeWebSearchResultBlock,
            FakeToolUseBlock,
            FakeUsage,
        )
        # Search retrieved DGS, but model cites a different (invented) URL.
        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://dgs.ca.gov/page",
                            "title": "DGS",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
                FakeToolUseBlock(
                    name="submit_verification_verdict",
                    input={
                        "verdict": "CONFIRMED",
                        "explanation": "Backed by some other source.",
                        "sources": ["https://invented.example.com"],
                        "correction": None,
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(),
        )
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        self._patch_retrieve(monkeypatch, msg)

        finding = _finding(severity="HIGH", code_ref="CBC")
        job = BatchJob(
            batch_id="bid",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        # Verdict downgraded because the only cited URL was ungrounded.
        assert parsed.verdict == "UNVERIFIED"
        assert parsed.accepted_sources == []
        assert len(parsed.rejected_sources) == 1
        assert parsed.rejected_sources[0]["reason"] == "ungrounded"
        # Searched URLs still preserved for diagnostics.
        assert parsed.searched_sources == ["https://dgs.ca.gov/page"]

    def test_wave_accepts_url_with_trailing_slash_difference(
        self, monkeypatch, fake_anthropic
    ):
        from types import SimpleNamespace
        from src.batch import BatchJob
        from src.verifier import _classify_wave_results

        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeWebSearchResultBlock,
            FakeToolUseBlock,
            FakeUsage,
        )
        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://nfpa.org/section",
                            "title": "NFPA",
                            "encrypted_content": "blob",
                        }
                    ]
                ),
                FakeToolUseBlock(
                    name="submit_verification_verdict",
                    input={
                        "verdict": "CONFIRMED",
                        "explanation": "Backed by NFPA.",
                        # Model cites with a trailing slash; search returned without.
                        "sources": ["https://nfpa.org/section/"],
                        "correction": None,
                    },
                ),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(),
        )
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        self._patch_retrieve(monkeypatch, msg)

        finding = _finding(severity="HIGH", code_ref="NFPA 13")
        job = BatchJob(
            batch_id="bid",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.verdict == "CONFIRMED"
        # The model's exact citation (with trailing slash) is preserved
        # in accepted_sources because normalization is internal only.
        assert parsed.accepted_sources == ["https://nfpa.org/section/"]
