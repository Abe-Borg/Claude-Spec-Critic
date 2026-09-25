"""Read and validate a ``<report-stem>.edits.json`` sidecar.

Accepts four shapes. Schemas 4 and 5 are what ``src/output/edit_sidecar.py``
has emitted: 4 for a single-module ``PipelineResult``, 5 for a routed-program
result, whose entries additionally carry ``module_id``. Both list **one entry
per affected file**, so the same fix needed at two places in one file reached
them once (plan WP-06B); they are read exactly as before, and nothing about
the lost second location is pretended back.

Schemas 6 and 7 are their occurrence-aware successors (single module and
routed program), which keep every location. This reader lands before the
writer does (plan chunk S12), so their contract is defined here:

* **Top level:** the same keys as 4 (for 6) and 5 (for 7).
* **Entries:** one per *occurrence* — one file, one place, one instruction —
  with every schema 4 / 5 entry key except ``has_per_file_original``, plus:

  - ``occurrence_id`` (required): the content-derived id
    ``pipeline.compute_occurrence_id`` mints (``oc-`` and 12 hex characters),
    from the module, the finding id, the file, the element, and the
    instruction. Several entries of one finding share its ``finding_id`` and
    differ here.
  - ``location_basis`` (required): ``validated`` or ``claimed`` when the entry
    names an element (``evidenceElementId`` or the proposal's
    ``target_element_id``, required then); ``unresolved`` or
    ``missing_original`` when it names none (and must carry none, so a
    locator can never borrow another place's element). ``missing_original``
    means no original was recorded for the file: an EDIT or DELETE is located
    by its text alone, and an ADD has no anchor and cannot be placed.
  - ``module_id`` (required in 7, optional in 6).

* **Unique key:** ``(module_id, occurrence_id)`` (:attr:`EditEntry.key`). A
  sidecar that lists one key twice has broken its own contract, so every
  copy is refused as malformed rather than one of them chosen by position —
  counting copies that are malformed anyway, so an executable copy beside a
  broken one is refused too.

The applier never relies on the key to decide what to write: instructions
that disagree about one place in a document are found from where they
resolve (``applier.conflicts``), in every schema.

An unknown ``schema_version`` raises :class:`SidecarSchemaError` rather than
being read on a best-effort basis. That mirrors
``governing_context.basis_from_dict``, and for the same reason: reinterpreting
a payload under rules it was not written against changes the meaning of a
record that a paid run produced. The failure mode here is worse than a wrong
cost figure — it is an edit applied to a legal document under a contract this
build does not actually understand.

Entries that are individually unusable (no proposal, an unknown action, a
missing required field) are **kept** and marked malformed rather than
dropped: the receipt must account for every entry the sidecar listed, or a
reviewer cannot tell "the applier considered and rejected this" from "the
applier never saw it".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    ACTION_ADD,
    ELEMENT_LOCATION_BASES,
    INSERT_AFTER,
    INSERT_BEFORE,
    LOCATION_BASES,
    LOCATION_MISSING_ORIGINAL,
    SUPPORTED_ACTIONS,
    EditEntry,
)

#: The per-file schemas, mirrored from ``edit_sidecar.SIDECAR_SCHEMA_VERSION``
#: / ``PROGRAM_SIDECAR_SCHEMA_VERSION``.
LEGACY_SCHEMA_VERSIONS = frozenset({4, 5})
#: Their occurrence-aware successors (see the module docstring).
OCCURRENCE_SCHEMA_VERSIONS = frozenset({6, 7})
#: Schema versions this build understands.
SUPPORTED_SCHEMA_VERSIONS = LEGACY_SCHEMA_VERSIONS | OCCURRENCE_SCHEMA_VERSIONS
#: The routed-program schemas: the per-file one and the occurrence-aware one.
PROGRAM_SCHEMA_VERSIONS = frozenset({5, 7})
#: The per-file program schema, as the writer names it today.
PROGRAM_SCHEMA_VERSION = 5


class SidecarError(Exception):
    """The sidecar could not be read at all."""


class SidecarSchemaError(SidecarError):
    """The sidecar's schema version is not one this build understands."""


class LoadedSidecar:
    """A parsed sidecar plus the run context an applier may want to show."""

    def __init__(self, payload: dict[str, Any], *, path: Path) -> None:
        self.path = path
        self.payload = payload
        self.schema_version: int = int(payload.get("schema_version", 0))
        self.generated_at: str | None = payload.get("generated_at")
        self.report_file: str | None = payload.get("report_file")
        self.program_id: str | None = payload.get("program_id")
        self.cycle_label: str | None = payload.get("cycle_label")
        self.project: dict[str, Any] | None = payload.get("project")
        # Present only on a routed-program sidecar, and only when the run's
        # coverage figures were normalized from inconsistent saved state.
        # The sidecar documents these as a signal to treat
        # ``submission_coverage`` as unverified; the applier surfaces them
        # so a reviewer is not told a partial run was a complete one.
        self.integrity_warnings: list[str] = [
            str(w) for w in (payload.get("integrity_warnings") or [])
        ]
        self.entries: list[EditEntry] = []
        #: ``(entry, why)`` for every listed edit this build cannot execute.
        #: Kept rather than dropped so the receipt accounts for every entry
        #: the sidecar listed — "considered and rejected" must be
        #: distinguishable from "never seen".
        self.malformed: list[tuple[EditEntry, str]] = []

    @property
    def is_program(self) -> bool:
        """A routed-program sidecar: schema 5, or its successor 7."""
        return self.schema_version in PROGRAM_SCHEMA_VERSIONS

    @property
    def is_occurrence_aware(self) -> bool:
        """Schemas 6 and 7: one entry per occurrence, keyed by occurrence id."""
        return self.schema_version in OCCURRENCE_SCHEMA_VERSIONS

    @property
    def file_names(self) -> list[str]:
        """Distinct spec file names referenced, in first-seen order."""
        return list(dict.fromkeys(e.file_name for e in self.entries if e.file_name))


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _occurrence_problem(entry: EditEntry, *, program: bool) -> str | None:
    """Why a schema 6 / 7 entry breaks the occurrence contract, or ``None``."""
    if not entry.occurrence_id:
        return "entry carries no occurrence_id"
    if entry.location_basis not in LOCATION_BASES:
        return f"unknown location_basis {entry.location_basis!r}"
    named = entry.target_element_id or entry.evidence_element_id
    if entry.location_basis in ELEMENT_LOCATION_BASES and not named:
        return f"location_basis {entry.location_basis!r} but the entry names no element"
    if entry.location_basis not in ELEMENT_LOCATION_BASES and named:
        return (
            f"location_basis {entry.location_basis!r} but the entry names element "
            f"{named}; an entry without a location of its own may not carry one"
        )
    if program and not entry.module_id:
        return "program entry names no module_id"
    return None


def _entry_problem(entry: EditEntry, *, version: int) -> str | None:
    """Why this entry cannot be executed, or ``None`` when it is usable.

    Deliberately restates the action-shape rules rather than importing
    ``reviewer.validate_edit_shape``: that function operates on ``Finding``
    objects inside the review parser, and an applier reading a file written
    by a *different* build must validate the serialized shape it actually
    received.
    """
    if not entry.file_name:
        return "entry names no file"
    if version in OCCURRENCE_SCHEMA_VERSIONS:
        problem = _occurrence_problem(entry, program=version in PROGRAM_SCHEMA_VERSIONS)
        if problem is not None:
            return problem
    if entry.action_type not in SUPPORTED_ACTIONS:
        return f"unsupported action_type {entry.action_type!r}"
    if entry.action_type == ACTION_ADD:
        if not entry.replacement_text:
            return "ADD without replacement_text"
        if not entry.anchor_text and not entry.target_element_id:
            if entry.location_basis == LOCATION_MISSING_ORIGINAL:
                return (
                    "ADD with no place in this file: its finding recorded no "
                    "original here, and an addition cannot be positioned by "
                    "another file's anchor"
                )
            return "ADD without anchor_text or target_element_id"
        if entry.insert_position not in (INSERT_BEFORE, INSERT_AFTER):
            return f"ADD with invalid insert_position {entry.insert_position!r}"
        return None
    if not entry.existing_text:
        return f"{entry.action_type} without existing_text"
    if entry.action_type == "EDIT" and not entry.replacement_text:
        return "EDIT without replacement_text"
    return None


def _build_entry(raw: dict[str, Any], *, occurrence_aware: bool = False) -> EditEntry:
    proposal = raw.get("edit_proposal") or {}
    if not isinstance(proposal, dict):
        proposal = {}
    if occurrence_aware:
        basis = str(raw.get("location_basis") or "").strip().lower() or None
        occurrence_fields = {
            "occurrence_id": _clean(raw.get("occurrence_id")),
            "location_basis": basis,
            # Superseded by the basis: only a missing original lacks a
            # location of its own, and such an entry names no element.
            "has_per_file_original": basis != LOCATION_MISSING_ORIGINAL,
        }
    else:
        occurrence_fields = {
            "has_per_file_original": bool(raw.get("has_per_file_original", True)),
        }
    return EditEntry(
        finding_id=str(raw.get("finding_id") or ""),
        file_name=str(raw.get("fileName") or ""),
        action_type=str(proposal.get("action_type") or "").strip().upper(),
        existing_text=_clean(proposal.get("existing_text")),
        replacement_text=_clean(proposal.get("replacement_text")),
        anchor_text=_clean(proposal.get("anchor_text")),
        insert_position=(
            str(proposal.get("insert_position")).strip().lower()
            if proposal.get("insert_position")
            else None
        ),
        target_element_id=_clean(proposal.get("target_element_id")),
        evidence_element_id=_clean(raw.get("evidenceElementId")),
        edit_confidence=_coerce_confidence(proposal.get("edit_confidence")),
        report_status=str(raw.get("report_status") or ""),
        verification_verdict=_clean(raw.get("verification_verdict")),
        severity=str(raw.get("severity") or ""),
        section=str(raw.get("section") or ""),
        issue=str(raw.get("issue") or ""),
        code_reference=_clean(raw.get("codeReference")),
        affected_files=tuple(
            str(name) for name in (raw.get("affected_files") or []) if name
        ),
        module_id=_clean(raw.get("module_id")),
        **occurrence_fields,
    )


def load_sidecar(path: Path | str) -> LoadedSidecar:
    """Load and validate a sidecar file.

    Raises :class:`SidecarError` when the file is unreadable or structurally
    wrong, and :class:`SidecarSchemaError` for a schema version this build
    does not understand.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SidecarError(f"Could not read sidecar {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"Sidecar {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SidecarError(f"Sidecar {path} is not a JSON object.")

    version = payload.get("schema_version")
    if not isinstance(version, int) or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SidecarSchemaError(
            f"Sidecar {path} declares schema_version {version!r}; this applier "
            f"understands {sorted(SUPPORTED_SCHEMA_VERSIONS)}. Refusing to "
            "reinterpret it — upgrade the applier rather than applying edits "
            "under a contract it does not implement."
        )

    raw_edits = payload.get("edits")
    if not isinstance(raw_edits, list):
        raise SidecarError(f"Sidecar {path} has no 'edits' list.")

    loaded = LoadedSidecar(payload, path=path)
    occurrence_aware = loaded.is_occurrence_aware
    for raw in raw_edits:
        if not isinstance(raw, dict):
            loaded.malformed.append(
                (
                    _build_entry({}, occurrence_aware=occurrence_aware),
                    "edit entry is not a JSON object",
                )
            )
            continue
        entry = _build_entry(raw, occurrence_aware=occurrence_aware)
        problem = _entry_problem(entry, version=version)
        if problem is not None:
            loaded.malformed.append((entry, problem))
            continue
        loaded.entries.append(entry)
    if occurrence_aware:
        _refuse_repeated_keys(loaded)
    return loaded


def _stated_key(entry: EditEntry, *, program: bool) -> tuple[str, str] | None:
    """The unique key an occurrence-aware entry states, or ``None`` when it
    states no complete one: no occurrence id, or no module in a program."""
    if not entry.occurrence_id or (program and not entry.module_id):
        return None
    return entry.key


def _refuse_repeated_keys(loaded: LoadedSidecar) -> None:
    """Move every entry whose unique key repeats to ``malformed``.

    Every copy, not all but one: which copy survived would be decided by
    where the sidecar listed it, and a file that breaks its own unique-entry
    contract does not say which copy it meant. A copy already refused for a
    defect of its own still counts when it states the key: beside it, an
    executable copy is still a key listed twice, and applying that one would
    let the file's corruption choose the instruction.
    """
    program = loaded.is_program
    counts: dict[tuple[str, str], int] = {}
    listed = [*loaded.entries, *(entry for entry, _ in loaded.malformed)]
    for entry in listed:
        key = _stated_key(entry, program=program)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    kept: list[EditEntry] = []
    for entry in loaded.entries:
        times = counts[entry.key]
        if times == 1:
            kept.append(entry)
            continue
        module, occurrence = entry.key
        where = f" in module {module}" if module else ""
        loaded.malformed.append(
            (
                entry,
                f"occurrence {occurrence}{where} is listed {times} times; each "
                "occurrence must appear once, so none of these copies is applied",
            )
        )
    loaded.entries = kept
