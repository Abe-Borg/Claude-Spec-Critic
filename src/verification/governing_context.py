"""Immutable governing basis for a verification run.

See CLAUDE.md, "Edition authority" — this module is the snapshot that closes
the inversion described there.

Verification today receives a finding's own fields plus, on a profile-bearing
run, a ``user_location`` dict and a ``jurisdiction_fingerprint`` — nothing more.
``src/verification/`` never reads ``project_context``, so the adoption facts the
research fan-out established reach review, cross-check and compliance but are
invisible to the verifier that judges their findings. Meanwhile the verifier
prompt instructs the model to treat the module's pinned edition as
authoritative, and on the data-center modules every one of those pins is marked
``UNVERIFIED``.

The consequence is an **inversion**: the stage holding the facts is overruled by
the stage without them, so a *correct* adoption-deferring finding can be ground
away as ``DISPUTED``. A false positive gets human review; a silently discarded
true positive does not.

:class:`VerificationBasis` is the bounded, immutable snapshot that closes that
gap. This module is the contract only — construction from a run's research, its
canonical identity, and its rendering. Threading it through the pipeline,
persisting it, and folding its fingerprint into the cache key are separate
work; nothing here reaches the network, the filesystem, or a model.

**Import direction.** ``src/research/`` imports from ``src/verification/``
(``retry_policy``, ``source_grounding``, ``verifier``), so this module must not
import the research runner back. It accepts a dependency-free structural
contract instead: any object exposing the ``ResearchItem`` /
``RequirementsProfile`` attribute names, or the plain dicts they serialize to.
The closed vocabularies come from ``review.structured_schemas``, which is a
constants module ``src/verification/`` already depends on elsewhere.

**Trust rules this module enforces**, each of which exists
because the alternative silently loses meaning:

* Claims and qualifications are preserved **verbatim**. Structure is
  normalized; legal meaning is not.
* ``grounded=True`` with no accepted citation is inconsistent and is recorded
  as **not** grounded. Historical research citations stay historical provenance
  and never enter the verifier's current-conversation retrieval pool.
* Contradictory claims are **both kept**. Resolving them by picking the higher
  model confidence would manufacture certainty the research did not have.
* Process advisories keep their classification and are never promoted into
  controlling requirements.
* Client, owner and insurer requirements are kept under **separate authority
  labels** — a contractual obligation is not a legal adoption, and merging them
  in either direction is an error.
* Selection is deterministic and size-bounded. Whole items are omitted, never
  truncated mid-qualification, and every omission is stated in the rendered
  block and folded into the fingerprint.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..review.structured_schemas import (
    RESEARCH_ACTIONABILITY_VALUES,
    RESEARCH_ITEM_CATEGORIES,
)

#: Bumped when the stored shape changes.
BASIS_SCHEMA_VERSION = 1

#: Bumped when authority, selection or rendering *semantics* change — i.e. when
#: the same inputs would now produce a materially different question for the
#: verifier. Distinct from the schema version: a snapshot written under an
#: unsupported policy must surface as an incompatibility rather than be
#: silently reinterpreted under today's rules.
BASIS_POLICY_VERSION = 1

#: Provenance-only: the module's own pins, with no researched adoption facts.
#: This is what a profile-less data-center run gets, and the point of naming it
#: is that such a run must *disclose* generic provenance rather than inherit an
#: authoritative-cycle assumption it has no basis for.
MODE_PROVENANCE_ONLY = "provenance_only"
#: Researched adoption facts are present and carried.
MODE_RESEARCHED_CONTEXT = "researched_context"

RESEARCH_STATE_AVAILABLE = "available"
RESEARCH_STATE_PARTIAL = "partial"
RESEARCH_STATE_UNAVAILABLE = "unavailable"
#: Recovered from saved state that predates the basis, so the original
#: assumptions cannot be reconstructed. Visibly distinct from "unavailable":
#: research may well have run, we just cannot honestly say what it found.
RESEARCH_STATE_RECOVERED = "recovered_without_snapshot"

_RESEARCH_STATES = frozenset(
    {
        RESEARCH_STATE_AVAILABLE,
        RESEARCH_STATE_PARTIAL,
        RESEARCH_STATE_UNAVAILABLE,
        RESEARCH_STATE_RECOVERED,
    }
)

#: Controlling categories, in the order they are selected and rendered. Plan
#: section 5.4 rule 3: adoption and AHJ facts come first because they answer
#: the applicability question the verifier is actually being asked.
_PRIORITY_CATEGORIES: tuple[str, ...] = (
    "governing_code",
    "local_amendment",
    "referenced_standard",
    "ahj_requirement",
)

#: Kept, but under a separate authority label (rule 4).
_CONTRACTUAL_CATEGORIES: tuple[str, ...] = (
    "client_standard",
    "insurer_requirement",
)

AUTHORITY_ADOPTED_LAW = "adopted_law_or_ahj"
AUTHORITY_CONTRACTUAL = "contractual_or_owner"
AUTHORITY_OTHER = "other"

#: Default size ceiling for the rendered block, in tokens. It
#: rule 8 proposes 4,000 and asks that it be validated against real profiles;
#: it is a parameter here rather than a constant baked into the renderer.
DEFAULT_BASIS_TOKEN_BUDGET = 4_000

#: Chars per token used only for the deterministic pre-render size estimate.
#: Deliberately conservative — over-estimating drops an item early, which is
#: visible in ``omissions``; under-estimating would silently blow the budget.
_CHARS_PER_TOKEN = 3.5

#: Tokens of fixed scaffolding :func:`_render_item` adds per item (the id and
#: topic line, the field labels, the provenance line). Counting only the item's
#: own text would under-estimate every item, which is the one direction the
#: budget must not err in.
_ITEM_RENDER_OVERHEAD_TOKENS = 30


def _unverified_pin_omission(module_basis: "ModuleBasis") -> str:
    """Disclosure that the module's pins are unconfirmed, or ``""``.

    Shared by both construction paths on purpose. A provenance-only run is the
    one where this matters most — the pins are the *only* thing it carries — so
    emitting it solely on the researched path would drop the disclosure exactly
    where nothing else compensates for it.
    """
    unverified = [s.name for s in module_basis.standards if s.unverified and s.edition]
    if not unverified:
        return ""
    return (
        "The module's pinned editions for "
        + ", ".join(unverified)
        + " are marked UNVERIFIED — reference assumptions, not confirmed "
        "adoptions for this project."
    )


def _authority_class(category: str) -> str:
    if category in _PRIORITY_CATEGORIES:
        return AUTHORITY_ADOPTED_LAW
    if category in _CONTRACTUAL_CATEGORIES:
        return AUTHORITY_CONTRACTUAL
    return AUTHORITY_OTHER


def _get(obj: Any, name: str, default: Any) -> Any:
    """Read ``name`` from a dataclass-like object or a plain dict."""
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


# ---------------------------------------------------------------------------
# Basis parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BasisItem:
    """One researched fact, normalized but never reworded.

    ``requirement`` and ``notes`` are carried verbatim from the research item.
    ``grounded`` is *re-derived* rather than trusted: a row claiming grounding
    with no accepted citation is inconsistent, and rendering it as grounded
    would overstate what the run actually established.
    """

    item_id: str
    dimension_id: str
    category: str
    authority_class: str
    topic: str
    requirement: str
    authority: str
    code_reference: str
    notes: str
    grounded: bool
    #: Historical provenance only. These URLs were retrieved by the *research*
    #: pass, not by the verification conversation, and must never be treated as
    #: current-conversation evidence — the grounding invariant is about what
    #: this conversation retrieved.
    historical_sources: tuple[str, ...]
    confidence: float
    actionability: str
    #: Recorded when the row claimed grounding it could not support.
    consistency_note: str = ""

    @property
    def is_advisory(self) -> bool:
        return self.actionability == "process_advisory"


@dataclass(frozen=True)
class StandardPin:
    """One pinned standard edition, with provenance *and* qualifiers preserved.

    Two things must survive the snapshot, and for the same reason:

    * **Provenance.** ``edition_summary_lines`` drops it, which is exactly what
      must not happen here — an ``UNVERIFIED`` pin presented without its marker
      is how a guess becomes an authority.
    * **Applicability qualifiers.** ``StandardEdition.note`` and ``ca_amended``
      are not decoration. ``datacenter_electrical`` pins NFPA 110 with
      ``note="where an EPSS or owner criterion invokes it"``, and the California
      cycle marks amended editions. Rendering a bare ``NFPA 110: 2022`` states
      an unconditional requirement the module never declared, and a verifier
      reading it could dispute a correct finding that says the standard does not
      apply here. Carrying only the base ``edition`` would also leave a qualifier
      change invisible to the fingerprint, so a materially different question
      would reuse an earlier answer.

    ``edition_phrase`` is the module's own rendering (``"2025, as amended by
    California"``, ``"2022 (where an EPSS or owner criterion invokes it)"``) and
    is what the prompt shows; ``note`` / ``ca_amended`` are kept structured so a
    later consumer does not have to parse prose back out.
    """

    name: str
    edition: str
    provenance: str
    unverified: bool
    #: The module's rendered descriptor, qualifiers included.
    edition_phrase: str = ""
    note: str = ""
    ca_amended: bool = False

    @property
    def is_qualified(self) -> bool:
        """True when the pin applies only under a stated condition."""
        return bool(self.note) or self.ca_amended


@dataclass(frozen=True)
class ModuleBasis:
    """The module's own code basis, snapshotted with provenance intact.

    A cycle *label* is not enough to reconstruct this later: the same label
    could be re-pointed at different editions, and a resumed run must render
    the assumptions it was actually built on.
    """

    module_id: str
    cycle_label: str
    base_codes: tuple[tuple[str, str, str], ...]  # (key, name, year)
    asce7: str
    asce7_previous: str
    standards: tuple[StandardPin, ...]

    @property
    def primary_code_year(self) -> str:
        return self.base_codes[0][2] if self.base_codes else ""

    @property
    def has_unverified_pins(self) -> bool:
        return any(s.unverified for s in self.standards)


@dataclass(frozen=True)
class DimensionOutcome:
    dimension_id: str
    status: str
    item_count: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# The basis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationBasis:
    """Immutable snapshot of what a run may treat as governing.

    Built once, before any review spend, and carried unchanged through both
    verification rounds, retries, continuations, escalation, shared-work
    grouping and resume. Nothing downstream may rebuild it from current state:
    a resumed run must ask the question it originally paid for.
    """

    schema_version: int
    policy_version: int
    mode: str
    module_basis: ModuleBasis
    research_state: str
    research_date: str
    #: Read-only after construction — see ``__post_init__``.
    project: Mapping[str, str]
    items: tuple[BasisItem, ...]
    dimension_statuses: tuple[DimensionOutcome, ...]
    #: Human-readable statements of what this basis does NOT contain. Rendered
    #: into the prompt and folded into the fingerprint, because a basis that
    #: silently dropped an item is a different question from one that kept it.
    omissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Seal ``project`` behind a read-only view.

        ``frozen=True`` stops the *attribute* being rebound, not the dict being
        mutated, and ``project`` feeds both the rendered prompt and
        :meth:`fingerprint`. A single ``basis.project["city"] = ...`` after the
        fingerprint had keyed a cache entry or a single-flight group would let a
        request carry different context under an identity earned by the old
        context — the exact reuse the identity exists to prevent. Copying first
        also un-aliases any dict the caller kept a reference to.
        """
        object.__setattr__(
            self,
            "project",
            MappingProxyType({str(k): str(v) for k, v in dict(self.project).items()}),
        )

    # -- derived identity ----------------------------------------------------

    def fingerprint(self) -> str:
        """Canonical identity of the interpretation-relevant snapshot.

        Covers everything that changes the question being asked: module and
        cycle assumptions with their provenance, the selected facts and their
        qualifications, location and client, research date and state, the
        per-dimension outcomes, the omissions, and the policy version.
        Deterministically equal inputs agree; a materially different question
        does not.

        Never supplied by a caller — an externally-provided identity could be
        made to collide with a different basis, which is precisely the reuse
        the cache key exists to prevent.
        """
        payload = {
            "policy_version": self.policy_version,
            "mode": self.mode,
            "module": {
                "module_id": self.module_basis.module_id,
                "cycle_label": self.module_basis.cycle_label,
                "base_codes": [list(bc) for bc in self.module_basis.base_codes],
                "asce7": self.module_basis.asce7,
                "asce7_previous": self.module_basis.asce7_previous,
                "standards": [
                    [
                        s.name,
                        s.edition,
                        s.edition_phrase,
                        s.note,
                        s.ca_amended,
                        s.provenance,
                        s.unverified,
                    ]
                    for s in self.module_basis.standards
                ],
            },
            "research_state": self.research_state,
            "research_date": self.research_date,
            "project": dict(sorted(self.project.items())),
            "items": [
                [
                    i.item_id,
                    i.category,
                    i.authority_class,
                    i.topic,
                    i.requirement,
                    i.authority,
                    i.code_reference,
                    i.notes,
                    i.grounded,
                    i.actionability,
                    i.consistency_note,
                ]
                for i in self.items
            ],
            "dimension_statuses": [
                [d.dimension_id, d.status, d.item_count, d.error]
                for d in self.dimension_statuses
            ],
            "omissions": list(self.omissions),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    # -- convenience ---------------------------------------------------------

    @property
    def controlling_items(self) -> tuple[BasisItem, ...]:
        """Grounded, non-advisory adoption/AHJ facts.

        Deliberately narrow: only these answer "what edition governs here".
        """
        return tuple(
            i
            for i in self.items
            if i.authority_class == AUTHORITY_ADOPTED_LAW
            and i.grounded
            and not i.is_advisory
        )

    @property
    def has_researched_facts(self) -> bool:
        return bool(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "module_basis": {
                "module_id": self.module_basis.module_id,
                "cycle_label": self.module_basis.cycle_label,
                "base_codes": [list(bc) for bc in self.module_basis.base_codes],
                "asce7": self.module_basis.asce7,
                "asce7_previous": self.module_basis.asce7_previous,
                "standards": [
                    {
                        "name": s.name,
                        "edition": s.edition,
                        "edition_phrase": s.edition_phrase,
                        "note": s.note,
                        "ca_amended": s.ca_amended,
                        "provenance": s.provenance,
                        "unverified": s.unverified,
                    }
                    for s in self.module_basis.standards
                ],
            },
            "research_state": self.research_state,
            "research_date": self.research_date,
            "project": dict(self.project),
            "items": [
                {
                    "item_id": i.item_id,
                    "dimension_id": i.dimension_id,
                    "category": i.category,
                    "authority_class": i.authority_class,
                    "topic": i.topic,
                    "requirement": i.requirement,
                    "authority": i.authority,
                    "code_reference": i.code_reference,
                    "notes": i.notes,
                    "grounded": i.grounded,
                    "historical_sources": list(i.historical_sources),
                    "confidence": i.confidence,
                    "actionability": i.actionability,
                    "consistency_note": i.consistency_note,
                }
                for i in self.items
            ],
            "dimension_statuses": [
                {
                    "dimension_id": d.dimension_id,
                    "status": d.status,
                    "item_count": d.item_count,
                    "error": d.error,
                }
                for d in self.dimension_statuses
            ],
            "omissions": list(self.omissions),
            "fingerprint": self.fingerprint(),
        }


class BasisPolicyIncompatible(ValueError):
    """A saved snapshot was written under a policy this build cannot honour.

    Raised rather than reinterpreted: silently re-reading an old snapshot under
    today's authority rules would change the question a paid run asked.
    """


def basis_from_dict(raw: dict[str, Any]) -> VerificationBasis:
    """Rebuild a basis from its stored form.

    A stored ``fingerprint`` is ignored and recomputed — trusting a supplied
    identity would let a tampered or stale record claim equivalence with a
    basis it does not match.
    """
    if int(raw.get("schema_version", 0)) != BASIS_SCHEMA_VERSION:
        raise BasisPolicyIncompatible(
            f"basis schema_version {raw.get('schema_version')!r} is not the "
            f"supported version {BASIS_SCHEMA_VERSION}"
        )
    policy = int(raw.get("policy_version", 0))
    if policy != BASIS_POLICY_VERSION:
        raise BasisPolicyIncompatible(
            f"basis policy_version {policy} was written by a different "
            f"authority policy than this build's ({BASIS_POLICY_VERSION}); "
            "surface the incompatibility rather than reinterpreting it"
        )

    mb = raw.get("module_basis") or {}
    module_basis = ModuleBasis(
        module_id=str(mb.get("module_id", "")),
        cycle_label=str(mb.get("cycle_label", "")),
        base_codes=tuple(tuple(str(x) for x in bc) for bc in mb.get("base_codes", [])),
        asce7=str(mb.get("asce7", "")),
        asce7_previous=str(mb.get("asce7_previous", "")),
        standards=tuple(
            StandardPin(
                name=str(s.get("name", "")),
                edition=str(s.get("edition", "")),
                provenance=str(s.get("provenance", "")),
                unverified=bool(s.get("unverified", False)),
                edition_phrase=str(s.get("edition_phrase", "") or s.get("edition", "")),
                note=str(s.get("note", "")),
                ca_amended=bool(s.get("ca_amended", False)),
            )
            for s in mb.get("standards", [])
        ),
    )
    return VerificationBasis(
        schema_version=BASIS_SCHEMA_VERSION,
        policy_version=BASIS_POLICY_VERSION,
        mode=str(raw.get("mode", MODE_PROVENANCE_ONLY)),
        module_basis=module_basis,
        research_state=str(raw.get("research_state", RESEARCH_STATE_UNAVAILABLE)),
        research_date=str(raw.get("research_date", "")),
        project={str(k): str(v) for k, v in (raw.get("project") or {}).items()},
        items=tuple(
            BasisItem(
                item_id=str(i.get("item_id", "")),
                dimension_id=str(i.get("dimension_id", "")),
                category=str(i.get("category", "")),
                authority_class=str(i.get("authority_class", AUTHORITY_OTHER)),
                topic=str(i.get("topic", "")),
                requirement=str(i.get("requirement", "")),
                authority=str(i.get("authority", "")),
                code_reference=str(i.get("code_reference", "")),
                notes=str(i.get("notes", "")),
                grounded=bool(i.get("grounded", False)),
                historical_sources=tuple(str(u) for u in i.get("historical_sources", [])),
                confidence=float(i.get("confidence", 0.0) or 0.0),
                actionability=str(i.get("actionability", "spec_requirement")),
                consistency_note=str(i.get("consistency_note", "")),
            )
            for i in raw.get("items", [])
        ),
        dimension_statuses=tuple(
            DimensionOutcome(
                dimension_id=str(d.get("dimension_id", "")),
                status=str(d.get("status", "")),
                item_count=int(d.get("item_count", 0) or 0),
                error=str(d.get("error", "")),
            )
            for d in raw.get("dimension_statuses", [])
        ),
        omissions=tuple(str(o) for o in raw.get("omissions", [])),
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def module_basis_from_cycle(module_id: str, cycle: Any) -> ModuleBasis:
    """Snapshot a module's code basis, keeping provenance.

    ``StandardEdition.source`` carries the maintainer's provenance note and is
    never rendered into a prompt today; here the derived ``unverified`` flag is
    what travels, so an unconfirmed pin cannot present itself as settled.
    """
    standards = []
    for std in getattr(cycle, "standards", ()) or ():
        source = str(getattr(std, "source", "") or "")
        edition = str(getattr(std, "edition", ""))
        standards.append(
            StandardPin(
                name=str(getattr(std, "name", "")),
                edition=edition,
                provenance=source,
                unverified=source.strip().upper().startswith("UNVERIFIED"),
                # The module's own rendering, so a qualifier ("where an EPSS or
                # owner criterion invokes it", "as amended by California") is
                # never dropped on the way into the prompt.
                edition_phrase=str(getattr(std, "edition_phrase", "") or edition),
                note=str(getattr(std, "note", "") or ""),
                ca_amended=bool(getattr(std, "ca_amended", False)),
            )
        )
    base_codes = tuple(
        (str(bc.key), str(bc.name), str(bc.year))
        for bc in getattr(cycle, "base_codes", ()) or ()
    )
    return ModuleBasis(
        module_id=module_id,
        cycle_label=str(getattr(cycle, "label", "")),
        base_codes=base_codes,
        asce7=str(getattr(cycle, "asce7", "") or ""),
        asce7_previous=str(getattr(cycle, "asce7_previous", "") or ""),
        standards=tuple(standards),
    )


def _normalize_item(raw: Any) -> BasisItem:
    category = str(_get(raw, "category", "")).strip()
    if category not in RESEARCH_ITEM_CATEGORIES:
        category = "site_environment"

    actionability = str(_get(raw, "actionability", "spec_requirement")).strip()
    if actionability not in RESEARCH_ACTIONABILITY_VALUES:
        # Matches the research parser's own coercion: over-checking is safe,
        # silently skipping a controlling requirement is not.
        actionability = "spec_requirement"

    accepted = tuple(str(u) for u in _get(raw, "accepted_sources", []) or ())
    claimed_grounded = bool(_get(raw, "grounded", False))
    grounded = claimed_grounded and bool(accepted)
    consistency_note = ""
    if claimed_grounded and not accepted:
        consistency_note = (
            "recorded as grounded by the research pass but carried no accepted "
            "citation; treated as ungrounded here"
        )

    return BasisItem(
        item_id=str(_get(raw, "item_id", "")),
        dimension_id=str(_get(raw, "dimension_id", "")),
        category=category,
        authority_class=_authority_class(category),
        topic=str(_get(raw, "topic", "")),
        requirement=str(_get(raw, "requirement", "")),
        authority=str(_get(raw, "authority", "")),
        code_reference=str(_get(raw, "code_reference", "")),
        notes=str(_get(raw, "notes", "")),
        grounded=grounded,
        historical_sources=accepted,
        confidence=float(_get(raw, "confidence", 0.0) or 0.0),
        actionability=actionability,
        consistency_note=consistency_note,
    )


def _selection_rank(item: BasisItem) -> tuple:
    """Deterministic ordering. Never depends on model confidence alone.

    Confidence breaks ties *within* a category only, and item_id breaks ties
    after that, so two runs with the same facts always select the same subset.
    Ordering by confidence across categories would let a high-confidence site
    note displace a grounded adoption fact.
    """
    try:
        cat_rank = _PRIORITY_CATEGORIES.index(item.category)
    except ValueError:
        cat_rank = len(_PRIORITY_CATEGORIES) + (
            0 if item.authority_class == AUTHORITY_CONTRACTUAL else 1
        )
    return (
        cat_rank,
        0 if item.grounded else 1,
        0 if not item.is_advisory else 1,
        -round(item.confidence, 4),
        item.item_id,
    )


def _item_size_estimate(item: BasisItem) -> int:
    """Upper-bound token cost of rendering one item.

    Includes the scaffolding :func:`_render_item` emits, not just the item's
    own text: a budget that counts less than it renders is not a budget.
    """
    text = " ".join(
        [
            item.item_id,
            item.topic,
            item.requirement,
            item.authority,
            item.code_reference,
            item.notes,
            item.consistency_note,
        ]
    )
    return _ITEM_RENDER_OVERHEAD_TOKENS + max(1, int(len(text) / _CHARS_PER_TOKEN))


def build_verification_basis(
    *,
    module_id: str,
    cycle: Any,
    profile: Any | None = None,
    project: dict[str, str] | None = None,
    token_budget: int = DEFAULT_BASIS_TOKEN_BUDGET,
) -> VerificationBasis:
    """Build the immutable basis for one run.

    ``profile`` is any object exposing ``items`` / ``dimension_statuses`` /
    ``research_date`` / ``project`` (a ``RequirementsProfile``, or the dict it
    serializes to). ``None`` yields the provenance-only mode: the module's own
    pins, disclosed as assumptions, which is what a profile-less data-center run
    must carry instead of inheriting an authoritative-cycle assumption.

    Selection is deterministic and bounded. Items are taken in
    :func:`_selection_rank` order until ``token_budget`` is reached; whatever
    does not fit is dropped **whole** and named in ``omissions``. A qualification
    is never cut mid-sentence, because half a legal caveat reads as a different
    requirement.
    """
    module_basis = module_basis_from_cycle(module_id, cycle)

    if profile is None:
        return VerificationBasis(
            schema_version=BASIS_SCHEMA_VERSION,
            policy_version=BASIS_POLICY_VERSION,
            mode=MODE_PROVENANCE_ONLY,
            module_basis=module_basis,
            research_state=RESEARCH_STATE_UNAVAILABLE,
            research_date="",
            project=dict(project or {}),
            items=(),
            dimension_statuses=(),
            omissions=tuple(
                note
                for note in (
                    "No jurisdiction research is attached to this run. The "
                    "module's pinned editions are reference assumptions only, "
                    "not established adoptions for this project.",
                    _unverified_pin_omission(module_basis),
                )
                if note
            ),
        )

    raw_items: Sequence[Any] = _get(profile, "items", []) or []
    normalized = [_normalize_item(i) for i in raw_items]
    normalized.sort(key=_selection_rank)

    kept: list[BasisItem] = []
    dropped: list[BasisItem] = []
    used = 0
    for item in normalized:
        size = _item_size_estimate(item)
        if used + size > token_budget and kept:
            dropped.append(item)
            continue
        kept.append(item)
        used += size
    # The first item is admitted unconditionally: a basis that silently
    # rendered nothing would be indistinguishable from a run with no research.
    # But that admission can put the block over budget, and an over-budget
    # block the caller was told is bounded is exactly the kind of quiet
    # breach this budget exists to prevent — so say so.
    overflow = used > token_budget

    statuses = tuple(
        DimensionOutcome(
            dimension_id=str(_get(d, "dimension_id", "")),
            status=str(_get(d, "status", "")),
            item_count=int(_get(d, "item_count", 0) or 0),
            error=str(_get(d, "error", "")),
        )
        for d in (_get(profile, "dimension_statuses", []) or [])
    )

    failed = [d for d in statuses if d.status != "completed"]
    if not statuses:
        research_state = RESEARCH_STATE_AVAILABLE if kept else RESEARCH_STATE_UNAVAILABLE
    elif failed and len(failed) == len(statuses):
        research_state = RESEARCH_STATE_UNAVAILABLE
    elif failed:
        research_state = RESEARCH_STATE_PARTIAL
    else:
        research_state = RESEARCH_STATE_AVAILABLE

    omissions: list[str] = []
    if overflow:
        omissions.append(
            f"The selected facts are estimated at ~{used} tokens, over the "
            f"{token_budget}-token basis budget: the highest-priority item is "
            "admitted whole even when it alone exceeds the budget, because "
            "truncating a qualification would change what it requires."
        )
    if dropped:
        by_cat: dict[str, int] = {}
        for item in dropped:
            by_cat[item.category] = by_cat.get(item.category, 0) + 1
        detail = ", ".join(f"{n} {cat}" for cat, n in sorted(by_cat.items()))
        omissions.append(
            f"{len(dropped)} researched item(s) omitted to stay within the "
            f"{token_budget}-token basis budget ({detail}). Items were dropped "
            "whole, lowest-priority first; none was truncated. Applicability "
            "questions that would have turned on an omitted item are NOT settled "
            "by this basis."
        )
    for d in failed:
        omissions.append(
            f"Research dimension '{d.dimension_id}' did not complete"
            + (f": {d.error}" if d.error else ".")
            + " Facts it would have established are missing, not absent."
        )
    pin_note = _unverified_pin_omission(module_basis)
    if pin_note:
        omissions.append(pin_note)

    profile_project = _get(profile, "project", None)
    resolved_project = dict(project or profile_project or {})

    return VerificationBasis(
        schema_version=BASIS_SCHEMA_VERSION,
        policy_version=BASIS_POLICY_VERSION,
        mode=MODE_RESEARCHED_CONTEXT,
        module_basis=module_basis,
        research_state=research_state,
        research_date=str(_get(profile, "research_date", "")),
        project={str(k): str(v) for k, v in resolved_project.items()},
        items=tuple(kept),
        dimension_statuses=statuses,
        omissions=tuple(omissions),
    )


_RECOVERED_PINS_NOTE = (
    "This run was recovered from saved state that predates the governing "
    "basis, so the assumptions it was originally reviewed under cannot be "
    "reconstructed. The module pins below are TODAY's values and must not "
    "be read as the ones that governed the original review."
)


def recovered_basis(
    module_id: str,
    cycle: Any,
    *,
    profile: Any | None = None,
    project: dict[str, str] | None = None,
    reason: str = "",
) -> VerificationBasis:
    """Basis for a run recovered from state that predates the snapshot.

    Distinct from provenance-only: research may well have run, we simply cannot
    say what it found. Pretending today's module pins were the original
    assumptions would let a resumed run answer a question it never asked.

    ``profile`` recovers what *was* saved. A pending run written before the
    basis existed can still carry its structured research and project identity,
    and discarding those would make a run that did real research
    indistinguishable from one that did none — a worse lie than admitting the
    pins are unknown. The recovered facts are carried; only the module
    assumptions are marked unreconstructable.

    ``reason`` names why recovery was needed (a legacy record, an unreadable
    snapshot) so the degradation is visible rather than inferred.
    """
    base = build_verification_basis(
        module_id=module_id, cycle=cycle, profile=profile, project=project
    )
    omissions = [_RECOVERED_PINS_NOTE]
    if reason:
        omissions.append(f"Recovery reason: {reason}")
    # Keep whatever the rebuilt basis already disclosed (dropped items, failed
    # dimensions, UNVERIFIED pins) — those limits are still true of the facts
    # actually recovered.
    omissions.extend(o for o in base.omissions if o not in omissions)
    return replace(
        base,
        research_state=RESEARCH_STATE_RECOVERED,
        omissions=tuple(omissions),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_item(item: BasisItem) -> list[str]:
    lines = [f"  - [{item.item_id or 'unknown'}] {item.topic or item.category}"]
    lines.append(f"      requirement: {item.requirement}")
    if item.authority:
        lines.append(f"      authority: {item.authority}")
    if item.code_reference:
        lines.append(f"      code reference: {item.code_reference}")
    if item.notes:
        lines.append(f"      qualifications: {item.notes}")
    marks = []
    marks.append("grounded by research" if item.grounded else "NOT grounded by research")
    if item.is_advisory:
        marks.append("process advisory — not a specification requirement")
    if item.consistency_note:
        marks.append(item.consistency_note)
    lines.append(f"      provenance: {'; '.join(marks)}")
    return lines


def render_basis_text(basis: VerificationBasis) -> str:
    """Plain-text rendering for the verifier prompt.

    Escaping for prompt boundaries is the caller's job (the prompt builders own
    ``prompt_serialization``); this returns the content, not a wrapped block.

    The framing states what the reader must not assume: researched claims are
    claims to investigate, module pins are reference assumptions, and the
    research pass's own citations are not evidence this conversation retrieved.
    """
    out: list[str] = []
    out.append("GOVERNING BASIS FOR THIS PROJECT")
    out.append("")
    out.append(
        "This is recorded context, not evidence. Researched claims below are "
        "claims to investigate, with the date and limits noted. Module pinned "
        "editions are reference assumptions unless applicability is established. "
        "Citations recorded here were retrieved by an earlier research pass and "
        "do NOT count as sources retrieved in this conversation."
    )
    out.append("")

    if basis.project:
        loc = ", ".join(
            str(basis.project[k])
            for k in ("city", "state_or_province", "country")
            if basis.project.get(k)
        )
        if loc:
            out.append(f"Project location: {loc}")
        if basis.project.get("client_name"):
            out.append(f"Client: {basis.project['client_name']}")
        out.append("")

    mb = basis.module_basis
    out.append(f"Module reference basis ({mb.module_id} / {mb.cycle_label}):")
    for _key, name, year in mb.base_codes:
        out.append(f"  - {name} {year} (module reference assumption)")
    if mb.asce7:
        out.append(f"  - ASCE {mb.asce7} (module reference assumption)")
    for pin in mb.standards:
        if not pin.edition:
            continue
        flag = " [UNVERIFIED provenance]" if pin.unverified else ""
        out.append(f"  - {pin.name}: {pin.edition_phrase or pin.edition}{flag}")
    out.append("")

    out.append(f"Research state: {basis.research_state}")
    if basis.research_date:
        out.append(f"Research as of: {basis.research_date}")
    out.append("")

    if basis.items:
        controlling = [i for i in basis.items if i.authority_class == AUTHORITY_ADOPTED_LAW]
        contractual = [i for i in basis.items if i.authority_class == AUTHORITY_CONTRACTUAL]
        other = [i for i in basis.items if i.authority_class == AUTHORITY_OTHER]

        if controlling:
            out.append("Adopted law and AHJ requirements (researched claims):")
            for item in controlling:
                out.extend(_render_item(item))
            out.append("")
        if contractual:
            out.append(
                "Client / owner / insurer requirements — CONTRACTUAL authority, "
                "not adopted law. A contractual requirement can be stricter than "
                "code without the code citation being wrong, and satisfying one "
                "does not discharge the other:"
            )
            for item in contractual:
                out.extend(_render_item(item))
            out.append("")
        if other:
            out.append("Other researched context:")
            for item in other:
                out.extend(_render_item(item))
            out.append("")

    if basis.omissions:
        out.append("Known limits of this basis:")
        for note in basis.omissions:
            out.append(f"  - {note}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def historical_source_urls(basis: VerificationBasis) -> tuple[str, ...]:
    """Every URL the basis carries as historical provenance.

    Exposed so the verification path can assert these never enter the accepted
    retrieval pool. A researched URL that has not been retrieved in the current
    conversation must not be able to ground a verdict.
    """
    seen: list[str] = []
    for item in basis.items:
        for url in item.historical_sources:
            if url not in seen:
                seen.append(url)
    return tuple(seen)


def validate_basis(basis: VerificationBasis) -> list[str]:
    """Structural problems with a basis; empty means well-formed."""
    problems: list[str] = []
    if basis.schema_version != BASIS_SCHEMA_VERSION:
        problems.append(f"schema_version {basis.schema_version} unsupported")
    if basis.policy_version != BASIS_POLICY_VERSION:
        problems.append(f"policy_version {basis.policy_version} unsupported")
    if basis.mode not in {MODE_PROVENANCE_ONLY, MODE_RESEARCHED_CONTEXT}:
        problems.append(f"mode {basis.mode!r} unknown")
    if basis.research_state not in _RESEARCH_STATES:
        problems.append(f"research_state {basis.research_state!r} unknown")
    if basis.mode == MODE_PROVENANCE_ONLY and basis.items:
        problems.append("provenance-only basis must carry no researched items")
    for item in basis.items:
        if item.grounded and not item.historical_sources:
            problems.append(
                f"{item.item_id}: grounded with no historical sources — the "
                "consistency re-check should have cleared this"
            )
        if item.authority_class not in {
            AUTHORITY_ADOPTED_LAW,
            AUTHORITY_CONTRACTUAL,
            AUTHORITY_OTHER,
        }:
            problems.append(f"{item.item_id}: unknown authority_class")
    return problems


__all__ = [
    "AUTHORITY_ADOPTED_LAW",
    "AUTHORITY_CONTRACTUAL",
    "AUTHORITY_OTHER",
    "BASIS_POLICY_VERSION",
    "BASIS_SCHEMA_VERSION",
    "BasisItem",
    "BasisPolicyIncompatible",
    "DEFAULT_BASIS_TOKEN_BUDGET",
    "DimensionOutcome",
    "MODE_PROVENANCE_ONLY",
    "MODE_RESEARCHED_CONTEXT",
    "ModuleBasis",
    "RESEARCH_STATE_AVAILABLE",
    "RESEARCH_STATE_PARTIAL",
    "RESEARCH_STATE_RECOVERED",
    "RESEARCH_STATE_UNAVAILABLE",
    "StandardPin",
    "VerificationBasis",
    "basis_from_dict",
    "build_verification_basis",
    "historical_source_urls",
    "module_basis_from_cycle",
    "recovered_basis",
    "render_basis_text",
    "validate_basis",
]
