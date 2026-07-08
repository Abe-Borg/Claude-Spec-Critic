"""Core :class:`ReviewModule` type and registry validation.

A **module** is one reviewable domain configuration — "California K-12 DSA
mechanical/plumbing" is the first; a future "hyperscale data-center fire
suppression" is the motivating second. The module is deliberately a single
atomic selection (one frozen object picked from a registry), not a set of
independent runtime knobs, so incoherent combinations (one domain's severity
anchors with another domain's code basis) are unrepresentable.

Phase 1 gave the module *identity* (``module_id`` + display strings) and the
*code basis* (the existing :class:`CodeCycle`, untouched). Phase 2 moves the
**prompt content slots** onto it: the reviewer / cross-check / verifier
personas, severity anchors, the review category list, the few-shot examples,
and the verifier's authoritative-source tiers. The prompt *protocol* — tool
contracts, confidence-rubric bands, evidence rules, grounding language —
stays engine-owned in the prompt builders, byte-identical across modules, so
a module author cannot break the parse contracts. Later phases move the
deterministic-detector vocabulary, the verification-profile keywords, and
the cross-check chunk map.

Invariants:

- ``module_id`` is the stable registry key. It is persisted into the
  pending-batch resume state and stamped into trace run metadata, so treat a
  rename like a schema change (legacy ids must keep resolving).
- Cycle labels are **globally unique across modules** (enforced by
  :func:`validate_module_registry`). Two consequences: the verification
  cache (keyed by cycle label) cannot collide across modules, and
  ``registry.module_for_cycle`` is a well-defined reverse lookup — the
  bridge that lets content layers still keyed by ``cycle=`` reach their
  module's prompt slots without signature churn.
- Prompt slots are validated at registration (:func:`validate_module_registry`
  → :func:`_validate_module_content`): non-empty, the categories template
  formats against the module's own cycle, and every JSON example in
  ``review_examples`` must satisfy the *real* edit-shape contract
  (``reviewer.validate_edit_shape``) so a module cannot ship few-shot
  examples that teach the model a shape the parser demotes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from ..core.code_cycles import CodeCycle


@dataclass(frozen=True)
class DetectorVocabulary:
    """Vocabulary the deterministic preprocessor scans for, per module.

    The detector *logic* (regex assembly, span dedup, the negation
    suppression window, sentence narrowing) stays engine-owned in
    ``input/preprocessor.py``; this dataclass carries only the domain data.
    Frozen + tuple-typed so it is hashable — the engine caches compiled
    patterns per vocabulary.

    Attributes:
        code_abbreviations: Abbreviations recognized next to a year
            (``"2019 CBC"`` / ``"CBC 2019"``). Escaped and alternated into
            the engine's year/code patterns.
        plausible_cycle_years: Real historical cycle years the *stale*
            detector may flag (a found year must be in this set and differ
            from the cycle's ``primary_code_year``). Keep it a recent window
            so legitimate distant-historical references don't alert.
        valid_cycle_years: Every year the jurisdiction has published (or
            announced) a cycle for. The *invalid* detector flags year/code
            citations outside this set. Must be a superset of
            ``plausible_cycle_years`` — that superset relationship is what
            keeps the stale and invalid detectors disjoint by construction
            (validated at registration).
        asce7_plausible_editions: Two-digit ASCE 7 editions the stale
            detector recognizes (recognition whitelist so a stray capture
            like ``7-42`` is ignored).
        stale_cycle_extra_patterns: Additional regex *sources* alternated
            after the engine's two year/code patterns (e.g. California's
            long-form ``"2019 California Building Code"``). Each must
            capture the year as group 1; compiled case-insensitive.
            Validated compilable at registration.
        flag_leed_references: Whether LEED references are appropriateness
            alerts for this domain (True for CA K-12 DSA, where LEED is
            typically a copy/paste error; a data-center module that
            genuinely pursues LEED sets False).
        jurisdiction_label: Word spliced into the invalid-cycle alert text
            (``"Invalid California code cycle year"``). Empty renders the
            generic ``"Invalid code cycle year"``.
    """

    code_abbreviations: tuple[str, ...]
    plausible_cycle_years: tuple[str, ...]
    valid_cycle_years: tuple[str, ...]
    asce7_plausible_editions: tuple[str, ...]
    stale_cycle_extra_patterns: tuple[str, ...] = ()
    flag_leed_references: bool = True
    jurisdiction_label: str = ""


@dataclass(frozen=True)
class ReviewModule:
    """One reviewable domain configuration.

    Attributes:
        module_id: Stable registry key (e.g. ``"california_k12_mep"``).
            Persisted into resume state and trace metadata — never rendered
            into a prompt.
        display_name: Human-readable name for GUI / report surfaces.
        description: One-line summary for GUI / About surfaces.
        cycle: The code basis this module reviews against. Phase 3
            generalizes :class:`CodeCycle` beyond its California-shaped
            fields; until then the module simply carries the existing object.
        reviewer_persona: First line of the reviewer system prompt — who the
            reviewer is and what project context applies.
        review_user_intro: First line of the per-spec review user message.
        review_severity_definitions: The CRITICAL/HIGH/MEDIUM/GRIPES anchor
            lines for the reviewer prompt (block interior only — the engine
            supplies the ``<severity_definitions>`` wrapper). The four
            severity *names* are protocol (report rendering and verification
            budgets key on them); the anchor examples are the domain content.
        review_confidence_high_example: The domain example spliced into the
            high-confidence rubric band. The band thresholds themselves are
            protocol (the report's confidence colors depend on them).
        review_categories_template: The numbered review-scope category list.
            May reference the placeholders from
            :func:`code_basis_format_kwargs`; formatted against the module's
            cycle at prompt-build time (and at registration, so a typo'd
            placeholder fails startup, not a run).
        review_examples: The few-shot examples block. JSON examples inside it
            are validated against the real edit-shape contract at
            registration. Must not reference ``evidenceElementId`` or
            element-id tags — those are per-request concepts and the block
            is part of the cached system-prompt prefix.
        cross_check_persona: First line of the cross-spec coordination
            system prompt.
        cross_check_severity_definitions: Severity anchor lines for the
            cross-check prompt (block interior only).
        verifier_persona: First line of the verifier system prompt.
        verifier_source_priorities: The numbered authoritative-source tier
            list for the verifier prompt (the ``Prefer authoritative
            sources`` header and the surrounding guidance are engine
            protocol; the tiers and domains are the domain content).
        review_user_code_basis_line: The "Current code cycle: …" line of the
            review user message. Like every ``*_code_basis_line*`` slot, a
            template formatted against :func:`code_basis_format_kwargs` —
            the module owns the display labels and which codes each surface
            names, so per-surface phrasing stays byte-controlled.
        cross_check_code_basis_line: Cycle line of the cross-check system
            prompt.
        verifier_system_code_basis_lines: Cycle line(s) of the verifier
            system prompt (may contain ``\\n`` — spliced line-by-line).
        verifier_user_code_basis_lines: Cycle line(s) of the per-finding
            verifier user prompt.
        detector_vocabulary: The deterministic preprocessor's domain
            vocabulary (see :class:`DetectorVocabulary`).
    """

    module_id: str
    display_name: str
    description: str
    cycle: CodeCycle
    # --- Prompt content slots (Phase 2) --------------------------------
    reviewer_persona: str
    review_user_intro: str
    review_severity_definitions: str
    review_confidence_high_example: str
    review_categories_template: str
    review_examples: str
    cross_check_persona: str
    cross_check_severity_definitions: str
    verifier_persona: str
    verifier_source_priorities: str
    # --- Code-basis rendering + detector vocabulary (Phase 3) ----------
    review_user_code_basis_line: str
    cross_check_code_basis_line: str
    verifier_system_code_basis_lines: str
    verifier_user_code_basis_lines: str
    detector_vocabulary: DetectorVocabulary


_PROMPT_SLOT_FIELDS: tuple[str, ...] = (
    "reviewer_persona",
    "review_user_intro",
    "review_severity_definitions",
    "review_confidence_high_example",
    "review_categories_template",
    "review_examples",
    "cross_check_persona",
    "cross_check_severity_definitions",
    "verifier_persona",
    "verifier_source_priorities",
    "review_user_code_basis_line",
    "cross_check_code_basis_line",
    "verifier_system_code_basis_lines",
    "verifier_user_code_basis_lines",
)

# Slots that are str.format templates over ``code_basis_format_kwargs`` —
# each is format-checked against the module's own cycle at registration so a
# typo'd placeholder fails startup, not a run.
_TEMPLATE_SLOT_FIELDS: tuple[str, ...] = (
    "review_categories_template",
    "review_user_code_basis_line",
    "cross_check_code_basis_line",
    "verifier_system_code_basis_lines",
    "verifier_user_code_basis_lines",
)

# The severity names and action types are protocol — report rendering,
# verification budgets, and the edit pipeline key on these closed sets.
_VALID_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "GRIPES"})
_VALID_ACTIONS = frozenset({"EDIT", "DELETE", "ADD", "REPORT_ONLY"})


def code_basis_format_kwargs(cycle: CodeCycle) -> dict[str, str]:
    """Placeholders a module's template slots may reference.

    Derived entirely from the module's :class:`CodeCycle`: one placeholder
    per :class:`BaseCode` (keyed by ``BaseCode.key``) plus ``asce7`` /
    ``asce7_prev`` and the ``pinned_standards`` inline phrase. Used by
    ``review_categories_template`` and every ``*_code_basis_line*`` slot —
    a module's placeholder set is therefore defined by its own cycle, and
    registration verifies each template formats against it.
    """
    kwargs = {code.key: code.year for code in cycle.base_codes}
    kwargs.update(
        asce7=cycle.asce7,
        asce7_prev=cycle.asce7_previous,
        pinned_standards=cycle.edition_inline_phrase() or "current editions",
    )
    return kwargs


def _iter_json_objects(text: str) -> Iterator[Mapping[str, object]]:
    """Yield every top-level JSON object embedded in ``text``.

    Scans for ``{`` and attempts a strict decode at each candidate — prose
    around the examples is skipped, nested objects are consumed as part of
    their parent. This is how registration finds the JSON few-shot examples
    inside a module's ``review_examples`` block without imposing a rigid
    layout on the surrounding teaching prose.
    """
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            yield obj
        idx = end


def _validate_review_examples(module: ReviewModule) -> None:
    """Every JSON example must satisfy the real parse-time edit contract."""
    # Deferred imports: reviewer pulls in the Anthropic SDK and must never
    # import back into ``modules`` (it doesn't — it is a content-layer leaf);
    # prompt_serialization is the single source of truth for the element-id
    # wrapper tag names, so a future tag rename updates this guard too.
    from ..review.prompt_serialization import TAG_HEADING, TAG_PARA, TAG_ROW
    from ..review.reviewer import validate_edit_shape

    block = module.review_examples
    # These are per-request concepts; the examples block is part of the
    # cached system-prompt prefix and must not mention them (pinned by
    # ``test_system_prompt_constant_and_does_not_embed_specs``). Every
    # element-id wrapper tag is forbidden — an example showing a stale
    # ``<row id="…">`` would teach the model to emit invalid
    # ``evidenceElementId`` values.
    element_wrappers = tuple(f"<{tag}" for tag in (TAG_PARA, TAG_ROW, TAG_HEADING))
    for forbidden in ("evidenceElementId", *element_wrappers):
        if forbidden in block:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: review_examples must not "
                f"reference {forbidden!r} (per-request concept inside the "
                "cached system-prompt prefix)"
            )

    examples = list(_iter_json_objects(block))
    if not examples:
        raise ValueError(
            f"ReviewModule {module.module_id!r}: review_examples contains no "
            "parseable JSON example findings"
        )
    for i, ex in enumerate(examples, start=1):
        severity = str(ex.get("severity") or "")
        if severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: example {i} has severity "
                f"{severity!r} outside {sorted(_VALID_SEVERITIES)}"
            )
        action = str(ex.get("actionType") or "")
        if action not in _VALID_ACTIONS:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: example {i} has actionType "
                f"{action!r} outside {sorted(_VALID_ACTIONS)}"
            )
        confidence = ex.get("confidence")
        if confidence is not None and not (
            isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0
        ):
            raise ValueError(
                f"ReviewModule {module.module_id!r}: example {i} confidence "
                f"{confidence!r} is not in [0, 1]"
            )
        demotion = validate_edit_shape(
            action,
            existing_text=ex.get("existingText"),
            replacement_text=ex.get("replacementText"),
            anchor_text=ex.get("anchorText"),
            insert_position=ex.get("insertPosition"),
        )
        if demotion is not None:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: example {i} "
                f"({action}) would be demoted by the parser: {demotion}. "
                "Few-shot examples must teach shapes the parser accepts."
            )


def _validate_detector_vocabulary(module: ReviewModule) -> None:
    """Fail fast on preprocessor vocabulary a module author got wrong."""
    vocab = module.detector_vocabulary
    if not isinstance(vocab, DetectorVocabulary):
        raise ValueError(
            f"ReviewModule {module.module_id!r}: detector_vocabulary must be a "
            f"DetectorVocabulary, got {type(vocab).__name__}"
        )
    if not vocab.code_abbreviations or not all(
        a and a.strip() for a in vocab.code_abbreviations
    ):
        raise ValueError(
            f"ReviewModule {module.module_id!r}: detector_vocabulary needs at "
            "least one non-empty code abbreviation"
        )
    # The stale detector flags years in the plausible set; the invalid
    # detector flags years OUTSIDE the valid set. plausible ⊆ valid is what
    # keeps the two disjoint by construction — a year can't be both a real
    # historical cycle and a fabrication.
    missing = set(vocab.plausible_cycle_years) - set(vocab.valid_cycle_years)
    if missing:
        raise ValueError(
            f"ReviewModule {module.module_id!r}: plausible_cycle_years must be "
            f"a subset of valid_cycle_years (missing: {sorted(missing)})"
        )
    for src in vocab.stale_cycle_extra_patterns:
        try:
            compiled = re.compile(src, flags=re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: stale_cycle_extra_patterns "
                f"entry does not compile: {src!r} ({exc})"
            ) from exc
        if compiled.groups < 1:
            raise ValueError(
                f"ReviewModule {module.module_id!r}: stale_cycle_extra_patterns "
                f"entry must capture the year as group 1: {src!r}"
            )


def _validate_module_content(module: ReviewModule) -> None:
    """Fail fast on prompt-slot / vocabulary content a module author got wrong."""
    for field_name in _PROMPT_SLOT_FIELDS:
        value = getattr(module, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"ReviewModule {module.module_id!r} has an empty prompt slot: "
                f"{field_name}"
            )

    codes = module.cycle.base_codes if module.cycle else ()
    if not codes:
        raise ValueError(
            f"ReviewModule {module.module_id!r}: cycle pins no base_codes — "
            "the first base code is the stale-detector target and the "
            "template-placeholder source"
        )
    keys = [code.key for code in codes]
    if len(set(keys)) != len(keys) or not all(k and k.strip() and code.year for k, code in zip(keys, codes)):
        raise ValueError(
            f"ReviewModule {module.module_id!r}: base_codes need unique, "
            f"non-empty keys and non-empty years (got keys {keys})"
        )

    kwargs = code_basis_format_kwargs(module.cycle)
    for field_name in _TEMPLATE_SLOT_FIELDS:
        try:
            getattr(module, field_name).format(**kwargs)
        except Exception as exc:  # KeyError / IndexError / ValueError from format
            raise ValueError(
                f"ReviewModule {module.module_id!r}: {field_name} does not "
                f"format against its own cycle ({exc!r}). Available "
                f"placeholders: {sorted(kwargs)}"
            ) from exc

    _validate_detector_vocabulary(module)
    _validate_review_examples(module)


def validate_module_registry(modules: Iterable[ReviewModule]) -> None:
    """Fail fast (``ValueError``) on an inconsistent module registry.

    Runs at import time in :mod:`registry` so a bad module definition breaks
    app startup, not a batch three hours in. Checks:

    - every ``module_id`` / ``display_name`` is non-empty and stripped;
    - ``module_id`` values are unique;
    - every module pins a cycle with a non-empty label;
    - cycle labels are unique across modules (the verification-cache
      namespace rule + the ``module_for_cycle`` bridge foundation — see the
      module docstring);
    - prompt-slot content is well-formed (:func:`_validate_module_content`),
      including parsing every JSON few-shot example through the real
      edit-shape contract.
    """
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for module in modules:
        if not module.module_id or module.module_id != module.module_id.strip():
            raise ValueError(
                f"ReviewModule has an empty or unstripped module_id: {module.module_id!r}"
            )
        if not module.display_name or not module.display_name.strip():
            raise ValueError(
                f"ReviewModule {module.module_id!r} has an empty display_name"
            )
        if module.module_id in seen_ids:
            raise ValueError(f"Duplicate module_id in registry: {module.module_id!r}")
        seen_ids.add(module.module_id)

        label = (module.cycle.label or "").strip() if module.cycle else ""
        if not label:
            raise ValueError(
                f"ReviewModule {module.module_id!r} pins no code cycle label"
            )
        if label in seen_labels:
            raise ValueError(
                f"Duplicate cycle label {label!r} across modules — cycle labels "
                "namespace the verification cache and must be registry-unique"
            )
        seen_labels.add(label)

        _validate_module_content(module)
