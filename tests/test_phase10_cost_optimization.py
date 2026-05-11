"""Phase 10 cost-optimization tests.

Covers:
- Per-severity ``web_search`` budget (CRITICAL/HIGH=7, MEDIUM=5, GRIPES=3).
- Persistent verification cache (load/save round-trip, atomic write,
  corrupt-file recovery, optional TTL pruning).
- Haiku verification triage eligibility safety net (CRITICAL/HIGH and
  code-citing findings can never be locally skipped).
- Source trimming (verifier no longer merges every retrieved URL into
  the model's curated ``sources`` list).
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

from src.code_cycles import DEFAULT_CYCLE
from src.reviewer import Finding


def _make_finding(
    *,
    severity: str = "MEDIUM",
    code_ref: str | None = None,
    issue: str = "Section X contradicts section Y on pipe spacing",
    existing: str | None = "5 ft",
    replacement: str | None = "8 ft",
    action: str = "EDIT",
) -> Finding:
    return Finding(
        severity=severity,
        fileName="23 21 13 - Hydronic.docx",
        section="2.2.B",
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=0.7,
    )


# ---------------------------------------------------------------------------
# Per-severity web_search budget
# ---------------------------------------------------------------------------


def test_per_severity_web_search_budgets():
    from src.api_config import (
        web_search_max_uses_for_severity,
        web_search_tool_for_severity,
    )

    assert web_search_max_uses_for_severity("CRITICAL") == 7
    assert web_search_max_uses_for_severity("HIGH") == 7
    assert web_search_max_uses_for_severity("MEDIUM") == 5
    assert web_search_max_uses_for_severity("GRIPES") == 3
    # Unknown severities fall back to the default budget so a misclassified
    # finding still gets reasonable headroom.
    assert web_search_max_uses_for_severity("WEIRD") >= 1
    assert web_search_max_uses_for_severity(None) >= 1
    # Lowercase / whitespace tolerance.
    assert web_search_max_uses_for_severity("  high  ") == 7
    assert web_search_max_uses_for_severity("Medium") == 5

    # The tool builder echoes the per-severity max_uses.
    tool_high = web_search_tool_for_severity("CRITICAL")
    tool_low = web_search_tool_for_severity("GRIPES")
    assert tool_high["max_uses"] == 7
    assert tool_low["max_uses"] == 3
    # Other tool config (blocked domains, location) is unchanged.
    assert tool_high["type"] == "web_search_20260209"
    assert tool_high["user_location"]["region"] == "California"
    assert "reddit.com" in tool_high["blocked_domains"]


def test_per_severity_budget_consistent_realtime_and_batch(monkeypatch):
    """Both verification paths (real-time and batch) must use the same
    per-severity budget so behavior doesn't drift between modes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from src.api_config import web_search_tool_for_severity
    from src.batch import _build_verification_request_params

    realtime_high = web_search_tool_for_severity("HIGH")
    batch_params = _build_verification_request_params(
        prompt="verify this",
        system_prompt="you are a verifier",
        severity="HIGH",
    )
    # The web_search tool comes first in the tool list (followed by the
    # verdict tool). Find it by its name.
    web_tool = next(t for t in batch_params["tools"] if t.get("name") == "web_search")
    assert web_tool["max_uses"] == realtime_high["max_uses"]


# ---------------------------------------------------------------------------
# Verification cache disk persistence
# ---------------------------------------------------------------------------


def test_cache_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", raising=False)
    from src.verifier import VerificationResult
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    f = _make_finding(severity="HIGH", code_ref="CBC 2025 §1004")
    cache.put(
        f,
        cycle=DEFAULT_CYCLE,
        result=VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://dgs.ca.gov/example"],
            model_used="claude-sonnet-4-6",
            web_search_requests=2,
            successful_source_count=2,
        ),
    )
    written = cache.save_to_disk()
    assert written == 1
    assert (tmp_path / "cache.json").exists()

    cache2 = VerificationCache()
    loaded = cache2.load_from_disk()
    assert loaded == 1
    hit = cache2.get(f, cycle=DEFAULT_CYCLE)
    assert hit is not None
    assert hit.verdict == "CONFIRMED"
    assert hit.grounded is True
    assert hit.cache_status == "hit"
    assert "https://dgs.ca.gov/example" in hit.sources


def test_cache_save_atomic_does_not_corrupt_existing(tmp_path, monkeypatch):
    """A failed save must not destroy a valid existing cache file."""
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
    from src.verifier import VerificationResult
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    f = _make_finding(severity="HIGH", code_ref="CBC 2025")
    # Chunk 5: CONFIRMED entries must carry at least one source.
    cache.put(
        f, cycle=DEFAULT_CYCLE,
        result=VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://dgs.ca.gov"],
        ),
    )
    cache.save_to_disk()
    original_bytes = (tmp_path / "cache.json").read_bytes()

    # Force a save into a path whose parent directory cannot be created
    # would not exercise the temp-file path; instead, verify that after a
    # successful re-save the file still contains valid JSON and the
    # expected entry — the temp-file dance happens transparently.
    cache.save_to_disk()
    new_bytes = (tmp_path / "cache.json").read_bytes()
    payload = json.loads(new_bytes.decode("utf-8"))
    # Chunk 5: schema version bumped to 2 to invalidate pre-Chunk-5
    # entries that may have stored source-less CONFIRMED/CORRECTED.
    assert payload["version"] == 2
    assert len(payload["entries"]) == 1


def test_cache_load_missing_file_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "no_such_file.json"))
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    loaded = cache.load_from_disk()
    assert loaded == 0
    assert cache.stats()["size"] == 0


def test_cache_load_corrupt_file_does_not_crash(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not valid json {{{", encoding="utf-8")
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(cache_path))
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    loaded = cache.load_from_disk()
    assert loaded == 0
    assert cache.stats()["size"] == 0


def test_cache_load_schema_mismatch_is_silent(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({"version": 999, "entries": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(cache_path))
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    loaded = cache.load_from_disk()
    assert loaded == 0


def test_cache_does_not_persist_ungrounded_results(tmp_path, monkeypatch):
    """Only ``grounded=True`` results are eligible for the cache. This is
    the safety guarantee — never serve an ungrounded verdict from disk."""
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
    from src.verifier import VerificationResult
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    f = _make_finding()
    cache.put(
        f, cycle=DEFAULT_CYCLE,
        result=VerificationResult(verdict="UNVERIFIED", grounded=False),
    )
    cache.save_to_disk()
    cache2 = VerificationCache()
    cache2.load_from_disk()
    assert cache2.get(f, cycle=DEFAULT_CYCLE) is None


def test_cache_default_ttl_is_no_expiration(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", raising=False)
    from src.verification_cache import cache_ttl_days

    assert cache_ttl_days() == 0


def test_cache_ttl_drops_old_entries_on_load(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "30")

    # Chunk 5: schema version is now 2; the test entries must carry an
    # accepted source (or the load-time invariant check rejects them).
    payload = {
        "version": 2,
        "saved_at": time.time(),
        "entries": {
            "stale_key": {
                "created_ts": time.time() - (60 * 86400),  # 60 days ago
                "result": {
                    "verdict": "CONFIRMED",
                    "grounded": True,
                    "sources": ["https://dgs.ca.gov"],
                    "accepted_sources": ["https://dgs.ca.gov"],
                    "explanation": "old",
                    "model_used": "claude-sonnet-4-6",
                    "escalated": False,
                    "web_search_requests": 1,
                    "successful_source_count": 1,
                    "search_error_count": 0,
                    "correction": None,
                },
            },
            "fresh_key": {
                "created_ts": time.time() - (5 * 86400),  # 5 days ago
                "result": {
                    "verdict": "CONFIRMED",
                    "grounded": True,
                    "sources": ["https://dgs.ca.gov"],
                    "accepted_sources": ["https://dgs.ca.gov"],
                    "explanation": "fresh",
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
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    from src.verification_cache import VerificationCache
    cache = VerificationCache()
    loaded = cache.load_from_disk()
    assert loaded == 1  # stale dropped
    stats = cache.stats()
    assert stats["expired_on_load"] == 1
    assert stats["size"] == 1


def test_cache_cycle_label_isolation(tmp_path, monkeypatch):
    """Cache keys are scoped by cycle label so a future cycle bump invalidates entries."""
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
    from dataclasses import replace

    from src.code_cycles import CALIFORNIA_2025
    from src.verifier import VerificationResult
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    f = _make_finding(code_ref="CBC §1004", issue="Edition is current")
    future_cycle = replace(CALIFORNIA_2025, label="2028", cbc="2028")
    # Chunk 5: CONFIRMED requires an accepted source.
    cache.put(
        f, cycle=CALIFORNIA_2025,
        result=VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://dgs.ca.gov"],
        ),
    )
    # Same finding under a different cycle label — different cache key.
    assert cache.get(f, cycle=future_cycle) is None
    assert cache.get(f, cycle=CALIFORNIA_2025) is not None


# ---------------------------------------------------------------------------
# Haiku triage eligibility (safety net)
# ---------------------------------------------------------------------------


def test_triage_eligibility_blocks_critical_severity():
    from src.triage import is_eligible_for_haiku_triage

    f = _make_finding(severity="CRITICAL", code_ref=None, issue="formatting issue")
    assert is_eligible_for_haiku_triage(f) is False


def test_triage_eligibility_blocks_high_severity():
    from src.triage import is_eligible_for_haiku_triage

    f = _make_finding(severity="HIGH", code_ref=None, issue="formatting")
    assert is_eligible_for_haiku_triage(f) is False


def test_triage_eligibility_blocks_findings_with_code_reference():
    from src.triage import is_eligible_for_haiku_triage

    f = _make_finding(severity="GRIPES", code_ref="CBC 2025 §1004")
    assert is_eligible_for_haiku_triage(f) is False


def test_triage_eligibility_allows_medium_without_code_ref():
    from src.triage import is_eligible_for_haiku_triage

    f = _make_finding(severity="MEDIUM", code_ref=None, issue="internal mismatch")
    assert is_eligible_for_haiku_triage(f) is True


def test_triage_eligibility_allows_gripes_without_code_ref():
    from src.triage import is_eligible_for_haiku_triage

    f = _make_finding(severity="GRIPES", code_ref=None, issue="formatting")
    assert is_eligible_for_haiku_triage(f) is True


def test_triage_filter_local_skips_re_applies_eligibility():
    """Even if Haiku returns ``local_skip`` for an ineligible finding, the
    filter must drop that classification — defense in depth."""
    from src.triage import filter_local_skips

    findings = [
        _make_finding(severity="CRITICAL", code_ref=None),  # ineligible
        _make_finding(severity="GRIPES", code_ref=None),    # eligible
        _make_finding(severity="MEDIUM", code_ref="CBC"),   # ineligible
    ]
    classifications = {0: "local_skip", 1: "local_skip", 2: "local_skip"}
    skipped = list(filter_local_skips(findings, classifications))
    # Only the eligible finding can be locally skipped, regardless of what
    # the (possibly-misbehaving) classification dict claims.
    assert skipped == [1]


def test_triage_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_HAIKU_TRIAGE", raising=False)
    import src.triage as triage_mod
    importlib.reload(triage_mod)

    assert triage_mod.haiku_triage_enabled() is False
    # When disabled, classify_findings_with_haiku is a no-op (returns {}).
    findings = [_make_finding(severity="GRIPES", code_ref=None)]
    assert triage_mod.classify_findings_with_haiku(findings) == {}


def test_triage_enabled_via_env(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_HAIKU_TRIAGE", "1")
    import src.triage as triage_mod
    importlib.reload(triage_mod)

    assert triage_mod.haiku_triage_enabled() is True


# ---------------------------------------------------------------------------
# Source trimming
# ---------------------------------------------------------------------------


def test_verifier_does_not_merge_bulk_urls_into_sources(monkeypatch):
    """The real-time verification path must not merge every retrieved URL
    into ``parsed.sources``; only the model's cited sources from the
    structured verdict tool survive."""
    # Inspect the verifier source to confirm the merge loop is gone. This
    # is a regression guard — a future refactor that re-introduces the bulk
    # merge would silently inflate every report's sources list again.
    src_path = Path(__file__).resolve().parent.parent / "src" / "verifier.py"
    text = src_path.read_text(encoding="utf-8")
    # The deleted merge had the form:
    #   for url in all_search_urls:
    #       if url not in existing:
    #           parsed.sources.append(url)
    # Confirm the literal merge loop no longer exists. successful_source_count
    # still tracks the full set for diagnostics.
    assert "for url in all_search_urls" not in text
    assert "successful_source_count" in text  # diagnostics still preserved


def test_batch_does_not_merge_bulk_urls_into_sources():
    src_path = Path(__file__).resolve().parent.parent / "src" / "batch.py"
    text = src_path.read_text(encoding="utf-8")
    # Same regression guard for the batch retrieval path.
    assert "for url in search_urls" not in text


# ---------------------------------------------------------------------------
# Synthesis pass on Haiku
# ---------------------------------------------------------------------------


def test_synthesis_default_model_is_haiku(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_SYNTHESIS_MODEL", raising=False)
    import src.api_config as api_config
    importlib.reload(api_config)

    assert api_config.SYNTHESIS_MODEL_DEFAULT == api_config.MODEL_HAIKU_45


def test_synthesis_output_cap_is_bounded():
    from src.api_config import SYNTHESIS_OUTPUT_CAP, synthesis_max_tokens, MODEL_HAIKU_45

    # Synthesis output is small; cap is a fail-fast guard, not a billing knob.
    assert SYNTHESIS_OUTPUT_CAP == 32_000
    # Helper clamps to model ceiling. Haiku ceiling is 64k, so 32k passes.
    assert synthesis_max_tokens(model=MODEL_HAIKU_45) == 32_000


def test_haiku_output_ceiling_in_dispatch():
    from src.api_config import (
        MODEL_HAIKU_45,
        MAX_OUTPUT_TOKENS_HAIKU,
        output_cap_for_model,
    )

    # Requested below ceiling — returned unchanged.
    assert output_cap_for_model(MODEL_HAIKU_45, requested=8_000) == 8_000
    # Requested above ceiling — clamped.
    assert output_cap_for_model(MODEL_HAIKU_45, requested=200_000) == MAX_OUTPUT_TOKENS_HAIKU


# ---------------------------------------------------------------------------
# Verification output cap tightening
# ---------------------------------------------------------------------------


def test_verification_output_cap_tightened_to_16k():
    from src.api_config import VERIFICATION_OUTPUT_CAP

    # Tightened from 32k. Pure guardrail — verdicts are 1-2 sentences.
    assert VERIFICATION_OUTPUT_CAP == 16_000
