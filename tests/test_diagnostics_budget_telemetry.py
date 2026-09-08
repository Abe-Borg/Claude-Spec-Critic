"""Diagnostics telemetry fixes (B-13b / B-13d) plus shared-verdict accounting.

* Search-budget saturation is judged per verdict event against the budget for
  *that finding's* severity (CRITICAL 8 / HIGH 7 / MEDIUM 5 / GRIPES 3), not
  the flat default of 5 — a CRITICAL at 6 of 8 is not saturated, a GRIPES at
  3 of 3 is.
* Count-based eviction (``max_events``) now adds the evicted payload to
  ``bytes_dropped`` exactly as the byte-cap eviction does.
* A single-flight follower's ``cache_status="shared"`` verdict is neither a
  disk replay nor a fresh call: it is counted apart from ``cache_hits`` /
  ``cache_misses``, excluded from the search-budget samples, and never
  inflates the per-phase call rollup even when the recorder stamped
  ``api_call`` / ``model`` on the event.
"""
from __future__ import annotations

from src.core.api_config import _SEVERITY_MAX_USES, web_search_max_uses_for_severity
from src.orchestration.diagnostics import DiagnosticsReport, _event_data_byte_size


def _verdict_event(
    report: DiagnosticsReport,
    *,
    requests: int,
    severity: str | None = "HIGH",
    cache_status: str = "miss",
    **extra,
) -> None:
    data = {
        "verdict": "UNVERIFIED",
        "web_search_requests": requests,
        "cache_status": cache_status,
        # Mirrors the GUI recorder: only hit / local_skip are flagged as
        # non-calls there, so a shared verdict arrives with api_call=True.
        "api_call": cache_status not in ("hit", "local_skip"),
        "model": "unit-test-verifier",
        "input_tokens": 0,
        "output_tokens": 0,
    }
    if severity is not None:
        data["finding_severity"] = severity
    data.update(extra)
    report.log("verification", "info", "verdict", data)


# ---------------------------------------------------------------------------
# B-13b — severity-tiered saturation
# ---------------------------------------------------------------------------


def test_critical_at_six_of_eight_is_not_saturated():
    report = DiagnosticsReport()
    _verdict_event(report, severity="CRITICAL", requests=6)
    budget = report.summary()["search_budget"]
    assert budget["samples"] == 1
    assert budget["saturated_calls"] == 0


def test_gripes_at_three_of_three_is_saturated():
    report = DiagnosticsReport()
    _verdict_event(report, severity="GRIPES", requests=3)
    budget = report.summary()["search_budget"]
    assert budget["samples"] == 1
    assert budget["saturated_calls"] == 1


def test_every_tier_is_judged_against_its_own_budget():
    report = DiagnosticsReport()
    for severity, uses in _SEVERITY_MAX_USES.items():
        _verdict_event(report, severity=severity, requests=uses)       # saturated
        _verdict_event(report, severity=severity, requests=uses - 1)   # not
    budget = report.summary()["search_budget"]
    assert budget["samples"] == 2 * len(_SEVERITY_MAX_USES)
    assert budget["saturated_calls"] == len(_SEVERITY_MAX_USES)
    assert budget["ceiling_by_severity"] == dict(_SEVERITY_MAX_USES)
    assert budget["ceiling"] == max(_SEVERITY_MAX_USES.values())


def test_event_without_severity_falls_back_to_the_default_budget():
    report = DiagnosticsReport()
    default_budget = web_search_max_uses_for_severity(None)
    _verdict_event(report, severity=None, requests=default_budget)
    _verdict_event(report, severity=None, requests=default_budget - 1)
    assert report.summary()["search_budget"]["saturated_calls"] == 1


def test_severity_key_is_case_insensitive():
    report = DiagnosticsReport()
    _verdict_event(report, severity="gripes", requests=3)
    assert report.summary()["search_budget"]["saturated_calls"] == 1


# ---------------------------------------------------------------------------
# B-13d — count-based eviction accounts dropped bytes
# ---------------------------------------------------------------------------


def test_count_based_eviction_increments_bytes_dropped():
    report = DiagnosticsReport(max_events=2, max_total_data_bytes=0)
    payload = {"api_call": True, "model": "unit-test-model", "note": "x" * 64}
    for index in range(3):
        report.log("review", "info", f"event {index}", dict(payload))

    size = _event_data_byte_size(report.events[0].data)
    summary = report.summary()
    assert summary["total_events"] == 2
    assert summary["events_dropped"] == 1
    assert summary["bytes_dropped"] == size
    assert summary["total_data_bytes"] == 2 * size


def test_eviction_accounting_agrees_across_both_caps():
    """Whichever cap evicts, dropped events and dropped bytes stay in lockstep."""
    report = DiagnosticsReport(max_events=3, max_total_data_bytes=400)
    payload = {"api_call": True, "note": "y" * 150}
    for index in range(8):
        report.log("review", "info", f"event {index}", dict(payload))
    size = _event_data_byte_size(report.events[0].data)
    summary = report.summary()
    assert summary["events_dropped"] + summary["total_events"] == 8
    assert summary["bytes_dropped"] == summary["events_dropped"] * size


# ---------------------------------------------------------------------------
# Shared verdicts are neither replays nor calls
# ---------------------------------------------------------------------------


def test_shared_verdicts_are_counted_apart_from_hits_and_misses():
    report = DiagnosticsReport()
    _verdict_event(report, severity="MEDIUM", requests=3, cache_status="miss",
                   input_tokens=900, output_tokens=200)
    _verdict_event(report, severity="MEDIUM", requests=3, cache_status="shared")
    _verdict_event(report, severity="MEDIUM", requests=3, cache_status="shared")
    # ``model=""`` keeps this pin independent of a pre-existing quirk: a hit
    # event that names a model already counts as a call in the phase rollup.
    _verdict_event(report, severity="MEDIUM", requests=0, cache_status="hit", model="")

    summary = report.summary()
    evidence = summary["verification_evidence"]
    assert evidence["cache_misses"] == 1
    assert evidence["cache_hits"] == 1
    assert evidence["shared_verdicts"] == 2
    # The leader's searches are counted once, not once per follower.
    assert evidence["search_requests"] == 3
    # Budget samples reflect calls that actually used the tool.
    assert summary["search_budget"]["samples"] == 1
    # The per-phase rollup counts the leader's call only.
    phases = summary["cost_summary"]["phases"]
    assert phases["verification"]["calls"] == 1
    assert phases["verification"]["input_tokens"] == 900
    assert "shared=2" in report.to_text()
