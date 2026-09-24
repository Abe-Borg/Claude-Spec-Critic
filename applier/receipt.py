"""The receipt: what was applied, what was withheld, and why.

A reviewer's real question after a run is not "how many edits landed?" but
"what did this program decide on my behalf, and what is still mine to do?".
So the receipt accounts for **every** entry the sidecar listed — applied,
held, unlocated, malformed, in a file that was not supplied, or in a file the
applier would not bind or write safely — and a count of entries in never
exceeds the count out. An applier that quietly processes 19 of 23 instructions
is worse than one that fails, because the missing four look like clean specs.

Two artifacts, one computation: a machine-readable JSON receipt for a
downstream tool or a CI job, and a text summary for the person at the
terminal. The summary is ordered by what needs a human — unapplied first,
applied last — because the applied ones are already visible in Word.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import FileResult, Outcome, OutcomeStatus

#: Printed before the outcomes that still need a person. The input holds lead:
#: they stopped a whole document, and fixing the invocation is the remedy.
_ATTENTION = (
    OutcomeStatus.FILE_AMBIGUOUS,
    OutcomeStatus.DESTINATION_CONFLICT,
    OutcomeStatus.UNLOCATED,
    OutcomeStatus.FAILED,
    OutcomeStatus.MALFORMED,
    OutcomeStatus.FILE_MISSING,
    OutcomeStatus.HELD_BY_POLICY,
)


def _counts(outcomes: list[Outcome]) -> dict[str, int]:
    counter = Counter(outcome.status.value for outcome in outcomes)
    return {status.value: counter.get(status.value, 0) for status in OutcomeStatus}


def build_receipt(
    *,
    sidecar,
    file_results: list[FileResult],
    settings: dict[str, Any],
    entries_in: int,
) -> dict[str, Any]:
    """Assemble the machine-readable receipt."""
    outcomes = [outcome for result in file_results for outcome in result.outcomes]
    counts = _counts(outcomes)
    return {
        "applier_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sidecar": {
            "path": str(sidecar.path),
            "schema_version": sidecar.schema_version,
            "generated_at": sidecar.generated_at,
            "report_file": sidecar.report_file,
            "program_id": sidecar.program_id,
            "cycle_label": sidecar.cycle_label,
            "project": sidecar.project,
            # Repeated from the sidecar rather than summarized: a run whose
            # coverage figures were normalized from inconsistent state must
            # not be reported downstream as a clean one.
            "integrity_warnings": sidecar.integrity_warnings,
        },
        "settings": settings,
        "accounting": {
            "entries_in_sidecar": entries_in,
            "entries_accounted_for": len(outcomes),
            "balanced": len(outcomes) == entries_in,
            "by_outcome": counts,
        },
        "files": [
            {
                "file_name": result.file_name,
                "source_path": result.source_path,
                "output_path": result.output_path,
                "applied": result.applied,
                "errors": result.errors,
                "candidate_paths": result.candidate_paths,
                "outcomes": [outcome.to_dict() for outcome in result.outcomes],
            }
            for result in file_results
        ],
    }


def write_receipt(receipt: dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _line(outcome: Outcome) -> str:
    entry = outcome.entry
    head = f"    [{entry.severity or '—'}] {entry.finding_id} {entry.action_type}"
    detail = outcome.change_note or outcome.reason
    location = outcome.location
    where = ""
    if location is not None and location.element_id:
        where = f" @{location.element_id}"
    return f"{head}{where}\n        {detail}" if detail else f"{head}{where}"


def render_summary(receipt: dict[str, Any], file_results: list[FileResult]) -> str:
    """The terminal summary. Unapplied first — that is the actionable half."""
    accounting = receipt["accounting"]
    counts = accounting["by_outcome"]
    settings = receipt["settings"]
    lines: list[str] = []

    lines.append("Spec Critic — Edit Applier")
    lines.append(
        f"  sidecar        {receipt['sidecar']['path']} "
        f"(schema v{receipt['sidecar']['schema_version']})"
    )
    lines.append(
        f"  mode           {settings['mode']}"
        + ("  [DRY RUN — nothing written]" if settings["dry_run"] else "")
    )
    lines.append(
        f"  policy         {settings['policy']}"
        + (
            f"  (+forced: {', '.join(settings['force_statuses'])})"
            if settings["force_statuses"]
            else ""
        )
    )
    lines.append(f"  assist         {'on' if settings['assist'] else 'off'}")

    for warning in receipt["sidecar"]["integrity_warnings"]:
        lines.append(f"  ! sidecar integrity warning: {warning}")

    lines.append("")
    verb = "would apply" if settings["dry_run"] else "applied"
    applied_key = (
        OutcomeStatus.WOULD_APPLY.value
        if settings["dry_run"]
        else OutcomeStatus.APPLIED.value
    )
    lines.append(
        f"  {counts[applied_key]} of {accounting['entries_in_sidecar']} "
        f"instructions {verb}."
    )
    for status in _ATTENTION:
        count = counts[status.value]
        if count:
            lines.append(f"  {count} {status.value.lower().replace('_', ' ')}")
    if not accounting["balanced"]:
        lines.append(
            f"  ! accounting mismatch: {accounting['entries_accounted_for']} "
            f"outcomes for {accounting['entries_in_sidecar']} entries"
        )

    for result in file_results:
        lines.append("")
        lines.append(f"  {result.file_name}")
        if result.output_path:
            lines.append(f"    -> {result.output_path}")
        for error in result.errors:
            lines.append(f"    ! {error}")
        needs_attention = [o for o in result.outcomes if o.status in _ATTENTION]
        done = [
            o
            for o in result.outcomes
            if o.status in (OutcomeStatus.APPLIED, OutcomeStatus.WOULD_APPLY)
        ]
        if needs_attention:
            lines.append("    Not applied:")
            lines.extend(_line(outcome) for outcome in needs_attention)
        if done:
            lines.append(f"    {verb.capitalize()}:")
            lines.extend(_line(outcome) for outcome in done)

    if not settings["dry_run"] and any(r.output_path for r in file_results):
        lines.append("")
        if settings["mode"] == "tracked":
            lines.append(
                "  Edits were written as Word tracked changes. Open the "
                "*.applied.docx files and use Review > Accept/Reject to "
                "decide each one. The source files were not modified."
            )
        else:
            lines.append(
                "  Edits were written directly, with no revision marks. The "
                "source files were not modified; compare against them before "
                "issuing."
            )
    return "\n".join(lines)
