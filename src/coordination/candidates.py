"""Which pairs of facts become coordination candidates (plan EX-06).

Stdlib-only and deterministic; no API call. Given the facts read from every
specification (``facts.py``) and a record of which specifications the
cross-check pass sent in one request, this module pairs facts that *look like*
two statements of one requirement with values that cannot both hold, keeps
only pairs no cross-check request ever saw together, and ranks and bounds
them. What it selects is what the observation pass would send to a model;
what it leaves out over a limit is recorded, never dropped silently.

**Which pairs are new.** A pair of specifications is *co-analyzed* when a
planned cross-check request contained both — whether that request completed
or failed, since a failed request is the cross-check pass's own reported gap.
Everything else was never compared:

- a pair in one module that the chunk planner put in different chunks
  (``cross_chunk``) — the within-discipline limitation of a chunked
  cross-check (CLAUDE.md, "Cross-check chunking");
- a pair routed to different modules of a program (``cross_module``) — no
  pass compares them today.

A module whose cross-check result did not record its plan contributes no
``cross_chunk`` pair (the reader cannot tell which pairs it saw) and is
listed as unassessed.

**A join needs all of:** the same category and attribute (a churn pressure is
never compared with a rated one), the same subject (see ``facts.py`` for how
narrowly a subject is identified), scopes that can be the same (a stated
Phase 1 and Phase 2, two buildings, or an existing item and a new one are
never joined), and values that cannot both hold (``facts.values_conflict``).
A pair that agrees, is scope-separated, or is not comparable is counted in
the statistics, so the controls the rules held apart are visible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Sequence

from .facts import (
    CATEGORIES,
    CATEGORY_RESPONSIBILITY,
    POLICY_VERSION,
    CoordinationFact,
    scope_conflict,
    scope_one_sided,
    subject_display,
    values_conflict,
)

KIND_CROSS_CHUNK = "cross_chunk"
KIND_CROSS_MODULE = "cross_module"

SCOPE_MODULE = "module"
SCOPE_PROGRAM = "program"

#: Default bounds. The whole pass sends at most ``MAX_CANDIDATES``
#: candidates, and one pair of specifications contributes at most
#: ``MAX_PER_FILE_PAIR``, so one noisy pair of documents cannot use the
#: whole budget.
MAX_CANDIDATES = 40
MAX_PER_FILE_PAIR = 4

DEFER_FILE_PAIR = "over the limit of candidates for one pair of specifications"
DEFER_TOTAL = "over the pass's candidate limit"


@dataclass(frozen=True)
class SpecUnit:
    """One module's view of which specifications shared a cross-check request.

    ``files`` are the specifications the module reviewed (a file whose review
    failed in this module is left out, as cross-check leaves it out).
    ``chunk_groups`` are the planned cross-check requests' file sets, or
    ``None`` when the module's cross-check result did not record them.
    """

    module_id: str
    files: tuple[str, ...]
    chunk_groups: tuple[frozenset[str], ...] | None
    display_name: str = ""


@dataclass(frozen=True)
class Candidate:
    """Two facts that may be one requirement stated two incompatible ways."""

    candidate_id: str
    kind: str
    category: str
    attribute: str
    subject: str
    side_a: CoordinationFact
    side_b: CoordinationFact
    modules_a: tuple[str, ...]
    modules_b: tuple[str, ...]
    uncertainty: tuple[str, ...] = ()

    @property
    def file_pair(self) -> tuple[str, str]:
        return (self.side_a.file_name, self.side_b.file_name)

    @property
    def priority(self) -> tuple:
        tags = 0 if (self.side_a.subject_is_tag and self.side_b.subject_is_tag) else 1
        category = CATEGORIES.index(self.category) if self.category in CATEGORIES else len(CATEGORIES)
        return (category, tags, len(self.uncertainty), self.candidate_id)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "category": self.category,
            "attribute": self.attribute,
            "subject": self.subject,
            "subject_display": subject_display(self.subject),
            "uncertainty": list(self.uncertainty),
            "sides": [
                _side_dict(self.side_a, self.modules_a),
                _side_dict(self.side_b, self.modules_b),
            ],
        }


def _side_dict(fact: CoordinationFact, modules: Sequence[str], *, passage_chars: int = 400) -> dict:
    start, end = fact.span
    passage = fact.passage
    if len(passage) > passage_chars:
        half = max(0, (passage_chars - (end - start)) // 2)
        lo = max(0, start - half)
        passage = passage[lo:lo + passage_chars]
    return {
        "file_name": fact.file_name,
        "element_id": fact.element_id,
        "modules": list(modules),
        "heading": fact.heading,
        "subject_text": fact.subject_text,
        "raw_value": fact.raw_value,
        "scope": [list(item) for item in fact.scope],
        "passage": passage,
        "fact_id": fact.fact_id,
    }


@dataclass
class CandidateSelection:
    """The candidates to assess, those over a limit, and what was counted."""

    selected: list[Candidate] = field(default_factory=list)
    deferred: list[tuple[Candidate, str]] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    unassessed: list[str] = field(default_factory=list)

    @property
    def joined(self) -> int:
        return len(self.selected) + len(self.deferred)


def candidate_id(
    category: str, attribute: str, subject: str, a: CoordinationFact, b: CoordinationFact
) -> str:
    """Content-derived and order-independent: ``co-`` + 12 hex."""
    sides = sorted(
        ((a.file_name, a.element_id, a.raw_value), (b.file_name, b.element_id, b.raw_value))
    )
    digest = hashlib.sha256(repr((category, attribute, subject, tuple(sides))).encode("utf-8"))
    return "co-" + digest.hexdigest()[:12]


def _file_modules(units: Sequence[SpecUnit]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for unit in units:
        for name in unit.files:
            modules = out.setdefault(name, [])
            if unit.module_id not in modules:
                modules.append(unit.module_id)
    return {name: tuple(modules) for name, modules in out.items()}


def pair_kind(
    a: str, b: str, units: Sequence[SpecUnit], *, scope: str
) -> tuple[str, str]:
    """``(kind, reason)`` for a pair of files: a kind when the pair was never
    compared and is in ``scope``, else ``("", why not)``."""
    shared = [unit for unit in units if a in unit.files and b in unit.files]
    for unit in units:
        for group in unit.chunk_groups or ():
            if a in group and b in group:
                return "", "co-analyzed by cross-check"
    if shared:
        if any(unit.chunk_groups is None for unit in shared):
            return "", "cross-check plan not recorded"
        return KIND_CROSS_CHUNK, ""
    if scope == SCOPE_PROGRAM:
        return KIND_CROSS_MODULE, ""
    return "", "different modules (module scope)"


def select_candidates(
    facts: Iterable[CoordinationFact],
    units: Sequence[SpecUnit],
    *,
    scope: str = SCOPE_MODULE,
    max_candidates: int = MAX_CANDIDATES,
    max_per_file_pair: int = MAX_PER_FILE_PAIR,
) -> CandidateSelection:
    """Join facts into candidates, rank them, and apply the bounds.

    Deterministic for the same facts and units. Each file's facts are read
    once; the modules a side belongs to ride along for provenance.
    """
    selection = CandidateSelection()
    modules_of = _file_modules(units)
    stats: dict = {
        "policy_version": POLICY_VERSION,
        "scope": scope,
        "facts": 0,
        "facts_by_category": {},
        "fact_pairs_compared": 0,
        "co_analyzed_pairs_skipped": 0,
        "out_of_scope_pairs_skipped": 0,
        "unrecorded_plan_pairs_skipped": 0,
        "scope_separated": 0,
        "agreeing": 0,
        "joined": 0,
        "joined_by_kind": {},
        "joined_by_category": {},
    }
    for unit in units:
        if unit.chunk_groups is None:
            selection.unassessed.append(
                f"{unit.display_name or unit.module_id}: the cross-check result did not "
                "record which specifications shared a request, so no within-module "
                "pair was compared"
            )

    groups: dict[tuple[str, str, str], list[CoordinationFact]] = {}
    seen_facts: set[str] = set()
    for fact in facts:
        if fact.fact_id in seen_facts or fact.file_name not in modules_of:
            continue
        seen_facts.add(fact.fact_id)
        stats["facts"] += 1
        stats["facts_by_category"][fact.category] = (
            stats["facts_by_category"].get(fact.category, 0) + 1
        )
        groups.setdefault((fact.category, fact.attribute, fact.subject), []).append(fact)

    pair_cache: dict[tuple[str, str], tuple[str, str]] = {}
    joined: dict[tuple, Candidate] = {}
    for (category, attribute, subject), members in sorted(groups.items()):
        members = sorted(members, key=lambda f: (f.file_name, f.element_id, f.fact_id))
        for a, b in combinations(members, 2):
            if a.file_name == b.file_name:
                continue
            key = tuple(sorted((a.file_name, b.file_name)))
            if key not in pair_cache:
                pair_cache[key] = pair_kind(key[0], key[1], units, scope=scope)
            kind, why = pair_cache[key]
            if not kind:
                if why == "co-analyzed by cross-check":
                    stats["co_analyzed_pairs_skipped"] += 1
                elif why == "cross-check plan not recorded":
                    stats["unrecorded_plan_pairs_skipped"] += 1
                else:
                    stats["out_of_scope_pairs_skipped"] += 1
                continue
            stats["fact_pairs_compared"] += 1
            if scope_conflict(a.scope, b.scope):
                stats["scope_separated"] += 1
                continue
            if not values_conflict(a, b):
                stats["agreeing"] += 1
                continue
            first, second = sorted((a, b), key=lambda f: (f.file_name, f.element_id, f.fact_id))
            notes = list(first.uncertainty) + list(second.uncertainty)
            if scope_one_sided(first.scope, second.scope):
                notes.append("one side states a phase, building, or data hall the other does not")
            dedup_key = (
                category, attribute, subject,
                first.file_name, first.element_id, second.file_name, second.element_id,
            )
            candidate = Candidate(
                candidate_id=candidate_id(category, attribute, subject, first, second),
                kind=kind,
                category=category,
                attribute=attribute,
                subject=subject,
                side_a=first,
                side_b=second,
                modules_a=modules_of.get(first.file_name, ()),
                modules_b=modules_of.get(second.file_name, ()),
                uncertainty=tuple(dict.fromkeys(notes)),
            )
            existing = joined.get(dedup_key)
            if existing is None or candidate.priority < existing.priority:
                joined[dedup_key] = candidate

    ranked = sorted(joined.values(), key=lambda c: c.priority)
    per_pair: dict[tuple[str, str], int] = {}
    for candidate in ranked:
        stats["joined"] += 1
        stats["joined_by_kind"][candidate.kind] = stats["joined_by_kind"].get(candidate.kind, 0) + 1
        stats["joined_by_category"][candidate.category] = (
            stats["joined_by_category"].get(candidate.category, 0) + 1
        )
        pair = candidate.file_pair
        if per_pair.get(pair, 0) >= max_per_file_pair:
            selection.deferred.append((candidate, DEFER_FILE_PAIR))
            continue
        if len(selection.selected) >= max_candidates:
            selection.deferred.append((candidate, DEFER_TOTAL))
            continue
        per_pair[pair] = per_pair.get(pair, 0) + 1
        selection.selected.append(candidate)
    selection.stats = stats
    return selection


def chunk_groups_from(cross_check_result) -> tuple[frozenset[str], ...] | None:
    """The planned cross-check requests' file sets from a cross-check result.

    ``None`` when there is no result or it recorded no plan
    (``ReviewResult.chunk_plan`` is ``None``); an empty tuple when the pass
    recorded that it planned nothing.
    """
    if cross_check_result is None:
        return None
    plan = getattr(cross_check_result, "chunk_plan", None)
    if plan is None:
        return None
    groups: list[frozenset[str]] = []
    for entry in plan:
        files = entry.get("files") if isinstance(entry, Mapping) else None
        if files:
            groups.append(frozenset(str(name) for name in files))
    return tuple(groups)


def responsibility_note(candidate: Candidate) -> str:
    """For a responsibility candidate, the two parties in words."""
    if candidate.category != CATEGORY_RESPONSIBILITY:
        return ""
    parties = [
        ", ".join(str(v) for v, _q in side.values)
        for side in (candidate.side_a, candidate.side_b)
    ]
    return f"{candidate.attribute}: {parties[0]} / {parties[1]}"


__all__ = [
    "Candidate",
    "CandidateSelection",
    "DEFER_FILE_PAIR",
    "DEFER_TOTAL",
    "KIND_CROSS_CHUNK",
    "KIND_CROSS_MODULE",
    "MAX_CANDIDATES",
    "MAX_PER_FILE_PAIR",
    "SCOPE_MODULE",
    "SCOPE_PROGRAM",
    "SpecUnit",
    "candidate_id",
    "chunk_groups_from",
    "pair_kind",
    "responsibility_note",
    "select_candidates",
]
