"""Read and validate a ``<report-stem>.edits.json`` sidecar.

Accepts both shapes ``src/output/edit_sidecar.py`` emits: schema 4 (a
single-module ``PipelineResult``) and schema 5 (a routed-program result,
whose entries additionally carry ``module_id``).

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
    INSERT_AFTER,
    INSERT_BEFORE,
    SUPPORTED_ACTIONS,
    EditEntry,
)

#: Schema versions this build understands, mirrored from
#: ``edit_sidecar.SIDECAR_SCHEMA_VERSION`` / ``PROGRAM_SIDECAR_SCHEMA_VERSION``.
SUPPORTED_SCHEMA_VERSIONS = frozenset({4, 5})
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
        return self.schema_version == PROGRAM_SCHEMA_VERSION

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


def _entry_problem(entry: EditEntry) -> str | None:
    """Why this entry cannot be executed, or ``None`` when it is usable.

    Deliberately restates the action-shape rules rather than importing
    ``reviewer.validate_edit_shape``: that function operates on ``Finding``
    objects inside the review parser, and an applier reading a file written
    by a *different* build must validate the serialized shape it actually
    received.
    """
    if not entry.file_name:
        return "entry names no file"
    if entry.action_type not in SUPPORTED_ACTIONS:
        return f"unsupported action_type {entry.action_type!r}"
    if entry.action_type == ACTION_ADD:
        if not entry.replacement_text:
            return "ADD without replacement_text"
        if not entry.anchor_text and not entry.target_element_id:
            return "ADD without anchor_text or target_element_id"
        if entry.insert_position not in (INSERT_BEFORE, INSERT_AFTER):
            return f"ADD with invalid insert_position {entry.insert_position!r}"
        return None
    if not entry.existing_text:
        return f"{entry.action_type} without existing_text"
    if entry.action_type == "EDIT" and not entry.replacement_text:
        return "EDIT without replacement_text"
    return None


def _build_entry(raw: dict[str, Any]) -> EditEntry:
    proposal = raw.get("edit_proposal") or {}
    if not isinstance(proposal, dict):
        proposal = {}
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
        has_per_file_original=bool(raw.get("has_per_file_original", True)),
        affected_files=tuple(
            str(name) for name in (raw.get("affected_files") or []) if name
        ),
        module_id=_clean(raw.get("module_id")),
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
    for raw in raw_edits:
        if not isinstance(raw, dict):
            loaded.malformed.append(
                (_build_entry({}), "edit entry is not a JSON object")
            )
            continue
        entry = _build_entry(raw)
        problem = _entry_problem(entry)
        if problem is not None:
            loaded.malformed.append((entry, problem))
            continue
        loaded.entries.append(entry)
    return loaded
