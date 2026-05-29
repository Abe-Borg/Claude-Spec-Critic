"""Tests for separating report deduplication from executable edit identity.

Plan section "Separate report deduplication from executable edit
identity". This work preserves the per-file pre-merge edit fields when
:func:`pipeline._deduplicate_findings` collapses findings across files, so
edit execution does not fan one representative's ``existingText`` /
``replacementText`` / ``anchorText`` / ``evidenceElementId`` /
``edit_proposal`` across files that may have differed.

Acceptance scenarios from the plan:

1. Two files have the same semantic issue but different exact text. Grouping
   produces one display group, but the edit planner uses different original
   edit text per file.
2. The same file has two similar findings in different paragraphs. Edit
   planner keeps the locations separate.
3. Representative-only legacy payload (no per-file originals recorded) does
   not auto-apply across files — auto-edit fires only on the representative's
   own file; other affected files are routed to manual review.
4. Non-edit REPORT_ONLY grouped findings still display correctly.

Plus the back-compat surface: resume payloads round-trip
``occurrence_originals``; pre-Chunk-8 payloads load with the field empty.
"""

from __future__ import annotations

from src.orchestration.pipeline import (
    _deduplicate_findings,
    group_findings,
)
from src.review.reviewer import Finding, REPORT_ONLY_ACTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    file_name: str = "spec.docx",
    issue: str = "Code reference uses outdated CBC edition.",
    section: str = "2.1",
    severity: str = "HIGH",
    confidence: float = 0.8,
    action: str = "EDIT",
    existing: str | None = "per CBC 2019",
    replacement: str | None = "per CBC 2025",
    code_ref: str | None = "CBC 2025",
    anchor: str | None = None,
    insert_pos: str | None = None,
    evidence_id: str | None = None,
) -> Finding:
    return Finding(
        severity=severity,
        fileName=file_name,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=confidence,
        anchorText=anchor,
        insertPosition=insert_pos,
        evidenceElementId=evidence_id,
    )


# ---------------------------------------------------------------------------
# _deduplicate_findings — occurrence_originals capture
# ---------------------------------------------------------------------------


class TestDedupCapturesOriginals:
    def test_singleton_finding_has_empty_occurrence_originals(self):
        f = _make_finding(file_name="a.docx")
        out = _deduplicate_findings([f])
        assert len(out) == 1
        # Singleton: the finding IS its own original; no separate list needed.
        assert out[0].occurrence_originals == []

    def test_merged_multifile_finding_retains_one_original_per_file(self):
        # Two findings with identical dedup identity but different filenames
        # collapse into one merged representative — the originals are kept.
        a = _make_finding(file_name="a.docx", existing="per CBC 2019", replacement="per CBC 2025")
        b = _make_finding(file_name="b.docx", existing="per CBC 2019", replacement="per CBC 2025")
        out = _deduplicate_findings([a, b])
        assert len(out) == 1
        merged = out[0]
        assert sorted(merged.affected_files) == ["a.docx", "b.docx"]
        assert len(merged.occurrence_originals) == 2
        files_in_originals = sorted(o.fileName for o in merged.occurrence_originals)
        assert files_in_originals == ["a.docx", "b.docx"]
        # No recursive nesting — members are leaves (folded in from the
        # former test_originals_themselves_have_empty_occurrence_originals).
        for orig in merged.occurrence_originals:
            assert orig.occurrence_originals == []

    def test_originals_preserve_per_file_edit_text(self):
        # Same dedup key, but each original carries its own text. The merged
        # representative's text is the rep's; per-file text lives on originals.
        a = _make_finding(file_name="a.docx", existing="per CBC 2019", replacement="per CBC 2025")
        b = _make_finding(file_name="b.docx", existing="per CBC 2019", replacement="per CBC 2025")
        # Tweak b's anchor to verify per-file fields are not overwritten.
        b.anchorText = "near top of section"
        out = _deduplicate_findings([a, b])
        merged = out[0]
        by_file = {o.fileName: o for o in merged.occurrence_originals}
        assert by_file["a.docx"].anchorText is None
        assert by_file["b.docx"].anchorText == "near top of section"

    def test_case_only_existing_text_difference_preserves_per_file_originals(self):
        """Phase 4 / Step 4.2 regression: case/whitespace-only differences in
        ``existingText`` collapse into one dedup group, but each per-file
        original retains its UNMODIFIED text.

        The dedup key normalizes ``existingText`` and ``replacementText``
        via ``_normalized_text_digest`` (lowercase + strip), so two
        findings whose existing text differs only in case or trailing
        whitespace land in the same group. The premise that triggered
        this test (P9 from the auto-apply quality plan) was that the
        per-file text for the dedup loser would be lost; in practice
        ``_deduplicate_findings`` keeps every group member in
        ``occurrence_originals`` as the original ``Finding`` object,
        which still carries its pre-normalization text. This regression
        test locks in that behavior so a future refactor cannot silently
        re-introduce the bug the plan worried about.
        """
        # Case-only difference in existing text.
        a = _make_finding(
            file_name="a.docx",
            existing="Per CBC 2019",
            replacement="Per CBC 2025",
        )
        b = _make_finding(
            file_name="b.docx",
            existing="per cbc 2019",
            replacement="per cbc 2025",
        )
        # Trailing-whitespace-only difference layered on top.
        c = _make_finding(
            file_name="c.docx",
            existing="per CBC 2019   ",
            replacement="per CBC 2025\n",
        )

        out = _deduplicate_findings([a, b, c])

        # All three collapse to one representative (same dedup key after
        # normalization).
        assert len(out) == 1
        merged = out[0]
        assert sorted(merged.affected_files) == ["a.docx", "b.docx", "c.docx"]

        # Every per-file original is preserved with its ORIGINAL text
        # intact — not the normalized form, not the representative's
        # form. Edit execution can rely on each file's own text.
        by_file = {o.fileName: o for o in merged.occurrence_originals}
        assert by_file["a.docx"].existingText == "Per CBC 2019"
        assert by_file["a.docx"].replacementText == "Per CBC 2025"
        assert by_file["b.docx"].existingText == "per cbc 2019"
        assert by_file["b.docx"].replacementText == "per cbc 2025"
        assert by_file["c.docx"].existingText == "per CBC 2019   "
        assert by_file["c.docx"].replacementText == "per CBC 2025\n"


# ---------------------------------------------------------------------------
# group_findings — FindingOccurrence binds per-file original
# ---------------------------------------------------------------------------


class TestGroupFindingsPerFileOriginal:
    def test_singleton_occurrence_binds_representative_as_original(self):
        f = _make_finding(file_name="A.docx")
        groups = group_findings([f])
        assert len(groups) == 1
        occ = groups[0].occurrences[0]
        # Singleton: the representative is the only original (backfill
        # so executable_finding() returns the right thing).
        assert occ.has_original()
        assert occ.original_finding is f
        assert occ.executable_finding() is f

    def test_merged_finding_binds_each_file_to_its_own_original(self):
        a = _make_finding(file_name="a.docx", existing="per CBC 2019", replacement="per CBC 2025")
        b = _make_finding(file_name="b.docx", existing="per CBC 2019", replacement="per CBC 2025")
        merged = _deduplicate_findings([a, b])[0]
        groups = group_findings([merged])
        occs_by_file = {o.file_name: o for o in groups[0].occurrences}
        # Each file's occurrence binds to the original that came from that file.
        assert occs_by_file["a.docx"].executable_finding().fileName == "a.docx"
        assert occs_by_file["b.docx"].executable_finding().fileName == "b.docx"
        # The representative is shared (display layer) — but the executable
        # finding is the per-file original, not the representative.
        assert occs_by_file["a.docx"].finding is merged
        assert occs_by_file["b.docx"].finding is merged
        assert occs_by_file["a.docx"].executable_finding() is not merged
        assert occs_by_file["b.docx"].executable_finding() is not merged

    def test_legacy_multifile_finding_without_originals_marks_missing(self):
        # Pre-Chunk-8 payload: a finding with multiple affected files but no
        # ``occurrence_originals``. The representative's own file binds to the
        # representative; other affected files have no original (manual
        # review path in apply_edits).
        f = _make_finding(file_name="a.docx")
        f.affected_files = ["a.docx", "b.docx"]
        # ``occurrence_originals`` left empty.
        groups = group_findings([f])
        occs_by_file = {o.file_name: o for o in groups[0].occurrences}
        assert occs_by_file["a.docx"].has_original()
        assert occs_by_file["a.docx"].original_finding is f
        assert not occs_by_file["b.docx"].has_original()
        assert occs_by_file["b.docx"].executable_finding() is f  # fallback only


# ---------------------------------------------------------------------------
# Same-file findings in different paragraphs stay separate
# ---------------------------------------------------------------------------


class TestSameFileDifferentParagraphsStaySeparate:
    def test_two_findings_same_file_different_text_not_merged(self):
        # Acceptance scenario 2: same file, similar issue text, different
        # exact existingText — must not be collapsed by dedup (so the edit
        # planner keeps the locations separate).
        f1 = _make_finding(
            file_name="a.docx",
            issue="Outdated code edition reference.",
            existing="per CBC 2019",
            replacement="per CBC 2025",
        )
        f2 = _make_finding(
            file_name="a.docx",
            issue="Outdated code edition reference.",
            existing="per CMC 2019",
            replacement="per CMC 2025",
        )
        out = _deduplicate_findings([f1, f2])
        # Different existingText → different dedup key → not merged.
        assert len(out) == 2
        assert {f.existingText for f in out} == {"per CBC 2019", "per CMC 2019"}


# ---------------------------------------------------------------------------
# REPORT_ONLY grouped findings still display correctly
# ---------------------------------------------------------------------------


class TestReportOnlyGroupedFindings:
    def test_report_only_finding_merges_and_stays_report_only(self):
        # Acceptance scenario 4: REPORT_ONLY findings can group across files
        # without rehydrating an edit slot. The merged representative stays
        # REPORT_ONLY, the per-file originals stay REPORT_ONLY, no edit
        # proposal materializes.
        a = _make_finding(
            file_name="a.docx",
            issue="Coordination conflict — sleeve sizing not coordinated with structural.",
            action=REPORT_ONLY_ACTION,
            existing=None,
            replacement=None,
        )
        b = _make_finding(
            file_name="b.docx",
            issue="Coordination conflict — sleeve sizing not coordinated with structural.",
            action=REPORT_ONLY_ACTION,
            existing=None,
            replacement=None,
        )
        out = _deduplicate_findings([a, b])
        assert len(out) == 1
        merged = out[0]
        assert merged.actionType == REPORT_ONLY_ACTION
        assert merged.as_edit_proposal() is None
        assert sorted(merged.affected_files) == ["a.docx", "b.docx"]
        assert len(merged.occurrence_originals) == 2
        for orig in merged.occurrence_originals:
            assert orig.actionType == REPORT_ONLY_ACTION
            assert orig.as_edit_proposal() is None
