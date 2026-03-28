"""In-memory diagnostics report for Spec Critic pipeline runs."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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

    def log(self, phase: str, level: str, message: str, data: Optional[dict] = None) -> None:
        self.events.append(DiagnosticEvent(
            timestamp=time.time(),
            elapsed=time.time() - self.started_at,
            phase=phase,
            level=level,
            message=message,
            data=data,
        ))

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
        for e in self.events:
            if e.data:
                total_input_tokens += e.data.get("input_tokens", 0)
                total_output_tokens += e.data.get("output_tokens", 0)

        # Verification verdict breakdown
        verdicts: dict[str, int] = {}
        for e in self.events:
            if e.data and "verdict" in e.data:
                v = e.data["verdict"]
                verdicts[v] = verdicts.get(v, 0) + 1

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
            "verification_verdicts": verdicts,
            "severity_counts": severities,
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
        if s["severity_counts"]:
            lines.append(f"  Findings:        {s['severity_counts']}")
        if s["verification_verdicts"]:
            lines.append(f"  Verdicts:        {s['verification_verdicts']}")
        if s["phase_durations"]:
            lines.append("  Phase Durations:")
            for phase, dur in s["phase_durations"].items():
                lines.append(f"    {phase:20s} {dur:.1f}s")
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
