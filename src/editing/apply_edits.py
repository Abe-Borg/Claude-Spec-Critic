"""Orchestration helpers for applying selected review edits to DOCX specs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from typing import TYPE_CHECKING

from .edit_locator import locate_edits, locator_evidence_from_result
from ..input.extractor import ExtractedSpec, extract_text_from_docx
from ..review.reviewer import Finding
from .spec_editor import (
    EditAction,
    EditOutcome,
    EditReport,
    apply_edits_to_spec,
    build_edit_actions,
)

if TYPE_CHECKING:
    from ..orchestration.diagnostics import DiagnosticsReport


def _ensure_paragraph_maps(specs: list[ExtractedSpec], source_paths: list[Path]) -> list[ExtractedSpec]:
    """Re-extract specs that are missing paragraph maps."""
    hydrated: list[ExtractedSpec] = []
    for spec, path in zip(specs, source_paths):
        if spec.paragraph_map is not None:
            hydrated.append(spec)
            continue

        if path.exists():
            hydrated.append(extract_text_from_docx(path))
            continue

        hydrated.append(spec)
    return hydrated


def _make_output_path(source_path: Path, output_dir: Path) -> Path:
    """Build a non-overwriting output path using _edited suffix (+ timestamp fallback)."""
    preferred = output_dir / f"{source_path.stem}_edited{source_path.suffix}"
    if not preferred.exists():
        return preferred

    timestamp = datetime.now().strftime("%H%M%S")
    return output_dir / f"{source_path.stem}_edited_{timestamp}{source_path.suffix}"


def _build_failure_report(
    *,
    source_path: Path,
    output_path: Path,
    actions: list[EditAction],
    warning: str,
) -> EditReport:
    failed_outcomes = [
        EditOutcome(
            action=action,
            status="failed",
            detail=warning,
            original_text=action.location.matched_text,
            new_text=None,
        )
        for action in actions
    ]
    return EditReport(
        source_path=source_path,
        output_path=output_path,
        total_edits_attempted=len(actions),
        edits_applied=0,
        edits_skipped=0,
        edits_failed=len(actions),
        outcomes=failed_outcomes,
        warnings=[warning],
    )


def execute_edit_plan(
    selected_finding_indices: list[int],
    all_findings: list[Finding],
    cross_check_findings: list[Finding],
    extracted_specs: list[ExtractedSpec],
    source_paths: list[Path],
    output_dir: Path,
    *,
    log: Callable[[str], None] = lambda _: None,
    diagnostics: "DiagnosticsReport | None" = None,
) -> list[EditReport]:
    """Run locate -> action build -> apply workflow for selected findings."""
    output_dir.mkdir(parents=True, exist_ok=True)

    mapped_specs = _ensure_paragraph_maps(extracted_specs, source_paths)

    source_by_name = {path.name: path for path in source_paths}
    spec_by_filename = {spec.filename: spec for spec in mapped_specs}
    filename_map: dict[str, tuple[ExtractedSpec, Path]] = {}
    for filename, spec in spec_by_filename.items():
        source_path = source_by_name.get(filename)
        if source_path is not None:
            filename_map[filename] = (spec, source_path)

    merged_findings = list(all_findings) + list(cross_check_findings)
    selected_pairs: list[tuple[int, Finding]] = []
    for idx in selected_finding_indices:
        if 0 <= idx < len(merged_findings):
            selected_pairs.append((idx, merged_findings[idx]))
        else:
            log(f"Skipping out-of-range selected finding index: {idx}")

    # Findings that were merged across multiple files during deduplication
    # carry every affected file in `affected_files`. The display layer keeps a
    # single representative row, but edit application must fan out to every
    # file or the user only edits one of N affected specs (audit Issue 3).
    #
    # Chunk 8: Display deduplication is fine for the report, but executable
    # edits MUST use the per-file pre-merge original (existingText,
    # replacementText, anchorText, evidenceElementId, edit_proposal) and not
    # the representative's. ``Finding.occurrence_originals`` is populated by
    # ``_deduplicate_findings`` for merged findings; lookup by fileName.
    # When the per-file original is missing (legacy resume payload, or an
    # affected file that wasn't reviewed in this run), the representative's
    # text is only safe on the representative's own file — other affected
    # files are routed to manual review rather than guessing.
    findings_by_file: dict[str, list[tuple[int, Finding, Finding | None]]] = defaultdict(list)
    manual_review_skips: dict[str, list[tuple[int, Finding]]] = defaultdict(list)
    for original_index, finding in selected_pairs:
        target_files = list(dict.fromkeys(finding.affected_files)) or (
            [finding.fileName] if finding.fileName else []
        )
        if not target_files:
            log(f"Skipping selected finding #{original_index}: no associated file name.")
            continue
        originals_by_file = {
            orig.fileName: orig
            for orig in finding.occurrence_originals
            if orig.fileName
        }
        for file_name in target_files:
            per_file_original = originals_by_file.get(file_name)
            if per_file_original is None and file_name == finding.fileName:
                # The representative IS the original for its own file.
                per_file_original = finding
            if per_file_original is None:
                # Chunk 8: a different file in affected_files but no per-file
                # original was recorded. The representative's text was
                # produced from its own file, so applying it here would be a
                # guess. Route to manual review.
                log(
                    f"[{file_name}] Finding #{original_index}: no per-file edit original "
                    "recorded for this affected file; routed to manual review (Chunk 8 safety)."
                )
                manual_review_skips[file_name].append((original_index, finding))
                continue
            findings_by_file[file_name].append((original_index, finding, per_file_original))

    reports: list[EditReport] = []

    for filename, indexed_findings in findings_by_file.items():
        spec_and_path = filename_map.get(filename)
        if spec_and_path is None:
            log(f"Skipping '{filename}': no matching source file/spec pair was found.")
            continue

        spec, source_path = spec_and_path
        paragraph_map = spec.paragraph_map
        if not paragraph_map:
            log(f"Skipping '{filename}': paragraph map unavailable and re-extraction was not possible.")
            continue

        # Chunk 8: pass each file the per-file ORIGINAL finding to the
        # locator, not the merged representative. The representative's
        # ``existingText`` is for its own file only; every other affected
        # file gets its own original's text.
        findings_for_locator = [executable for _, _, executable in indexed_findings]
        locator_results = locate_edits(findings_for_locator, paragraph_map)
        # Phase 5 / Step 5.1: tally the per-spec count of CORRECTED
        # findings whose ``verification.correction`` failed the
        # replaceability sanity check (``correction_looks_replaceable``).
        # The locator already swapped in the model's original
        # ``replacement_text`` for those findings; this counter just
        # surfaces the swap to operators via per-spec EditReport and
        # the run-level diagnostics block.
        correction_rejected_per_spec = sum(
            1
            for result in locator_results
            if getattr(result, "correction_rejected_as_replacement", False)
        )
        for pair, locator_result in zip(indexed_findings, locator_results):
            original_index, representative_finding, per_file_finding = pair
            # Chunk 4 / Trust Upgrade: stash the locator-evidence snapshot
            # on both the per-file original (the locator actually ran on
            # this one) and the representative (so the report renders the
            # panel for the deduplicated entry). Both can be the same
            # object for singletons.
            evidence = locator_evidence_from_result(locator_result)
            per_file_finding.locator_evidence = evidence
            if representative_finding is not per_file_finding:
                representative_finding.locator_evidence = evidence
            if locator_result.status == "not_found":
                log(f"[{filename}] Finding #{original_index} not found in document text.")
            elif locator_result.status == "ambiguous":
                log(f"[{filename}] Finding #{original_index} matched multiple locations; skipped — review and apply manually.")
                # Phase 4 / Step 4.3: distinguish the cross-paragraph
                # multi-window subset from regular single-paragraph
                # ambiguous matches so the diagnostics rollup can show
                # how often the model emitted a repeated multi-paragraph
                # quote.
                if (
                    diagnostics is not None
                    and getattr(locator_result, "cross_paragraph_ambiguous", False)
                ):
                    diagnostics.cross_paragraph_ambiguity_routed_to_manual_count += 1
            if locator_result.warning:
                log(f"[{filename}] Finding #{original_index} warning: {locator_result.warning}")
            # Chunk K5: record locator-method telemetry and surface a
            # human-readable trace for id-based matches so a future
            # debugging session can grep the logs for "via id=" and tell
            # which findings carried evidence pointers.
            if locator_result.locations:
                best = max(
                    locator_result.locations,
                    key=lambda loc: loc.match_confidence,
                )
                if diagnostics is not None:
                    diagnostics.record_locator_method(best.match_method)
                if best.match_method == "id":
                    log(
                        f"[{filename}] Finding #{original_index} located via "
                        f"id={best.mapping.element_id!r} "
                        f"(body_index={best.mapping.body_index})."
                    )

        actions = build_edit_actions(locator_results)
        output_path = _make_output_path(source_path, output_dir)

        if not actions:
            warning = "No edits were located for selected findings; skipped writing output."
            log(f"[{filename}] {warning}")
            reports.append(
                EditReport(
                    source_path=source_path,
                    output_path=output_path,
                    total_edits_attempted=0,
                    edits_applied=0,
                    edits_skipped=len(indexed_findings),
                    edits_failed=0,
                    outcomes=[],
                    warnings=[warning],
                    # Even when no edit landed, the locator may have
                    # rejected a CORRECTED-with-explanatory-correction
                    # finding. Carry that count through so the report
                    # still surfaces "the verifier emitted explanation,
                    # not replacement text" at the spec level.
                    verifier_correction_rejected_as_replacement_count=(
                        correction_rejected_per_spec
                    ),
                )
            )
            if correction_rejected_per_spec and diagnostics is not None:
                diagnostics.verifier_correction_rejected_as_replacement_count += (
                    correction_rejected_per_spec
                )
            continue

        try:
            report = apply_edits_to_spec(source_path, output_path, actions)
            # Phase 5 / Step 5.1: stamp the locator-derived rejection
            # count onto the per-spec report. ``apply_edits_to_spec``
            # only sees ``EditAction`` objects and cannot compute this
            # from the actions alone (the rejection happened upstream
            # in ``_resolve_replacement_text``), so we set it here from
            # the locator results that produced the actions.
            report.verifier_correction_rejected_as_replacement_count = (
                correction_rejected_per_spec
            )
            reports.append(report)
            # Chunk 9: surface unsafe-markup refusals in the run log so users
            # know an auto-edit was deliberately not applied because of Word
            # markup (hyperlinks, field codes, drawings, etc.) rather than a
            # silent skip. Same for the all-or-none transactional abort.
            for outcome in getattr(report, "outcomes", []) or []:
                if getattr(outcome, "refused_unsafe_markup", False):
                    log(f"[{filename}] {outcome.detail}")
            if getattr(report, "aborted_transactional", False):
                log(
                    f"[{filename}] Edit output suppressed under all-or-none "
                    f"policy; see EditReport.warnings for details."
                )
            # Phase 1 / Step 1.1: roll the per-spec "replacement
            # normalized" counter into the run-level diagnostics so the
            # report's Auto-Apply Quality block surfaces how often the
            # style normalizer fired.
            normalized = getattr(report, "replacement_normalized_count", 0)
            if normalized and diagnostics is not None:
                diagnostics.replacement_text_normalized_count += normalized
            # Phase 1 / Step 1.2: same pattern for the punctuation
            # boundary repair counter.
            punct_fixed = getattr(report, "punctuation_boundary_fixed_count", 0)
            if punct_fixed and diagnostics is not None:
                diagnostics.punctuation_boundary_fixed_count += punct_fixed
            # Phase 2 / Step 2.2: roll up the apply-time defensive
            # refusal counter for ADDs missing a usable insertPosition.
            # Typically 0 in normal flow because the parser demotes
            # these at parse time; non-zero values are a visible signal
            # that a Finding bypassed the parser (legacy resume payload
            # or directly-constructed test fixture).
            add_pos_missing = getattr(
                report, "add_demoted_missing_position_count", 0
            )
            if add_pos_missing and diagnostics is not None:
                diagnostics.add_demoted_missing_position_count += add_pos_missing
            # Phase 3 / Step 3.2: roll up the known-pattern bold
            # restoration counter. 0 in default-off runs; non-zero
            # only when operators have enabled
            # ``SPEC_CRITIC_RESTORE_KNOWN_FORMATTING`` AND a partial
            # replacement actually crossed multi-format runs in a
            # paragraph that contained a recognized standards / code
            # token.
            known_pattern_restored = getattr(
                report, "known_pattern_formatting_restored_count", 0
            )
            if known_pattern_restored and diagnostics is not None:
                diagnostics.known_pattern_formatting_restored_count += (
                    known_pattern_restored
                )
            # Phase 4 / Step 4.1: roll up the count of narrower edits
            # whose intent was lost to a broader containing edit's
            # replacement. The broader edit applies; the narrower one
            # is recorded on ``EditReport.outcomes`` with
            # ``contained_edit_lost_intent=True`` so the per-spec
            # report still surfaces "you probably want to revisit
            # these manually". The run-level counter aggregates the
            # frequency for the diagnostics quality block.
            contained_lost_intent = getattr(
                report, "contained_edits_lost_intent_count", 0
            )
            if contained_lost_intent and diagnostics is not None:
                diagnostics.contained_edits_lost_intent_count += (
                    contained_lost_intent
                )
            # Phase 5 / Step 5.1: roll up the count of CORRECTED
            # findings whose ``verification.correction`` was rejected
            # by the replaceability sanity check. The applied edit
            # used the model's original ``replacement_text``; the
            # verifier's correction stays on the result for the
            # report. Non-zero values are a "verifier emitted
            # explanation, not replacement text" signal that
            # operators may want to revisit manually.
            verifier_correction_rejected = getattr(
                report,
                "verifier_correction_rejected_as_replacement_count",
                0,
            )
            if verifier_correction_rejected and diagnostics is not None:
                diagnostics.verifier_correction_rejected_as_replacement_count += (
                    verifier_correction_rejected
                )
        except Exception as exc:
            warning = f"Failed to apply edits: {exc}"
            log(f"[{filename}] {warning}")
            failure_report = _build_failure_report(
                source_path=source_path,
                output_path=output_path,
                actions=actions,
                warning=warning,
            )
            # Phase 5 / Step 5.1: even when the apply step blew up, the
            # locator's rejection decision still happened — surface the
            # count so the per-spec report and the diagnostics rollup
            # reflect reality.
            failure_report.verifier_correction_rejected_as_replacement_count = (
                correction_rejected_per_spec
            )
            if correction_rejected_per_spec and diagnostics is not None:
                diagnostics.verifier_correction_rejected_as_replacement_count += (
                    correction_rejected_per_spec
                )
            reports.append(failure_report)

    # Chunk 8: emit a no-output report for affected files where the per-file
    # edit original was missing. The user sees that the file was affected,
    # but no auto-edit was attempted because the representative's text would
    # have been a guess. Files that are also in ``findings_by_file`` (i.e.,
    # had at least one valid per-file original) collapse into the existing
    # per-file report rather than producing a separate manual-review entry.
    for filename, indexed_findings in manual_review_skips.items():
        if filename in findings_by_file:
            continue
        spec_and_path = filename_map.get(filename)
        if spec_and_path is None:
            log(f"Skipping manual-review report for '{filename}': no matching source file.")
            continue
        _, source_path = spec_and_path
        output_path = _make_output_path(source_path, output_dir)
        warning = (
            "Selected finding(s) had no per-file edit original recorded for this affected "
            "file. Auto-edit was skipped to avoid applying another file's text; routed to "
            "manual review (Chunk 8 safety)."
        )
        log(f"[{filename}] {warning}")
        reports.append(
            EditReport(
                source_path=source_path,
                output_path=output_path,
                total_edits_attempted=0,
                edits_applied=0,
                edits_skipped=len(indexed_findings),
                edits_failed=0,
                outcomes=[],
                warnings=[warning],
            )
        )

    return reports


def populate_locator_evidence(
    findings: list[Finding],
    extracted_specs: list[ExtractedSpec],
) -> None:
    """Stamp :attr:`Finding.locator_evidence` on findings with edit proposals.

    Chunk 4 / Trust Upgrade. Lets the report exporter render the "Edit
    Target Evidence" panel (match method, confidence, safety category,
    element id) without re-running the locator inside the exporter.
    Called once after the review/verification phases land (in
    :func:`pipeline.finalize_batch_result` and :func:`pipeline.run_review`)
    so the first-time report — exported before
    :func:`execute_edit_plan` ever runs — has the data, and again from
    :func:`execute_edit_plan` itself so a resumed run that hits apply
    directly still refreshes the field.

    For merged findings (``occurrence_originals`` non-empty), the
    locator is run against each per-file original using that file's
    paragraph map. The representative finding receives the evidence
    snapshot of its own file's original (matching :attr:`fileName`) so
    a single-line evidence panel still makes sense for the dedup'd
    entry, and each original carries its own per-file snapshot for
    callers that walk the originals list.

    Findings whose paragraph map is unavailable (file not in
    ``extracted_specs``, no paragraph map, or no edit proposal) are
    left with :attr:`locator_evidence` ``None`` so the report renders
    "no edit-target evidence available" rather than fabricating one.
    """
    spec_map_by_filename: dict[str, list] = {}
    for spec in extracted_specs:
        if spec.paragraph_map:
            spec_map_by_filename[spec.filename] = spec.paragraph_map

    if not spec_map_by_filename:
        return

    for finding in findings:
        if finding.as_edit_proposal() is None:
            continue
        originals_by_file = {
            orig.fileName: orig
            for orig in finding.occurrence_originals
            if orig.fileName
        }
        # Singletons: the representative IS the original.
        if not originals_by_file and finding.fileName:
            originals_by_file = {finding.fileName: finding}
        # Compute per-file locator evidence; pick the entry whose
        # filename matches the representative for the headline snapshot.
        rep_evidence: dict | None = None
        for file_name, original in originals_by_file.items():
            paragraph_map = spec_map_by_filename.get(file_name)
            if not paragraph_map:
                continue
            try:
                results = locate_edits([original], paragraph_map)
            except Exception:
                # Locator failures here are non-fatal — the report
                # falls back to "no locator evidence" rather than
                # blowing up the whole export.
                continue
            if not results:
                continue
            evidence = locator_evidence_from_result(results[0])
            original.locator_evidence = evidence
            if file_name == finding.fileName:
                rep_evidence = evidence
        if rep_evidence is None and originals_by_file:
            # Representative's own file was not in extracted_specs;
            # fall back to any computed evidence so the panel still
            # surfaces something.
            for orig in originals_by_file.values():
                if orig.locator_evidence is not None:
                    rep_evidence = orig.locator_evidence
                    break
        if rep_evidence is not None:
            finding.locator_evidence = rep_evidence
