"""Domain models for the applier: what came in, what was found, what happened.

Three closed enums carry the whole decision trail, and each outcome records
*why* — a refusal that does not say what it refused on is indistinguishable
from a bug, and the receipt is the only artifact a reviewer sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Action types mirrored from ``src.review.reviewer.EDIT_ACTION_TYPES``. They
# are restated rather than imported so a sidecar written by a future Spec
# Critic version fails validation here loudly instead of being silently
# reinterpreted under this build's notion of the action set.
ACTION_EDIT = "EDIT"
ACTION_ADD = "ADD"
ACTION_DELETE = "DELETE"
SUPPORTED_ACTIONS = frozenset({ACTION_EDIT, ACTION_ADD, ACTION_DELETE})

INSERT_BEFORE = "before"
INSERT_AFTER = "after"

# How a schema 6 / 7 entry's location was established, mirrored from
# ``src.orchestration.pipeline.LOCATION_BASES`` and restated for the same
# reason as the action types: an unknown value fails validation loudly.
LOCATION_VALIDATED = "validated"
LOCATION_CLAIMED = "claimed"
LOCATION_UNRESOLVED = "unresolved"
LOCATION_MISSING_ORIGINAL = "missing_original"
LOCATION_BASES = frozenset(
    {LOCATION_VALIDATED, LOCATION_CLAIMED, LOCATION_UNRESOLVED, LOCATION_MISSING_ORIGINAL}
)
#: The bases whose entry names an element (and must carry its id).
ELEMENT_LOCATION_BASES = frozenset({LOCATION_VALIDATED, LOCATION_CLAIMED})


class LocationStatus(str, Enum):
    """How confidently the applier located an edit's target element."""

    #: The sidecar's element id resolved and its text confirmed the target.
    RESOLVED_BY_ID = "RESOLVED_BY_ID"
    #: No usable id, but the target text occurs exactly once in the document.
    RESOLVED_BY_UNIQUE_TEXT = "RESOLVED_BY_UNIQUE_TEXT"
    #: Several text matches, narrowed to one by the finding's section.
    RESOLVED_BY_SECTION = "RESOLVED_BY_SECTION"
    #: The optional assist tier chose among candidates.
    RESOLVED_BY_ASSIST = "RESOLVED_BY_ASSIST"
    #: Several candidates, none distinguishable. Never guessed.
    AMBIGUOUS = "AMBIGUOUS"
    #: The id resolved but its text no longer matches — the spec has drifted.
    DRIFTED = "DRIFTED"
    #: The target text is nowhere in the document.
    NOT_FOUND = "NOT_FOUND"
    #: The id names an element kind this version cannot write to.
    UNSUPPORTED_ELEMENT = "UNSUPPORTED_ELEMENT"


#: Location statuses that authorize a write. Everything else is reported.
APPLICABLE_LOCATIONS = frozenset(
    {
        LocationStatus.RESOLVED_BY_ID,
        LocationStatus.RESOLVED_BY_UNIQUE_TEXT,
        LocationStatus.RESOLVED_BY_SECTION,
        LocationStatus.RESOLVED_BY_ASSIST,
    }
)


class OutcomeStatus(str, Enum):
    """What the applier did with one sidecar entry."""

    APPLIED = "APPLIED"
    #: Located and applicable, but ``--dry-run`` was in effect.
    WOULD_APPLY = "WOULD_APPLY"
    #: The trust-model policy withheld it (DISPUTED, CONTESTED, ...).
    HELD_BY_POLICY = "HELD_BY_POLICY"
    #: The target could not be located unambiguously.
    UNLOCATED = "UNLOCATED"
    #: The named spec file was not among the documents supplied.
    FILE_MISSING = "FILE_MISSING"
    #: The named spec file matches more than one different supplied document
    #: (or the sidecar spells one name two ways), so no document was chosen.
    #: Never resolved by guessing, input order, or ``--assist``.
    FILE_AMBIGUOUS = "FILE_AMBIGUOUS"
    #: The document's edited copy would overwrite a supplied specification or
    #: another document's edited copy, so the document was not processed.
    DESTINATION_CONFLICT = "DESTINATION_CONFLICT"
    #: The entry itself is unusable (bad action, missing text, ...).
    MALFORMED = "MALFORMED"
    #: Located and authorized, but the write failed.
    FAILED = "FAILED"
    #: This instruction and another would change the same text, or insert
    #: different paragraphs at one place, in different ways. None of them is
    #: applied: whichever came first in the sidecar would otherwise win.
    EDIT_CONFLICT = "EDIT_CONFLICT"
    #: Identical to another instruction for the same place, which was applied
    #: (or would be, in a dry run); the change is written once.
    DUPLICATE = "DUPLICATE"


class ElementKind(str, Enum):
    """The document surface an element id points at."""

    BODY_PARAGRAPH = "body_paragraph"
    TABLE_ROW = "table_row"
    HEADER_FOOTER = "header_footer"
    #: Text read from inside a block content control (or custom XML block):
    #: every id with a ``cc<n>`` step (``cc3p0``, ``t0cc4r1``, ``s0hcc2p0``).
    #: Readable for review, never written: a control can be locked, bound to
    #: document data that Word rewrites it from, or showing placeholder text,
    #: and reviewability does not grant editability (plan WP-02).
    CONTENT_CONTROL = "content_control"
    #: Text boxes, footnotes, endnotes and the synthetic block delimiters.
    #: The extractor surfaces their text so a reviewer can find a
    #: requirement authored there, but python-docx does not model them as
    #: editable containers, so this applier reports rather than writes.
    UNSUPPORTED = "unsupported"


#: The kinds this applier writes to. Every other kind is reported, never
#: written, and a copy of the target text in one of them makes an edit that
#: has no confirming element id ambiguous (see ``locator.locate``).
WRITABLE_KINDS = frozenset(
    {ElementKind.BODY_PARAGRAPH, ElementKind.TABLE_ROW, ElementKind.HEADER_FOOTER}
)


@dataclass(frozen=True)
class EditEntry:
    """One sidecar edit instruction, normalized.

    Mirrors the ``edits[]`` shape of ``edit_sidecar.build_edit_instructions``
    for schema 4 (single module) and schema 5 (routed program), and of their
    occurrence-aware successors, schemas 6 and 7, whose entries also carry an
    ``occurrence_id`` and a ``location_basis`` (see ``applier.sidecar``). The
    unique key is :attr:`key`.
    """

    finding_id: str
    file_name: str
    action_type: str
    existing_text: str | None
    replacement_text: str | None
    anchor_text: str | None
    insert_position: str | None
    target_element_id: str | None
    evidence_element_id: str | None
    edit_confidence: float
    report_status: str
    verification_verdict: str | None
    severity: str
    section: str
    issue: str
    code_reference: str | None
    #: False when the locator fields were borrowed from the merged group's
    #: representative rather than this file's own pre-merge original. The
    #: sidecar documents this as a signal to confirm before applying, so the
    #: locator refuses to resolve such an entry on its element id alone.
    has_per_file_original: bool
    affected_files: tuple[str, ...] = ()
    #: Schemas 5 and 7 (routed program), where every entry names its module;
    #: optional in schema 6.
    module_id: str | None = None
    #: Schemas 6 and 7 only: the content-derived id of the occurrence this
    #: entry applies — one file, one place, one instruction.
    occurrence_id: str | None = None
    #: Schemas 6 and 7 only: how that place was established (``validated``,
    #: ``claimed``, ``unresolved``, ``missing_original``).
    location_basis: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """The entry's unique key within its sidecar.

        Schemas 6 and 7: ``(module_id, occurrence_id)``, with ``""`` for a
        single-module entry that names no module; the reader refuses a
        sidecar entry whose key repeats. Schemas 4 and 5: ``(finding_id,
        file_name)``, as they document it; one file's second location of the
        same fix never reached those sidecars, and two modules' entries can
        share it. Nothing in this program relies on a key to decide what to
        apply: conflicts are found from the places entries resolve to.
        """
        if self.occurrence_id:
            return (self.module_id or "", self.occurrence_id)
        return (self.finding_id, self.file_name)

    @property
    def label(self) -> str:
        """How receipts and reasons name this entry."""
        name = self.finding_id or "(no finding id)"
        if self.module_id:
            name = f"{self.module_id}/{name}"
        if self.occurrence_id:
            return f"{name} [{self.occurrence_id}]"
        return name

    @property
    def sort_key(self) -> tuple:
        """A content-only order for entries: whichever of two identical
        instructions is written, and the order independent edits are applied
        in, never depend on where the sidecar listed them."""
        return (
            self.module_id or "",
            self.finding_id,
            self.occurrence_id or "",
            self.file_name,
            self.action_type,
            self.existing_text or "",
            self.replacement_text or "",
            self.anchor_text or "",
            self.insert_position or "",
            self.target_element_id or "",
            self.evidence_element_id or "",
        )

    @property
    def locator_text(self) -> str | None:
        """The text used to find this edit's target element.

        EDIT / DELETE locate on the text they consume; ADD locates on its
        anchor.
        """
        if self.action_type == ACTION_ADD:
            return self.anchor_text
        return self.existing_text


@dataclass(frozen=True)
class Candidate:
    """One possible target element, drawn from the extracted paragraph map."""

    element_id: str
    text: str
    section_id: str
    element_type: str
    kind: ElementKind


@dataclass(frozen=True)
class Location:
    """The locator's verdict for one entry."""

    status: LocationStatus
    element_id: str | None = None
    kind: ElementKind = ElementKind.UNSUPPORTED
    detail: str = ""
    #: Populated on AMBIGUOUS so the receipt can name what it saw, and fed
    #: to the assist tier when it is enabled.
    candidates: tuple[Candidate, ...] = ()

    @property
    def is_applicable(self) -> bool:
        return self.status in APPLICABLE_LOCATIONS and bool(self.element_id)


@dataclass
class Outcome:
    """What happened to one entry, and why. One per sidecar entry, always."""

    entry: EditEntry
    status: OutcomeStatus
    reason: str = ""
    location: Location | None = None
    #: Present on APPLIED / WOULD_APPLY: a short human-readable note of the
    #: change made, for the text summary.
    change_note: str = ""
    #: The other entries this outcome is about: the instructions an
    #: EDIT_CONFLICT disagrees with, or the one a DUPLICATE was written as.
    #: Their :attr:`EditEntry.label`, in content order.
    related: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        location = self.location
        return {
            "finding_id": self.entry.finding_id,
            "occurrence_id": self.entry.occurrence_id,
            "location_basis": self.entry.location_basis,
            "file_name": self.entry.file_name,
            "module_id": self.entry.module_id,
            "action_type": self.entry.action_type,
            "severity": self.entry.severity,
            "section": self.entry.section,
            "issue": self.entry.issue,
            "report_status": self.entry.report_status,
            "verification_verdict": self.entry.verification_verdict,
            "edit_confidence": self.entry.edit_confidence,
            "outcome": self.status.value,
            "reason": self.reason,
            "change_note": self.change_note,
            "location_status": location.status.value if location else None,
            "element_id": location.element_id if location else None,
            "element_kind": location.kind.value if location else None,
            "location_detail": location.detail if location else "",
            "candidate_element_ids": (
                [c.element_id for c in location.candidates] if location else []
            ),
            "related_entries": list(self.related),
        }


@dataclass
class FileResult:
    """Per-document roll-up: what was written, and what was left alone."""

    file_name: str
    source_path: str = ""
    output_path: str | None = None
    applied: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Every different supplied document the name matched, when it matched
    #: more than one (``FILE_AMBIGUOUS``); empty otherwise.
    candidate_paths: list[str] = field(default_factory=list)
