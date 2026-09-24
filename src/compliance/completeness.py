"""Compliance coverage completeness (plan WP-09).

A successful compliance response is not evidence that every controlling
requirement was assessed. The model can leave rows out, a chunk can fail or
be too large to send, and a specification whose review failed never reaches
the pass at all. This module turns what the requests actually returned into
the two things every output path shares:

- one coverage row per expected requirement, each saying who decided its
  status (``origin``) and how much of the package that status rests on
  (``assessment``); and
- a :class:`CoverageCompleteness` record for the whole pass: the expected
  set, what came back, what did not, which specifications no request
  assessed, and whether the coverage is complete.

It is kept apart from the execution status (``ReviewResult.cross_check_status``
— ``completed`` / ``failed`` / ``skipped``): a request that completed can
still have assessed only part of the package, and an operationally failed
request stays failed.

Pure and stdlib-only: it works on plain coverage-row dicts and specification
names, so the compliance checker, its chunked merge, the program merge, and
the tests share one implementation.

Vocabulary
----------
expected set
    The ids of the controlling requirements: grounded ``spec_requirement``
    research items with a non-empty id, de-duplicated in profile order.
    Unverified (ungrounded) items stay advisory and process advisories are
    not specification content, so neither is ever expected.
assessment unit
    One request's view of the package: its specifications and, when the
    request completed, the rows it returned. A single pass is one unit; a
    chunked pass has one unit per planned chunk, including chunks that
    failed or could not be sent.
row ``origin``
    ``"model"`` when the status is the model's own classification (merged
    across units by precedence); ``"synthetic"`` when no unit established
    one, so this module set it: either no unit returned a row for the id
    (not assessed), or every unit that returned one said ``missing`` while
    part of the package was not assessed (absence not established). A
    synthetic row's status is always ``unclear`` — an omission never becomes
    ``represented``, ``missing``, or a completed assessment — and it carries
    a ``reason``.
row ``assessment``
    ``"full"`` — every unit returned a row and no specification went
    unassessed; ``"partial"`` — some unit returned a row, but another
    completed unit did not, or some specification was never assessed;
    ``"none"`` — no unit returned a row.

Status precedence across units is the D-7 rule, unchanged: ``contradicted``
> ``represented`` > ``unclear`` > ``missing``. A definite signal from any
unit wins — one unit that finds the requirement is enough to say the package
has it — but ``missing`` is a claim about the whole package, so it stands
only when every unit returned ``missing`` and nothing went unassessed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence

ORIGIN_MODEL = "model"
ORIGIN_SYNTHETIC = "synthetic"

ASSESSMENT_FULL = "full"
ASSESSMENT_PARTIAL = "partial"
ASSESSMENT_NONE = "none"

STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_NO_APPLICABLE_ITEMS = "no_applicable_items"
STATE_UNKNOWN = "unknown"

# D-7 merge precedence: a definite signal from any unit beats a weaker one.
# An unrecognized status ranks with ``unclear`` (the honest default).
STATUS_PRECEDENCE: dict[str, int] = {
    "contradicted": 0,
    "represented": 1,
    "unclear": 2,
    "missing": 3,
}


def _rank(status: object) -> int:
    return STATUS_PRECEDENCE.get(str(status or ""), STATUS_PRECEDENCE["unclear"])


@dataclass(frozen=True)
class AssessmentUnit:
    """One request's view of the package.

    ``rows`` are the request's own coverage rows, already normalized against
    the expected set (see ``compliance_checker._normalize_coverage``); they
    count only when ``completed`` is true. ``label`` names the unit in reasons
    (a chunk label; empty for a single pass).
    """

    label: str
    filenames: tuple[str, ...]
    completed: bool
    rows: tuple[dict, ...] = ()
    ignored_rows: int = 0


@dataclass(frozen=True)
class CoverageCompleteness:
    """How much of the expected coverage a compliance pass actually assessed.

    ``expected_ids`` is ``None`` when the expected set could not be
    determined at all (no requirements profile — a recovery without saved
    state); that is never read as "no applicable items". An empty tuple is a
    known empty set: the profile has no controlling requirements, which is a
    valid, complete result with nothing to assess.

    ``returned_ids`` / ``omitted_ids`` partition the expected set: an id is
    returned when at least one completed request returned a row for it, and
    omitted when none did. ``partially_assessed_ids`` are returned ids that
    another *completed* request left out. ``unassessed_specs`` are the
    specifications no completed request assessed: those in failed, skipped,
    or not-analyzed chunks, and those excluded before the pass ran.
    ``held_addition_count`` is the number of ADD findings held as report-only
    because the absence they depend on was not established.
    """

    expected_ids: tuple[str, ...] | None
    returned_ids: tuple[str, ...] = ()
    omitted_ids: tuple[str, ...] = ()
    partially_assessed_ids: tuple[str, ...] = ()
    unassessed_specs: tuple[str, ...] = ()
    ignored_row_count: int = 0
    held_addition_count: int = 0
    reason: str = ""

    @property
    def expected_count(self) -> int | None:
        return None if self.expected_ids is None else len(self.expected_ids)

    @property
    def returned_count(self) -> int:
        return len(self.returned_ids)

    @property
    def omitted_count(self) -> int:
        return len(self.omitted_ids)

    @property
    def state(self) -> str:
        """``complete`` / ``incomplete`` / ``no_applicable_items`` / ``unknown``."""
        if self.expected_ids is None:
            return STATE_UNKNOWN
        if not self.expected_ids:
            return STATE_NO_APPLICABLE_ITEMS
        if self.omitted_ids or self.partially_assessed_ids or self.unassessed_specs:
            return STATE_INCOMPLETE
        return STATE_COMPLETE

    @property
    def complete(self) -> bool:
        """True only when every expected requirement was assessed everywhere.

        A known-empty expected set is complete (nothing to assess); an
        unknown one is not.
        """
        return self.state in (STATE_COMPLETE, STATE_NO_APPLICABLE_ITEMS)

    def to_dict(self) -> dict:
        """JSON-friendly form for the sidecar, profile export, and HTML payload."""
        return {
            "state": self.state,
            "complete": self.complete,
            "expected_count": self.expected_count,
            "returned_count": self.returned_count,
            "omitted_count": self.omitted_count,
            "expected_ids": None if self.expected_ids is None else list(self.expected_ids),
            "omitted_ids": list(self.omitted_ids),
            "partially_assessed_ids": list(self.partially_assessed_ids),
            "unassessed_specs": list(self.unassessed_specs),
            "ignored_row_count": self.ignored_row_count,
            "held_addition_count": self.held_addition_count,
            "reason": self.reason,
        }


def expected_ids_from(ids: Iterable[str]) -> tuple[str, ...]:
    """Unique, non-empty ids in first-seen order (the expected-set shape)."""
    return tuple(dict.fromkeys(str(i) for i in ids if str(i or "").strip()))


def nothing_assessed(
    expected_ids: Sequence[str] | None,
    *,
    unassessed_specs: Iterable[str] = (),
    reason: str = "",
) -> CoverageCompleteness:
    """Completeness of a pass that assessed nothing (skipped or failed)."""
    expected = None if expected_ids is None else expected_ids_from(expected_ids)
    return CoverageCompleteness(
        expected_ids=expected,
        omitted_ids=expected or (),
        unassessed_specs=tuple(dict.fromkeys(s for s in unassessed_specs if s)),
        reason=reason,
    )


def _location(row: dict) -> tuple[str, str | None, str | None]:
    return (str(row.get("status") or ""), row.get("evidence"), row.get("fileName"))


def _join(names: Sequence[str]) -> str:
    return ", ".join(names)


def _no_row_reason(units: Sequence[AssessmentUnit], unassessed: Sequence[str]) -> str:
    head = (
        "The compliance model returned no coverage row for this requirement"
        if len(units) == 1
        else "No chunk returned a coverage row for this requirement"
    )
    if unassessed:
        head += (
            ", and these specifications were not assessed at all: "
            f"{_join(unassessed)}"
        )
    return head + ". It was not assessed, so its coverage is unknown."


def _unit_name(unit: AssessmentUnit) -> str:
    return unit.label or "the specifications that were assessed"


def _absence_not_established_reason(
    missing_units: Sequence[AssessmentUnit],
    silent_units: Sequence[AssessmentUnit],
    unassessed: Sequence[str],
) -> str:
    clauses = [
        "Reported missing in "
        + "; ".join(dict.fromkeys(_unit_name(u) for u in missing_units))
    ]
    if silent_units:
        clauses.append(
            "no coverage row was returned for it by "
            + "; ".join(dict.fromkeys(_unit_name(u) for u in silent_units))
        )
    if unassessed:
        clauses.append(f"these specifications were not assessed: {_join(unassessed)}")
    return (
        ", but ".join([clauses[0], " and ".join(clauses[1:])])
        + ". Absence from the whole package is not established."
    )


def reconcile(
    units: Sequence[AssessmentUnit],
    *,
    expected_ids: Iterable[str],
    excluded_specs: Iterable[str] = (),
) -> tuple[list[dict], CoverageCompleteness]:
    """Merge the units' rows into one row per expected id, plus completeness.

    Rows come back in expected-set order, whatever order the model used.
    When no unit completed there is nothing to merge: the rows are empty (the
    execution status already says the pass did not run) and the completeness
    lists every expected id as omitted. Otherwise every expected id gets
    exactly one row:

    - no completed unit returned it → a synthetic ``unclear`` row
      (assessment ``none``) with the reason;
    - the precedence winner is ``missing`` but part of the package was not
      assessed (another completed unit left the id out, or some
      specification was never assessed) → a synthetic ``unclear`` row
      (assessment ``partial``): absence is not established;
    - otherwise the model's winning row, with ``origin="model"`` and its
      assessment.

    ``also_reported`` keeps the location of every other returned row that
    carries one (evidence or a file name) and differs from the winner, so a
    ``contradicted`` row still says where another unit found the requirement
    represented. It is always present, possibly empty.
    """
    expected = expected_ids_from(expected_ids)
    unassessed = tuple(
        dict.fromkeys(
            [name for unit in units if not unit.completed for name in unit.filenames]
            + [name for name in excluded_specs if name]
        )
    )
    completed = [unit for unit in units if unit.completed]
    ignored = sum(int(unit.ignored_rows or 0) for unit in units)
    if not completed:
        return [], CoverageCompleteness(
            expected_ids=expected,
            omitted_ids=expected,
            unassessed_specs=unassessed,
            ignored_row_count=ignored,
        )

    returned: list[str] = []
    omitted: list[str] = []
    partial: list[str] = []
    rows: list[dict] = []
    for rid in expected:
        pairs = [
            (index, unit, row)
            for index, unit in enumerate(completed)
            for row in unit.rows
            if row.get("requirement_id") == rid
        ]
        if not pairs:
            omitted.append(rid)
            rows.append({
                "requirement_id": rid,
                "status": "unclear",
                "evidence": None,
                "fileName": None,
                "origin": ORIGIN_SYNTHETIC,
                "assessment": ASSESSMENT_NONE,
                "reason": _no_row_reason(units, unassessed),
                "also_reported": [],
            })
            continue
        returned.append(rid)
        returning = {index for index, _unit, _row in pairs}
        silent = [unit for index, unit in enumerate(completed) if index not in returning]
        if silent:
            partial.append(rid)
        is_partial = bool(silent) or bool(unassessed)
        # ``min`` returns the first of equal minima: at the best rank, the
        # first unit (then its first row) wins, so the merge is order-stable.
        _w_index, _w_unit, winner = min(
            pairs, key=lambda pair: _rank(pair[2].get("status"))
        )
        also: list[dict] = []
        seen = {_location(winner)}
        for _index, _unit, row in pairs:
            location = _location(row)
            if row is winner or location in seen:
                continue
            if not (row.get("evidence") or row.get("fileName")):
                continue
            seen.add(location)
            also.append({
                "status": location[0],
                "evidence": location[1],
                "fileName": location[2],
            })
        if winner.get("status") == "missing" and is_partial:
            rows.append({
                "requirement_id": rid,
                "status": "unclear",
                "evidence": None,
                "fileName": None,
                "origin": ORIGIN_SYNTHETIC,
                "assessment": ASSESSMENT_PARTIAL,
                "reason": _absence_not_established_reason(
                    [unit for _index, unit, _row in pairs], silent, unassessed
                ),
                "also_reported": also,
            })
            continue
        rows.append({
            "requirement_id": rid,
            "status": str(winner.get("status") or "unclear"),
            "evidence": winner.get("evidence"),
            "fileName": winner.get("fileName"),
            "origin": ORIGIN_MODEL,
            "assessment": ASSESSMENT_PARTIAL if is_partial else ASSESSMENT_FULL,
            "reason": winner.get("reason"),
            "also_reported": also,
        })
    return rows, CoverageCompleteness(
        expected_ids=expected,
        returned_ids=tuple(returned),
        omitted_ids=tuple(omitted),
        partially_assessed_ids=tuple(partial),
        unassessed_specs=unassessed,
        ignored_row_count=ignored,
    )


def combine(
    labeled: Iterable[tuple[str, CoverageCompleteness | None]],
) -> CoverageCompleteness | None:
    """Program-level view over per-module completeness records.

    Counts and id lists concatenate in module order; unassessed specification
    names are prefixed with their module label, since a specification routed
    to two modules can be assessed in one and not the other. A module whose
    record is missing, or whose expected set is unknown, makes the combined
    expected set unknown — missing metadata is never read as complete.
    Returns ``None`` when there is nothing to combine.
    """
    parts = list(labeled)
    if not parts:
        return None
    expected: list[str] | None = []
    returned: list[str] = []
    omitted: list[str] = []
    partial: list[str] = []
    unassessed: list[str] = []
    ignored = held = 0
    reasons: list[str] = []
    for label, part in parts:
        if part is None:
            expected = None
            reasons.append(f"{label}: coverage completeness was not recorded")
            continue
        if part.expected_ids is None:
            expected = None
        elif expected is not None:
            expected.extend(part.expected_ids)
        returned.extend(part.returned_ids)
        omitted.extend(part.omitted_ids)
        partial.extend(part.partially_assessed_ids)
        unassessed.extend(f"{label}: {name}" for name in part.unassessed_specs)
        ignored += part.ignored_row_count
        held += part.held_addition_count
        if part.reason:
            reasons.append(f"{label}: {part.reason}")
    return CoverageCompleteness(
        expected_ids=None if expected is None else tuple(expected),
        returned_ids=tuple(returned),
        omitted_ids=tuple(omitted),
        partially_assessed_ids=tuple(partial),
        unassessed_specs=tuple(unassessed),
        ignored_row_count=ignored,
        held_addition_count=held,
        reason="; ".join(reasons),
    )


def with_held_additions(
    completeness: CoverageCompleteness, held: int
) -> CoverageCompleteness:
    """``completeness`` with its held-addition count set."""
    return replace(completeness, held_addition_count=int(held))


def gap_phrases(
    *,
    expected: int,
    not_assessed: int,
    partially_assessed: int,
    unassessed_specs: Sequence[str],
    held_additions: int,
) -> list[str]:
    """Plain phrases naming what an incomplete pass did not assess.

    Takes counts rather than a record so the program banner, which sums
    several modules, and the run log, which reads one record, word the gaps
    identically. Empty when there is nothing to report.
    """

    def were(count: int) -> str:
        return "was" if count == 1 else "were"

    phrases: list[str] = []
    if not_assessed:
        phrases.append(
            f"{not_assessed} of {expected} controlling requirement"
            f"{'' if expected == 1 else 's'} {were(not_assessed)} not assessed "
            "(no coverage row was returned)"
        )
    if partially_assessed:
        phrases.append(
            f"{partially_assessed} {were(partially_assessed)} not assessed by "
            "every chunk"
        )
    if unassessed_specs:
        count = len(unassessed_specs)
        phrases.append(
            f"{count} specification{'' if count == 1 else 's'} {were(count)} not "
            f"assessed at all: {_join(list(unassessed_specs))}"
        )
    if held_additions:
        phrases.append(
            f"{held_additions} addition{'' if held_additions == 1 else 's'} that "
            f"depended on a requirement's absence {'is' if held_additions == 1 else 'are'} "
            "shown as report-only"
        )
    return phrases


def describe_gaps(completeness: CoverageCompleteness) -> list[str]:
    """:func:`gap_phrases` for one record."""
    return gap_phrases(
        expected=completeness.expected_count or 0,
        not_assessed=completeness.omitted_count,
        partially_assessed=len(completeness.partially_assessed_ids),
        unassessed_specs=completeness.unassessed_specs,
        held_additions=completeness.held_addition_count,
    )
