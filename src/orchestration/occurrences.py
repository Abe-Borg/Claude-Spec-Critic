"""Display groups and executable occurrences of findings (plan WP-06B).

The report shows a finding once — "this issue, found in these files" — and
the edit sidecar lists every place its edit applies. This module is the one
model of both: :func:`group_findings` turns deduplicated findings into
display groups that keep each location, and :func:`edit_occurrences` lists a
run's executable places. It imports only the standard library, so the report
exporters and the sidecar writer use it without importing the pipeline; the
pipeline re-exports its names.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # annotations only: the model reads findings duck-typed
    from ..input.extractor import ExtractedSpec
    from ..review.reviewer import Finding


def _normalized_text_digest(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    # Hash the full text so long passages can never collide just because
    # their first 200 characters happen to match. Truncating before hashing
    # silently merged distinct findings.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Display groups vs executable occurrences (plan WP-06B).
#
# A *display group* is one finding as the report shows it: "this issue, found
# in these files". An *executable occurrence* is one place an edit applies:
# this file, this element, this instruction. One group may hold several
# occurrences — in several files, and at several locations within one file
# (the same fix needed at p4 and at p8). ``group_findings`` builds both from
# the deduplicated finding list and ``edit_occurrences`` lists a run's
# executable ones; the list-of-Finding API is unchanged, so every other
# caller keeps working.
# ---------------------------------------------------------------------------

#: How an occurrence's location was established. The location is the element
#: the review named (the proposal's ``target_element_id``, else the finding's
#: ``evidenceElementId``), confirmed where the reviewed text is available.
#:
#: ``validated``: the element exists in that file's extracted text and
#: contains the edit's locator text (``existingText``, or an ADD's
#: ``anchorText``); a report-only finding needs only the element.
#:
#: ``claimed``: the review named an element and no extracted text was
#: available to check it (a recovery whose source files moved). The id is
#: used as given; an applier confirms it against the document itself.
#:
#: ``unresolved``: no usable element was named: none, or one that failed
#: validation. The text has to be found in the document, and identical prose
#: does not prove an identical location, so every such member of one file is
#: one uncertain occurrence, never several.
#:
#: ``missing_original``: no pre-merge original was recorded for this file (a
#: legacy or hand-built multi-file finding, or a finding that names no file).
#: Only the group's shared instruction text is known; no other location's
#: element or anchor is borrowed for it.
LOCATION_VALIDATED = "validated"
LOCATION_CLAIMED = "claimed"
LOCATION_UNRESOLVED = "unresolved"
LOCATION_MISSING_ORIGINAL = "missing_original"
LOCATION_BASES = (
    LOCATION_VALIDATED,
    LOCATION_CLAIMED,
    LOCATION_UNRESOLVED,
    LOCATION_MISSING_ORIGINAL,
)
#: The bases whose occurrence names an element.
ELEMENT_LOCATIONS = frozenset({LOCATION_VALIDATED, LOCATION_CLAIMED})

#: Prefix of every occurrence id, beside the ``rf-`` / ``cf-`` / ``lc-``
#: finding ids it never collides with.
OCCURRENCE_ID_PREFIX = "oc"

#: ``{file name: {element id: element text}}`` for the files a run reviewed.
ElementIndex = dict[str, dict[str, str]]


def element_index_from_specs(specs: Iterable[ExtractedSpec] | None) -> ElementIndex:
    """The element text of every reviewed file, for validating element ids.

    Built from each spec's ``paragraph_map``: the elements the review model
    was shown, under the ids it could cite. A spec without a map contributes
    nothing, so its findings' element ids stay ``claimed``. A file name that
    two specs share is left out too: an id cannot be checked against a file
    the name does not single out.
    """
    index: ElementIndex = {}
    shared: set[str] = set()
    for spec in specs or ():
        name = getattr(spec, "filename", "") or ""
        mappings = getattr(spec, "paragraph_map", None)
        if not name or not mappings:
            continue
        if name in index:
            shared.add(name)
            continue
        elements: dict[str, str] = {}
        for mapping in mappings:
            element_id = getattr(mapping, "element_id", "") or ""
            if element_id and not element_id.startswith("meta:"):
                elements.setdefault(element_id, getattr(mapping, "text", "") or "")
        index[name] = elements
    for name in shared:
        del index[name]
    return index


def _contains_text(haystack: str, needle: str) -> bool:
    """Whether ``needle`` occurs in ``haystack``, exactly or with whitespace
    runs collapsed: the test the applier confirms an element id with, and the
    one anchor validation applies to a whole file. Never case- or
    wording-tolerant."""
    if not needle:
        return False
    if needle in haystack:
        return True
    return " ".join(needle.split()) in " ".join(haystack.split())


def _named_element(finding: Finding, proposal) -> str | None:
    """The element a finding names: its proposal's target, else its evidence."""
    named = getattr(proposal, "target_element_id", None) if proposal is not None else None
    named = (named or finding.evidenceElementId or "").strip()
    return named or None


def _locator_text(proposal) -> str | None:
    """The text that must appear in an edit's element: what an EDIT or DELETE
    consumes, or an ADD's anchor. A report-only finding has none."""
    if proposal is None:
        return None
    if proposal.action_type == "ADD":
        return proposal.anchor_text
    return proposal.existing_text


def _member_location(
    member: Finding,
    proposal,
    file_name: str,
    element_index: ElementIndex | None,
) -> tuple[str | None, str, str]:
    """``(element id, location basis, note)`` for one pre-merge member."""
    named = _named_element(member, proposal)
    if named is None:
        return None, LOCATION_UNRESOLVED, "the review named no element for this edit"
    if element_index is None or file_name not in element_index:
        return named, LOCATION_CLAIMED, ""
    text = element_index[file_name].get(named)
    if text is None:
        return (
            None,
            LOCATION_UNRESOLVED,
            f"the review named {named}, which is not an element of {file_name}",
        )
    needle = _locator_text(proposal)
    if needle and not _contains_text(text, needle):
        return (
            None,
            LOCATION_UNRESOLVED,
            f"the review named {named}, which does not contain the text this edit targets",
        )
    return named, LOCATION_VALIDATED, ""


def _instruction_key(proposal) -> tuple:
    """What an occurrence does, normalized the way finding identity is.

    Within one group the action and the edit text already agree (they are in
    the dedup key), so this separates what can still differ: an ADD's side
    and its anchor. The anchor stays in even where an element is named,
    because an element can hold more than one paragraph — a table row
    resolves to a paragraph per cell, and the anchor chooses which one the
    new paragraph goes beside — so leaving it out merged two places into one.
    Two anchors inside one paragraph are then two occurrences of one place,
    which the applier, seeing the document, writes once (``DUPLICATE``).
    """
    if proposal is None:
        return ("REPORT_ONLY",)
    addition = proposal.action_type == "ADD"
    key = (
        proposal.action_type,
        _normalized_text_digest(proposal.existing_text),
        _normalized_text_digest(proposal.replacement_text),
        (proposal.insert_position or "") if addition else "",
    )
    if addition:
        key += (_normalized_text_digest(proposal.anchor_text),)
    return key


def _natural_key(text: str) -> tuple:
    """Sort ``p4`` before ``p10`` and ``t0r2`` before ``t0r10``."""
    return tuple(
        (0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk)
        for chunk in re.split(r"(\d+)", text or "")
        if chunk
    )


def _member_sort_key(member: Finding) -> tuple:
    """A content-only order for the members of one occurrence, so which member
    stands for it never depends on the order findings arrived in."""
    return (
        member.existingText or "",
        member.replacementText or "",
        member.anchorText or "",
        member.insertPosition or "",
        member.evidenceElementId or "",
        member.issue or "",
        member.section or "",
        member.severity or "",
        -float(member.confidence or 0.0),
    )


_LOCATION_RANK = {basis: rank for rank, basis in enumerate(LOCATION_BASES)}


@dataclass(frozen=True)
class FindingOccurrence:
    """One executable place a finding applies: a file, an element, an instruction.

    ``finding`` is the group's representative: display and verification
    fields (issue, severity, the ``VerificationResult``) come from it, because
    verification runs after dedup on the representative alone.
    ``original_finding`` is this location's own pre-merge member: the source
    of every executable field. When several members collapsed into this
    occurrence (a duplicate emission of the same edit at the same place) they
    are all in ``members``, and ``original_finding`` is the first of them in a
    content-only order. ``original_finding`` is ``None`` only for a
    ``missing_original`` occurrence, and then nothing is borrowed for it.

    ``occurrence_id`` is content-derived — module, finding id, file, element,
    and instruction — so it does not depend on input order or on any
    presentation counter, and two modules' occurrences never share one.
    """

    occurrence_id: str
    file_name: str
    finding: Finding
    original_finding: Finding | None = None
    #: The element this occurrence targets; ``None`` unless the location is
    #: ``validated`` or ``claimed``.
    element_id: str | None = None
    location: str = LOCATION_UNRESOLVED
    #: Why the location is uncertain, when it is (for example the element the
    #: review named, and why it could not be used).
    location_note: str = ""
    module_id: str | None = None
    #: Every pre-merge member of this file that targets this place.
    members: tuple[Finding, ...] = ()
    #: Other findings that reported this same occurrence: content-identical
    #: findings with the same id (two identical coordination findings, say).
    #: Set by :func:`edit_occurrences`; each carries its own verification,
    #: so a writer can keep the least trusted of them.
    also_reported_by: tuple[Finding, ...] = ()

    @property
    def is_located(self) -> bool:
        """Whether this occurrence names an element."""
        return self.location in ELEMENT_LOCATIONS

    def executable_finding(self) -> Finding:
        """The per-location original when there is one, else the representative.

        The fallback is for display; a ``missing_original`` occurrence
        borrows the representative's text here, locator included. Code that
        emits an instruction uses :meth:`executable_proposal`, which never
        borrows a locator.
        """
        return self.original_finding if self.original_finding is not None else self.finding

    def has_original(self) -> bool:
        """True iff a pre-merge original was recorded for this location."""
        return self.original_finding is not None

    def executable_proposal(self):
        """This occurrence's own edit instruction, or ``None`` when it has none.

        ``None`` when the representative carries no proposal (the report shows
        a report-only finding, so nothing is emitted) or when this location's
        own original carries none. The target element is this occurrence's
        ``element_id``: an element the review named but that failed
        validation is dropped rather than passed on. A ``missing_original``
        occurrence gets the group's shared instruction text with no element
        and no anchor, because both would come from another location.
        """
        representative = self.finding.as_edit_proposal()
        if representative is None:
            return None
        if self.original_finding is None:
            return replace(representative, target_element_id=None, anchor_text=None)
        own = self.original_finding.as_edit_proposal()
        if own is None:
            return None
        return replace(own, target_element_id=self.element_id)


@dataclass(frozen=True)
class FindingGroup:
    """One finding as the report shows it, with every place it applies.

    ``occurrences`` holds one :class:`FindingOccurrence` per file and target,
    in a stable order: the group's files in ``affected_files`` order, and
    within a file its located occurrences by element, then the uncertain one.
    """

    group_id: str
    representative: Finding
    occurrences: list[FindingOccurrence] = field(default_factory=list)
    module_id: str | None = None

    @property
    def file_names(self) -> list[str]:
        """The group's files, once each, in occurrence order."""
        return list(dict.fromkeys(o.file_name for o in self.occurrences))


def compute_occurrence_id(
    module_id: str | None, finding_id: str, file_name: str, target: tuple
) -> str:
    """The content-derived id of one occurrence (see :class:`FindingOccurrence`).

    ``target`` is ``("element", element id, instruction key)`` for a located
    occurrence, ``("unresolved", instruction key)`` for an uncertain one, and
    ``("missing_original",)`` for a file with no recorded original.
    """
    key = (module_id or "", finding_id, file_name, target)
    digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
    return f"{OCCURRENCE_ID_PREFIX}-{digest[:12]}"


def _members_by_file(finding: Finding) -> dict[str, list[Finding]]:
    """Each file's pre-merge members. A singleton is its own original."""
    if finding.occurrence_originals:
        members: dict[str, list[Finding]] = {}
        for original in finding.occurrence_originals:
            if original.fileName:
                members.setdefault(original.fileName, []).append(original)
        return members
    return {finding.fileName: [finding]} if finding.fileName else {}


def _file_occurrences(
    finding: Finding,
    file_name: str,
    members: list[Finding],
    *,
    group_identity: str,
    module_id: str | None,
    element_index: ElementIndex | None,
) -> list[FindingOccurrence]:
    """One file's occurrences: one per distinct target, duplicates collapsed."""
    if not members:
        note = (
            f"no original was recorded for {file_name}; only the group's shared "
            "edit text is known"
            if file_name
            else "the finding names no file"
        )
        target = ("missing_original",)
        return [
            FindingOccurrence(
                occurrence_id=compute_occurrence_id(module_id, group_identity, file_name, target),
                file_name=file_name,
                finding=finding,
                location=LOCATION_MISSING_ORIGINAL,
                location_note=note,
                module_id=module_id,
            )
        ]

    buckets: dict[tuple, list[tuple[Finding, str | None, str, str]]] = {}
    for member in members:
        proposal = member.as_edit_proposal()
        element_id, basis, note = _member_location(member, proposal, file_name, element_index)
        if element_id is not None:
            target = ("element", element_id, _instruction_key(proposal))
        else:
            target = ("unresolved", _instruction_key(proposal))
        buckets.setdefault(target, []).append((member, element_id, basis, note))

    occurrences: list[FindingOccurrence] = []
    for target, located in buckets.items():
        ordered = sorted(located, key=lambda item: _member_sort_key(item[0]))
        _, element_id, basis, _ = ordered[0]
        notes = list(dict.fromkeys(note for _, _, _, note in ordered if note))
        occurrences.append(
            FindingOccurrence(
                occurrence_id=compute_occurrence_id(module_id, group_identity, file_name, target),
                file_name=file_name,
                finding=finding,
                original_finding=ordered[0][0],
                element_id=element_id,
                location=basis,
                location_note="; ".join(notes),
                module_id=module_id,
                members=tuple(member for member, _, _, _ in ordered),
            )
        )
    occurrences.sort(
        key=lambda o: (
            _LOCATION_RANK[o.location] if o.location not in ELEMENT_LOCATIONS else 0,
            _natural_key(o.element_id or ""),
            o.occurrence_id,
        )
    )
    return occurrences


def group_findings(
    findings: list[Finding],
    *,
    module_id: str | None = None,
    element_index: ElementIndex | None = None,
) -> list[FindingGroup]:
    """One :class:`FindingGroup` per deduplicated finding, in input order.

    Each group's occurrences keep every location: one per file and validated
    target (element plus instruction), so the same fix at p4 and p8 of one
    file is two occurrences. A duplicate emission of one edit at one place
    collapses into a single occurrence; members that name no usable element
    collapse into one uncertain occurrence per file and instruction, never
    several. A file the group lists without a recorded original gets a
    ``missing_original`` occurrence, and a finding with no file at all gets
    one placeholder occurrence with an empty file name.

    ``element_index`` (see :func:`element_index_from_specs`) validates the
    element ids the review named; without it they are used as ``claimed``.
    ``module_id`` qualifies every occurrence id, so identical findings in two
    modules of a program never share one.

    A group's identity is its finding id as the run stamped it (``rf-`` /
    ``cf-`` / ``lc-``), never one minted here: identity comes only from the
    run's :class:`FindingIdentityContext`. A finding never given an id (a
    hand-built one; every finding a run reports has one) groups under ``""``,
    so its occurrence ids rest on file, place, and instruction alone.
    """
    groups: list[FindingGroup] = []
    for finding in findings:
        files = list(dict.fromkeys(finding.affected_files)) or (
            [finding.fileName] if finding.fileName else [""]
        )
        identity = finding.finding_id or ""
        members_by_file = _members_by_file(finding)
        occurrences: list[FindingOccurrence] = []
        for file_name in files:
            occurrences.extend(
                _file_occurrences(
                    finding,
                    file_name,
                    members_by_file.get(file_name, []),
                    group_identity=identity,
                    module_id=module_id,
                    element_index=element_index,
                )
            )
        groups.append(
            FindingGroup(
                group_id=identity,
                representative=finding,
                occurrences=occurrences,
                module_id=module_id,
            )
        )
    return groups


_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}


def _representative_sort_key(finding: Finding) -> tuple:
    """A content-only order among findings that report one occurrence."""
    return (
        _SEVERITY_RANK.get(finding.severity, 99),
        -float(finding.confidence or 0.0),
        finding.issue or "",
        finding.section or "",
        finding.codeReference or "",
    )


def edit_occurrences(
    findings: list[Finding],
    *,
    module_id: str | None = None,
    element_index: ElementIndex | None = None,
) -> list[FindingOccurrence]:
    """Every occurrence of every finding that proposes an edit, once each.

    The executable view of a run: groups whose representative carries no
    proposal are left out, as the report leaves them report-only. Two
    findings with the same id and content (an identical coordination finding
    returned twice, say) report the same occurrence; it is listed once, under
    the first of them in a content-only order, with the others in
    ``also_reported_by``, since each carries its own verification. An
    occurrence may still have no :meth:`~FindingOccurrence.executable_proposal`
    (a location whose own original was demoted, or an ADD with no recorded
    original); it is listed so a writer can account for it.
    """
    by_id: dict[str, list[FindingOccurrence]] = {}
    for group in group_findings(findings, module_id=module_id, element_index=element_index):
        if group.representative.as_edit_proposal() is None:
            continue
        for occurrence in group.occurrences:
            by_id.setdefault(occurrence.occurrence_id, []).append(occurrence)
    result: list[FindingOccurrence] = []
    for same in by_id.values():
        ordered = sorted(same, key=lambda o: _representative_sort_key(o.finding))
        first = ordered[0]
        if len(ordered) > 1:
            first = replace(first, also_reported_by=tuple(o.finding for o in ordered[1:]))
        result.append(first)
    return result
