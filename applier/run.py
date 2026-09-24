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

Before any of that, every document is **bound and given a destination**, for
all files at once and before the first write:

- A sidecar names a document by file name, so a name that matches two
  *different* supplied files is ``FILE_AMBIGUOUS``: it used to bind whichever
  came first, so reversing the inputs edited the other project's copy. The
  same file supplied twice (or by two spellings of one resolved path) is one
  input, not an ambiguity. Names match case-insensitively, so a sidecar that
  spells one name two ways is ambiguous too.
- A destination that would overwrite **any** supplied specification, not only
  its own source, or the edited copy of another document, is
  ``DESTINATION_CONFLICT``.

Either refusal holds every instruction for that document, with the reason, and
leaves the other documents actionable. Neither is ever settled by input order,
and ``--assist`` never sees a held document: it chooses among elements inside
one bound document, never among files. A dry run makes the same decisions.
"""
from __future__ import annotations

import os
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


@dataclass(frozen=True)
class _SuppliedInput:
    """One supplied specification: the spelling to show, and where it is."""

    path: Path
    identity: str


def _path_identity(path: Path) -> str:
    """Where ``path`` really is, compared under the platform's case rules.

    ``Path.resolve`` follows symlinks and ``..``; ``os.path.normcase`` folds
    case where the platform's filesystems do (Windows). Works for a path that
    does not exist yet, which is what every destination is.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(path))
    return os.path.normcase(str(resolved))


def _collision_key(path: Path) -> str:
    """Identity for "would these two writes land on one file?".

    Case-folded even where ``normcase`` is not (macOS, case-sensitive
    Linux): when the answer is unsure, the safe direction is to refuse.
    """
    return _path_identity(path).casefold()


def _is_same_input(a: _SuppliedInput, b: _SuppliedInput) -> bool:
    if a.identity == b.identity:
        return True
    # A case-insensitive volume whose case rules ``normcase`` does not know
    # (macOS): the same path up to case, and the filesystem confirms one file.
    # A zero inode means the filesystem cannot say — two different files
    # would then compare equal — so it never counts as confirmation.
    if a.identity.casefold() != b.identity.casefold():
        return False
    try:
        first, second = os.stat(a.path), os.stat(b.path)
    except OSError:
        return False
    return first.st_ino != 0 and (first.st_dev, first.st_ino) == (
        second.st_dev,
        second.st_ino,
    )


def _index_specs(spec_paths: Iterable[Path]) -> dict[str, tuple[Path, ...]]:
    """Map each case-folded file name to every *different* supplied file.

    One entry per distinct input, not per spelling: the same file supplied
    twice is one input. Two entries under one name are an ambiguity the
    caller must refuse, never resolve by position — the order of each tuple
    is fixed by where the files are, so reversing the inputs changes nothing.
    """
    buckets: dict[str, list[_SuppliedInput]] = {}
    for raw in spec_paths:
        path = Path(raw)
        candidate = _SuppliedInput(path=path, identity=_path_identity(path))
        bucket = buckets.setdefault(path.name.casefold(), [])
        for position, known in enumerate(bucket):
            if _is_same_input(known, candidate):
                # One file, several spellings: keep the lexically smallest
                # spelling, not the first to arrive, so the receipt is
                # order-free too.
                if str(candidate.path) < str(known.path):
                    bucket[position] = candidate
                break
        else:
            bucket.append(candidate)
    return {
        name: tuple(item.path for item in sorted(bucket, key=lambda item: item.identity))
        for name, bucket in buckets.items()
    }


def _output_path(source: Path, settings: RunSettings) -> Path:
    directory = settings.output_dir or source.parent
    return Path(directory) / f"{source.stem}{settings.output_suffix}{source.suffix}"


def _names_a_path(file_name: str) -> bool:
    return "/" in file_name or "\\" in file_name


@dataclass
class _FilePlan:
    """Everything decided about one document before any document is written."""

    file_name: str
    spellings: list[str] = field(default_factory=list)
    entries: list[EditEntry] = field(default_factory=list)
    malformed: list[Outcome] = field(default_factory=list)
    source: Path | None = None
    destination: Path | None = None
    candidates: tuple[Path, ...] = ()
    #: ``(status, error for the file, reason for each entry)`` when held.
    hold: tuple[OutcomeStatus, str, str] | None = None


def _plan_files(
    sidecar, supplied: list[Path], settings: RunSettings
) -> list[_FilePlan]:
    """Bind every named document and choose every destination, up front.

    Nothing is opened or written here. A document whose binding or
    destination is unsafe is held whole; the rest stay actionable.
    """
    index = _index_specs(supplied)
    plans: dict[str, _FilePlan] = {}

    def plan_for(file_name: str) -> _FilePlan:
        plan = plans.setdefault(file_name.casefold(), _FilePlan(file_name=file_name))
        if file_name not in plan.spellings:
            plan.spellings.append(file_name)
        return plan

    for entry in sidecar.entries:
        plan_for(entry.file_name).entries.append(entry)
    # Malformed entries never reach a document, but they must still be
    # accounted for — file by file, so the receipt attributes them.
    for entry, problem in sidecar.malformed:
        plan_for(entry.file_name).malformed.append(
            Outcome(
                entry=entry,
                status=OutcomeStatus.MALFORMED,
                reason=f"this edit instruction is unusable: {problem}",
            )
        )

    for key, plan in plans.items():
        name = plan.file_name
        candidates = index.get(key, ())
        if len(plan.spellings) > 1:
            spelled = " and ".join(repr(s) for s in plan.spellings)
            reason = (
                f"the sidecar names {spelled}; file names are matched "
                "case-insensitively, so this applier cannot tell whether they "
                "are one specification or two, and edits neither"
            )
            plan.hold = (OutcomeStatus.FILE_AMBIGUOUS, reason, reason)
        elif name and _names_a_path(name):
            plan.hold = (
                OutcomeStatus.FILE_MISSING,
                f"{name} is a path, not a file name",
                "the sidecar names a path, not a file name; the applier binds "
                "only to the specifications supplied to it, by file name",
            )
        elif not candidates:
            plan.hold = (
                OutcomeStatus.FILE_MISSING,
                f"{name} was not among the specifications supplied",
                "the specification this instruction targets was not supplied "
                "to the applier",
            )
        elif len(candidates) > 1:
            plan.candidates = candidates
            listed = ", ".join(str(path) for path in candidates)
            plan.hold = (
                OutcomeStatus.FILE_AMBIGUOUS,
                f"{name} matches {len(candidates)} different supplied files "
                f"({listed}); refusing to guess which one this sidecar was "
                "written for",
                f"{len(candidates)} different supplied specifications are named "
                f"{name} ({listed}); supply only the one this sidecar was "
                "written for",
            )
        else:
            (plan.source,) = candidates
            plan.destination = _output_path(plan.source, settings)

    _hold_unsafe_destinations(
        [plan for plan in plans.values() if plan.hold is None], supplied
    )
    return list(plans.values())


def _hold_unsafe_destinations(bound: list[_FilePlan], supplied: list[Path]) -> None:
    """Refuse a destination that would overwrite a supplied file or a sibling.

    Every supplied specification is protected, not only the document's own
    source: with ``--output-suffix .v2``, ``x.docx``'s copy is ``x.v2.docx``,
    and if that was supplied too, writing it would destroy an input — or, if
    it is processed later, edit this run's output instead of the reviewed
    document.
    """
    protected = {_collision_key(path): path for path in supplied}
    by_destination: dict[str, list[_FilePlan]] = {}
    for plan in bound:
        by_destination.setdefault(_collision_key(plan.destination), []).append(plan)

    advice = "choose a different --output-dir or --output-suffix"
    for plan in bound:
        key = _collision_key(plan.destination)
        if key == _collision_key(plan.source):
            reason = f"refusing to write over the source specification; {advice}"
        elif key in protected:
            reason = (
                f"refusing to write the edited copy to {plan.destination}: that "
                f"would overwrite {protected[key]}, which was supplied as a "
                f"specification. Move or rename it, or {advice}"
            )
        elif len(by_destination[key]) > 1:
            others = ", ".join(
                str(other.source) for other in by_destination[key] if other is not plan
            )
            reason = (
                f"refusing to write the edited copy to {plan.destination}: the "
                f"edited copy of {others} would be written there too, and one "
                f"would overwrite the other; {advice}"
            )
        else:
            continue
        plan.hold = (OutcomeStatus.DESTINATION_CONFLICT, reason, reason)


def apply_sidecar(
    sidecar,
    spec_paths: Iterable[Path],
    settings: RunSettings,
    *,
    client=None,
    log: Callable[[str], None] = lambda _message: None,
) -> list[FileResult]:
    """Apply a loaded sidecar to the supplied specifications."""
    supplied = [Path(p) for p in spec_paths]
    plans = _plan_files(sidecar, supplied, settings)
    protected = frozenset(_collision_key(path) for path in supplied)

    results: list[FileResult] = []
    for plan in plans:
        result = FileResult(
            file_name=plan.file_name,
            candidate_paths=[str(path) for path in plan.candidates],
        )
        result.outcomes.extend(plan.malformed)
        if plan.hold is not None:
            status, file_error, entry_reason = plan.hold
            result.errors.append(file_error)
            for entry in plan.entries:
                result.outcomes.append(
                    Outcome(entry=entry, status=status, reason=entry_reason)
                )
            results.append(result)
            continue

        result.source_path = str(plan.source)
        _apply_to_file(
            plan.source,
            plan.entries,
            result,
            settings,
            destination=plan.destination,
            protected=protected,
            client=client,
            log=log,
        )
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
    destination: Path,
    protected: frozenset[str],
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

    # ``_plan_files`` already refused every unsafe destination before any
    # document was opened. This re-check is the last line of defense before
    # the one irreversible step, so a future caller that skips planning still
    # cannot write over a supplied specification.
    destination_key = _collision_key(destination)
    if destination_key in protected or destination_key == _collision_key(source):
        message = (
            "refusing to write over a supplied specification; choose a "
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
