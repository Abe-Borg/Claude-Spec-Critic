"""In-memory diagnostics report for Spec Critic pipeline runs."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Phase 7.3 (audit Section 11.3): cap retained events so a long-running batch
# poll cannot grow the in-memory report unbounded. Truncation tracking lets
# the report still surface that older events were dropped.
_DEFAULT_MAX_EVENTS = 5000


@dataclass
class DiagnosticEvent:
    timestamp: float
    elapsed: float
    phase: str
    level: str
    message: str
    data: Optional[dict] = None


@dataclass
class DiagnosticsReport:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    mode: str = ""
    model: str = ""
    cycle_label: str = ""
    files_selected: list[str] = field(default_factory=list)
    project_context_tokens: int = 0
    cross_check_enabled: bool = False
    export_mode: bool = False
    events: list[DiagnosticEvent] = field(default_factory=list)
    # Phase 7.3 actionable fields. Populated by the pipeline / GUI when the
    # corresponding phase records actionable failure or skip information.
    failed_specs: list[str] = field(default_factory=list)
    skipped_specs: list[str] = field(default_factory=list)
    edit_skip_reasons: dict[str, int] = field(default_factory=dict)
    ambiguous_locator_count: int = 0
    edits_applied_total: int = 0
    edits_skipped_total: int = 0
    edits_failed_total: int = 0
    max_events: int = _DEFAULT_MAX_EVENTS
    events_dropped: int = 0

    def log(self, phase: str, level: str, message: str, data: Optional[dict] = None) -> None:
        # Cap the event list to bound memory on long-running batch polls.
        # When the cap is exceeded, drop the oldest event and remember that
        # truncation happened so the summary can flag it.
        if self.max_events > 0 and len(self.events) >= self.max_events:
            self.events.pop(0)
            self.events_dropped += 1
        self.events.append(DiagnosticEvent(
            timestamp=time.time(),
            elapsed=time.time() - self.started_at,
            phase=phase,
            level=level,
            message=message,
            data=data,
        ))

    def record_failed_spec(self, filename: str) -> None:
        if filename and filename not in self.failed_specs:
            self.failed_specs.append(filename)

    def record_skipped_spec(self, filename: str) -> None:
        if filename and filename not in self.skipped_specs:
            self.skipped_specs.append(filename)

    def record_edit_skip(self, reason: str) -> None:
        if not reason:
            return
        self.edit_skip_reasons[reason] = self.edit_skip_reasons.get(reason, 0) + 1
        if reason == "ambiguous":
            self.ambiguous_locator_count += 1

    def record_edit_report(
        self,
        *,
        applied: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        self.edits_applied_total += int(applied)
        self.edits_skipped_total += int(skipped)
        self.edits_failed_total += int(failed)

    def finish(self) -> None:
        if self.ended_at is None:
            self.ended_at = time.time()

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        total_time = (self.ended_at or time.time()) - self.started_at
        error_events = [e for e in self.events if e.level == "error"]
        warning_events = [e for e in self.events if e.level == "warning"]
        success_events = [e for e in self.events if e.level == "success"]

        # Phase durations: first event to last event per phase
        phase_times: dict[str, dict] = {}
        for e in self.events:
            if e.phase not in phase_times:
                phase_times[e.phase] = {"start": e.elapsed, "end": e.elapsed}
            else:
                phase_times[e.phase]["end"] = e.elapsed

            if e.data and any(key in e.data for key in ("verdict", "confidence")):
                synthetic = phase_times.setdefault("verification", {"start": e.elapsed, "end": e.elapsed})
                synthetic["start"] = min(synthetic["start"], e.elapsed)
                synthetic["end"] = max(synthetic["end"], e.elapsed)
        phase_durations = {
            p: round(t["end"] - t["start"], 2) for p, t in phase_times.items()
        }

        # Aggregate token data from events
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        # Phase 9 plan 13.4: output-size and search-budget telemetry. We track
        # the maximum output observed per phase, the count of truncated calls
        # (stop_reason != end_turn), and aggregate search budget consumption
        # so future tuning has data to draw on.
        output_samples: list[int] = []
        output_max_by_phase: dict[str, int] = {}
        truncated_calls = 0
        truncated_phases: dict[str, int] = {}
        max_output_cap_observed = 0
        for e in self.events:
            if not e.data:
                continue
            in_tok = int(e.data.get("input_tokens", 0) or 0)
            out_tok = int(e.data.get("output_tokens", 0) or 0)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            total_cache_creation_tokens += int(e.data.get("cache_creation_input_tokens", 0) or 0)
            total_cache_read_tokens += int(e.data.get("cache_read_input_tokens", 0) or 0)
            if out_tok > 0:
                output_samples.append(out_tok)
                phase_max = output_max_by_phase.get(e.phase, 0)
                if out_tok > phase_max:
                    output_max_by_phase[e.phase] = out_tok
            stop_reason = e.data.get("stop_reason")
            if stop_reason and stop_reason not in ("end_turn", "tool_use", None):
                truncated_calls += 1
                truncated_phases[e.phase] = truncated_phases.get(e.phase, 0) + 1
            cap = int(e.data.get("max_output_tokens", 0) or 0)
            if cap > max_output_cap_observed:
                max_output_cap_observed = cap

        # Verification verdict breakdown + Phase 3 evidence telemetry
        verdicts: dict[str, int] = {}
        verification_stats = {
            "grounded": 0,
            "ungrounded": 0,
            "escalated": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "local_skips": 0,
            "search_errors": 0,
            "search_requests": 0,
        }
        for e in self.events:
            if e.data and "verdict" in e.data:
                v = e.data["verdict"]
                verdicts[v] = verdicts.get(v, 0) + 1
                # Optional Phase 3 fields. Missing keys are simply ignored.
                if e.data.get("grounded") is True:
                    verification_stats["grounded"] += 1
                elif "grounded" in e.data:
                    verification_stats["ungrounded"] += 1
                if e.data.get("escalated") is True:
                    verification_stats["escalated"] += 1
                cs = e.data.get("cache_status")
                if cs == "hit":
                    verification_stats["cache_hits"] += 1
                elif cs == "miss":
                    verification_stats["cache_misses"] += 1
                elif cs == "local_skip":
                    verification_stats["local_skips"] += 1
                verification_stats["search_errors"] += int(e.data.get("search_error_count", 0) or 0)
                verification_stats["search_requests"] += int(e.data.get("web_search_requests", 0) or 0)

        # Phase 9 plan 13.4: search-budget telemetry. We aggregate per-finding
        # search-request counts so a future tuning pass can see whether the
        # default ``max_uses`` is over- or under-allocated. Findings with zero
        # web-search activity (local-skip / cache hit) are excluded so the
        # budget percentile reflects calls that actually used the tool.
        search_budget_samples: list[int] = []
        budget_ceiling = 0
        try:
            from .api_config import DEFAULT_VERIFICATION_MAX_USES
            budget_ceiling = int(DEFAULT_VERIFICATION_MAX_USES)
        except Exception:
            budget_ceiling = 0
        budget_saturated = 0
        for e in self.events:
            if not e.data or "verdict" not in e.data:
                continue
            if (e.data.get("cache_status") or "") in {"hit", "local_skip"}:
                continue
            requests = int(e.data.get("web_search_requests", 0) or 0)
            if requests <= 0:
                continue
            search_budget_samples.append(requests)
            if budget_ceiling and requests >= budget_ceiling:
                budget_saturated += 1

        def _percentile(values: list[int], pct: float) -> int:
            if not values:
                return 0
            ordered = sorted(values)
            idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
            return ordered[idx]

        search_budget = {
            "samples": len(search_budget_samples),
            "ceiling": budget_ceiling,
            "saturated_calls": budget_saturated,
            "max_observed": max(search_budget_samples) if search_budget_samples else 0,
            "p50": _percentile(search_budget_samples, 50),
            "p95": _percentile(search_budget_samples, 95),
            "total": sum(search_budget_samples),
        }
        output_telemetry = {
            "samples": len(output_samples),
            "max_observed": max(output_samples) if output_samples else 0,
            "p50": _percentile(output_samples, 50),
            "p95": _percentile(output_samples, 95),
            "max_by_phase": dict(output_max_by_phase),
            "truncated_calls": truncated_calls,
            "truncated_by_phase": dict(truncated_phases),
            "max_cap_observed": max_output_cap_observed,
        }

        # Finding severity breakdown
        severities: dict[str, int] = {}
        for e in self.events:
            if e.data and "severity_counts" in e.data:
                for sev, cnt in e.data["severity_counts"].items():
                    severities[sev] = severities.get(sev, 0) + cnt

        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "cycle_label": self.cycle_label,
            "total_time_seconds": round(total_time, 2),
            "files_selected": len(self.files_selected),
            "total_events": len(self.events),
            "errors": len(error_events),
            "warnings": len(warning_events),
            "successes": len(success_events),
            "phase_durations": phase_durations,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_creation_input_tokens": total_cache_creation_tokens,
            "total_cache_read_input_tokens": total_cache_read_tokens,
            "verification_verdicts": verdicts,
            "verification_evidence": verification_stats,
            "search_budget": search_budget,
            "output_telemetry": output_telemetry,
            "severity_counts": severities,
            # Phase 7.3 actionable fields.
            "failed_specs": list(self.failed_specs),
            "skipped_specs": list(self.skipped_specs),
            "edit_skip_reasons": dict(self.edit_skip_reasons),
            "ambiguous_locator_count": self.ambiguous_locator_count,
            "edits_applied_total": self.edits_applied_total,
            "edits_skipped_total": self.edits_skipped_total,
            "edits_failed_total": self.edits_failed_total,
            "events_dropped": self.events_dropped,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _event_to_dict(self, e: DiagnosticEvent) -> dict:
        d: dict = {
            "timestamp": e.timestamp,
            "elapsed": round(e.elapsed, 3),
            "phase": e.phase,
            "level": e.level,
            "message": e.message,
        }
        if e.data:
            d["data"] = e.data
        return d

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "started_at_iso": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "ended_at": self.ended_at,
            "ended_at_iso": datetime.fromtimestamp(self.ended_at, tz=timezone.utc).isoformat() if self.ended_at else None,
            "mode": self.mode,
            "model": self.model,
            "cycle_label": self.cycle_label,
            "files_selected": self.files_selected,
            "project_context_tokens": self.project_context_tokens,
            "cross_check_enabled": self.cross_check_enabled,
            "export_mode": self.export_mode,
            "summary": self.summary(),
            "events": [self._event_to_dict(e) for e in self.events],
        }

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("SPEC CRITIC — DIAGNOSTICS REPORT")
        lines.append("=" * 72)
        lines.append("")

        # Config
        lines.append("RUN CONFIGURATION")
        lines.append("-" * 40)
        lines.append(f"  Run ID:          {self.run_id}")
        lines.append(f"  Mode:            {self.mode}")
        lines.append(f"  Model:           {self.model}")
        lines.append(f"  Code Cycle:      {self.cycle_label}")
        lines.append(f"  Files:           {len(self.files_selected)}")
        for f in self.files_selected:
            lines.append(f"                   - {f}")
        lines.append(f"  Context Tokens:  {self.project_context_tokens:,}")
        lines.append(f"  Cross-Check:     {'Yes' if self.cross_check_enabled else 'No'}")
        lines.append(f"  Export Mode:     {'Yes' if self.export_mode else 'No'}")
        started = datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"  Started:         {started}")
        if self.ended_at:
            ended = datetime.fromtimestamp(self.ended_at).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"  Ended:           {ended}")
            lines.append(f"  Duration:        {self.ended_at - self.started_at:.1f}s")
        lines.append("")

        # Summary
        s = self.summary()
        lines.append("SUMMARY")
        lines.append("-" * 40)
        lines.append(f"  Total Time:      {s['total_time_seconds']:.1f}s")
        lines.append(f"  Events:          {s['total_events']}")
        lines.append(f"  Errors:          {s['errors']}")
        lines.append(f"  Warnings:        {s['warnings']}")
        lines.append(f"  Input Tokens:    {s['total_input_tokens']:,}")
        lines.append(f"  Output Tokens:   {s['total_output_tokens']:,}")
        cache_create = s.get("total_cache_creation_input_tokens", 0)
        cache_read = s.get("total_cache_read_input_tokens", 0)
        if cache_create or cache_read:
            lines.append(f"  Cache Creation:  {cache_create:,} tokens")
            lines.append(f"  Cache Read:      {cache_read:,} tokens")
        if s["severity_counts"]:
            lines.append(f"  Findings:        {s['severity_counts']}")
        if s["verification_verdicts"]:
            lines.append(f"  Verdicts:        {s['verification_verdicts']}")
        evidence = s.get("verification_evidence")
        if evidence and any(evidence.values()):
            lines.append(
                "  Evidence:        "
                f"grounded={evidence['grounded']}, "
                f"ungrounded={evidence['ungrounded']}, "
                f"escalated={evidence['escalated']}, "
                f"cache_hits={evidence['cache_hits']}, "
                f"local_skips={evidence['local_skips']}, "
                f"search_errors={evidence['search_errors']}"
            )
        if s["phase_durations"]:
            lines.append("  Phase Durations:")
            for phase, dur in s["phase_durations"].items():
                lines.append(f"    {phase:20s} {dur:.1f}s")
        # Phase 9 plan 13.4: surface output-size and search-budget usage so
        # operators can see whether dynamic caps and ``max_uses`` defaults
        # match real workloads.
        out_t = s.get("output_telemetry") or {}
        if out_t.get("samples"):
            lines.append(
                "  Output Tokens (samples="
                f"{out_t['samples']}): max={out_t['max_observed']:,}, "
                f"p50={out_t['p50']:,}, p95={out_t['p95']:,}"
            )
            if out_t.get("truncated_calls"):
                lines.append(
                    f"    Truncated calls: {out_t['truncated_calls']}"
                    + (
                        f" — by phase: {out_t['truncated_by_phase']}"
                        if out_t.get("truncated_by_phase")
                        else ""
                    )
                )
        budget = s.get("search_budget") or {}
        if budget.get("samples"):
            ceiling = budget.get("ceiling") or 0
            saturated = budget.get("saturated_calls") or 0
            ceiling_part = f"/{ceiling}" if ceiling else ""
            saturated_part = (
                f", saturated={saturated}" if ceiling and saturated else ""
            )
            lines.append(
                f"  Search Budget (samples={budget['samples']}): "
                f"max={budget['max_observed']}{ceiling_part}, "
                f"p50={budget['p50']}, p95={budget['p95']}, "
                f"total={budget['total']}{saturated_part}"
            )
        # Phase 7.3 actionable section: surface failed specs, skipped edits,
        # ambiguous locator count, and event truncation so users can see
        # what required attention without scanning the timeline.
        if s.get("failed_specs"):
            lines.append("")
            lines.append(f"  Failed Specs:    {len(s['failed_specs'])}")
            for fname in s["failed_specs"]:
                lines.append(f"                   - {fname}")
        if s.get("skipped_specs"):
            lines.append(f"  Skipped Specs:   {len(s['skipped_specs'])}")
            for fname in s["skipped_specs"]:
                lines.append(f"                   - {fname}")
        if s.get("edit_skip_reasons"):
            lines.append("  Edit Skips:")
            for reason, cnt in s["edit_skip_reasons"].items():
                lines.append(f"    {reason:20s} {cnt}")
        if s.get("ambiguous_locator_count"):
            lines.append(f"  Ambiguous Locators: {s['ambiguous_locator_count']}")
        if (s.get("edits_applied_total") or s.get("edits_skipped_total")
                or s.get("edits_failed_total")):
            lines.append(
                "  Edit Application: "
                f"applied={s['edits_applied_total']}, "
                f"skipped={s['edits_skipped_total']}, "
                f"failed={s['edits_failed_total']}"
            )
        if s.get("events_dropped"):
            lines.append(
                f"  Events Dropped:  {s['events_dropped']} "
                f"(cap={self.max_events:,}; older events truncated)"
            )
        lines.append("")

        # Timeline
        lines.append("EVENT TIMELINE")
        lines.append("-" * 72)
        level_icons = {
            "info": " ",
            "success": "+",
            "warning": "!",
            "error": "X",
            "step": ">",
        }
        for e in self.events:
            icon = level_icons.get(e.level, " ")
            ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S")
            elapsed = f"{e.elapsed:8.2f}s"
            phase_tag = f"[{e.phase}]" if e.phase else ""
            lines.append(f"  {ts} {elapsed} {icon} {phase_tag:20s} {e.message}")
            if e.data:
                for k, v in e.data.items():
                    lines.append(f"{'':42s} {k}: {v}")
        lines.append("")
        lines.append("=" * 72)
        return "\n".join(lines)

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def save_text(self, path: str | Path) -> None:
        Path(path).write_text(self.to_text(), encoding="utf-8")
