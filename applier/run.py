"""Orchestration: sidecar + specifications in, edited copies + receipt out.

The run is deliberately shaped so that nothing is written until every
decision about a document has been made. Per file:

1. extract it with Spec Critic's own extractor (so element ids mean the same
   thing they did at review time),
2. decide policy and location for **every** entry against the unmutated
   document, resolving each applicable one to a live element,
3. only then apply, and only then save — to a new file.

``--dry-run`` runs steps 1-3 in full and skips only the save, so its report
is what a real run would do rather than an optimistic approximation of it.

Step 2 finishing before step 3 begins is not tidiness. Element ids are
positional, so the first inserted paragraph renumbers every later one; and a
document that is half-edited when an exception lands is the one outcome worse
than a document that is not edited at all.

Two refusals are enforced here rather than in the writer, because both are
properties of the *document* rather than of any one edit:

**A source with pending tracked changes is skipped.** The extractor reads the
accept-all view, so the text the reviewer saw is a version that does not
exist on disk. Layering new revisions on top of undecided ones produces a
document where nobody can tell which author proposed what.
``--allow-tracked-source`` overrides the document-level skip, but it is not a
blanket permission: an edit whose own target text sits inside an undecided
revision is still refused by the writer, so the override admits the clean
parts of a partly-redlined specification rather than the whole file.

**The source file is never the destination.** Checked by resolved path, so a
``--output-dir`` pointing at the source directory cannot alias onto it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from src.input.extractor import extract_text_from_docx

from .assist import AssistConfig, assist_location
from .docx_edit import TRACKED, DocumentEditor, EditError, EditMode
from .locator import build_candidates, locate
from .models import (
    EditEntry,
    FileResult,
    Outcome,
    OutcomeStatus,
)
from .policy import PolicyConfig

#: Appended to a source stem to name its edited copy.
DEFAULT_OUTPUT_SUFFIX = ".applied"


@dataclass(frozen=True)
class RunSettings:
    mode: EditMode = TRACKED
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    assist: AssistConfig = field(default_factory=AssistConfig)
    dry_run: bool = False
    allow_tracked_source: bool = False
    output_dir: Path | None = None
    output_suffix: str = DEFAULT_OUTPUT_SUFFIX

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.label,
            "policy": self.policy.policy.value,
            "min_edit_confidence": self.policy.min_edit_confidence,
            "force_statuses": sorted(self.policy.force_statuses),
            "only_finding_ids": sorted(self.policy.only_finding_ids),
            "assist": self.assist.enabled,
            "assist_model": self.assist.model if self.assist.enabled else None,
            "dry_run": self.dry_run,
            "allow_tracked_source": self.allow_tracked_source,
            "output_suffix": self.output_suffix,
        }


def _index_specs(spec_paths: Iterable[Path]) -> dict[str, Path]:
    """Map lower-cased file name to path, first occurrence winning."""
    index: dict[str, Path] = {}
    for path in spec_paths:
        key = path.name.casefold()
        index.setdefault(key, path)
    return index


def _group_by_file(entries: list[EditEntry]) -> dict[str, list[EditEntry]]:
    grouped: dict[str, list[EditEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.file_name, []).append(entry)
    return grouped


def _output_path(source: Path, settings: RunSettings) -> Path:
    directory = settings.output_dir or source.parent
    return Path(directory) / f"{source.stem}{settings.output_suffix}{source.suffix}"


def apply_sidecar(
    sidecar,
    spec_paths: Iterable[Path],
    settings: RunSettings,
    *,
    client=None,
    log: Callable[[str], None] = lambda _message: None,
) -> list[FileResult]:
    """Apply a loaded sidecar to the supplied specifications."""
    index = _index_specs([Path(p) for p in spec_paths])
    grouped = _group_by_file(sidecar.entries)

    # Malformed entries never reach a document, but they must still be
    # accounted for — file by file, so the receipt attributes them.
    malformed: dict[str, list[Outcome]] = {}
    for entry, problem in sidecar.malformed:
        malformed.setdefault(entry.file_name, []).append(
            Outcome(
                entry=entry,
                status=OutcomeStatus.MALFORMED,
                reason=f"this edit instruction is unusable: {problem}",
            )
        )

    results: list[FileResult] = []
    for file_name in dict.fromkeys(list(grouped) + list(malformed)):
        entries = grouped.get(file_name, [])
        result = FileResult(file_name=file_name)
        result.outcomes.extend(malformed.get(file_name, []))

        source = index.get(file_name.casefold())
        if source is None:
            result.errors.append(
                f"{file_name} was not among the specifications supplied"
            )
            for entry in entries:
                result.outcomes.append(
                    Outcome(
                        entry=entry,
                        status=OutcomeStatus.FILE_MISSING,
                        reason=(
                            "the specification this instruction targets was "
                            "not supplied to the applier"
                        ),
                    )
                )
            results.append(result)
            continue

        result.source_path = str(source)
        _apply_to_file(source, entries, result, settings, client=client, log=log)
        results.append(result)
    return results


def _hold_all(
    entries: list[EditEntry], result: FileResult, reason: str
) -> None:
    for entry in entries:
        result.outcomes.append(
            Outcome(entry=entry, status=OutcomeStatus.FAILED, reason=reason)
        )


def _apply_to_file(
    source: Path,
    entries: list[EditEntry],
    result: FileResult,
    settings: RunSettings,
    *,
    client,
    log: Callable[[str], None],
) -> None:
    from docx import Document

    try:
        extracted = extract_text_from_docx(source)
    except Exception as exc:  # noqa: BLE001 - a bad file is data, not a crash
        message = f"could not read {source.name}: {exc}"
        result.errors.append(message)
        _hold_all(entries, result, message)
        return

    if extracted.tracked_changes_detected and not settings.allow_tracked_source:
        message = (
            f"{source.name} already contains pending tracked changes. The "
            "review read its accept-all view, so applying on top would mix "
            "two authors' undecided revisions. Resolve them in Word, or pass "
            "--allow-tracked-source."
        )
        result.errors.append(message)
        _hold_all(entries, result, message)
        return

    for warning in extracted.extraction_warnings:
        result.errors.append(f"extraction warning: {warning}")

    candidates = build_candidates(extracted)
    document = Document(source)
    editor = DocumentEditor(document, mode=settings.mode)

    # --- Decide everything first, against the unmutated document ---------
    planned: list[tuple[Outcome, list]] = []
    for entry in entries:
        decision = settings.policy.decide(entry)
        if not decision.allowed:
            result.outcomes.append(
                Outcome(
                    entry=entry,
                    status=OutcomeStatus.HELD_BY_POLICY,
                    reason=decision.reason,
                )
            )
            continue

        location = locate(entry, candidates)
        if not location.is_applicable and settings.assist.enabled and client is not None:
            location = assist_location(
                entry, candidates, location, client=client, config=settings.assist, log=log
            )

        if not location.is_applicable:
            result.outcomes.append(
                Outcome(
                    entry=entry,
                    status=OutcomeStatus.UNLOCATED,
                    reason=location.detail or location.status.value,
                    location=location,
                )
            )
            continue

        outcome = Outcome(entry=entry, status=OutcomeStatus.WOULD_APPLY, location=location)
        try:
            resolved = editor.resolve(location)
        except EditError as exc:
            result.outcomes.append(
                Outcome(
                    entry=entry,
                    status=OutcomeStatus.UNLOCATED,
                    reason=str(exc),
                    location=location,
                )
            )
            continue
        planned.append((outcome, resolved))

    # --- Then write ---------------------------------------------------------
    # A dry run takes this same path and differs in exactly one way: the
    # document is never saved. Reporting WOULD_APPLY straight off the
    # locator was optimistic — it skipped every writer-level refusal (an
    # unresolvable nested-table path, a boundary inside an unsplittable
    # tab run, a target repeated within its element, text inside another
    # author's revision), so `--dry-run --strict` could exit 0 on work a
    # real run would refuse. A preview that is rosier than the thing it
    # previews is worse than no preview. Mutating the in-memory Document is
    # safe precisely because nothing below writes it back.
    for outcome, resolved in planned:
        try:
            note = editor.apply_resolved(outcome.entry, resolved)
        except EditError as exc:
            outcome.status = OutcomeStatus.UNLOCATED
            outcome.reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad edit must not lose the rest
            outcome.status = OutcomeStatus.FAILED
            outcome.reason = f"unexpected failure applying this edit: {exc}"
        else:
            outcome.status = (
                OutcomeStatus.WOULD_APPLY if settings.dry_run else OutcomeStatus.APPLIED
            )
            outcome.change_note = note
            if not settings.dry_run:
                result.applied += 1
        result.outcomes.append(outcome)

    if settings.dry_run or result.applied == 0:
        return

    destination = _output_path(source, settings)
    if destination.resolve() == source.resolve():
        message = (
            "refusing to write over the source specification; choose a "
            "different --output-dir or --output-suffix"
        )
        result.errors.append(message)
        for outcome in result.outcomes:
            if outcome.status is OutcomeStatus.APPLIED:
                outcome.status = OutcomeStatus.FAILED
                outcome.reason = message
        result.applied = 0
        return

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination)
    except Exception as exc:  # noqa: BLE001 - report, never half-claim success
        message = f"could not write {destination}: {exc}"
        result.errors.append(message)
        for outcome in result.outcomes:
            if outcome.status is OutcomeStatus.APPLIED:
                outcome.status = OutcomeStatus.FAILED
                outcome.reason = message
        result.applied = 0
        return

    result.output_path = str(destination)
    log(f"{source.name}: {result.applied} edit(s) -> {destination.name}")
