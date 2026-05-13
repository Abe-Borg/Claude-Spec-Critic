"""Chunk M tests: cross-check dependency tracking.

Plan section "Chunk M — Cross-Check Dependency Tracking". The chunk replaces
the prior file+section overlap heuristic with explicit ID-based dependency
tracking so post-verification suppression is deterministic and reports can
explain *why* a coordination finding was kept or dropped.

The chunk has five moving pieces:

* M-Schema  — cross-check tool schema gains ``upstreamFindingIds`` and
  ``independentEvidenceIds`` (required arrays, possibly empty).
* M-Parser  — :func:`_parse_findings` populates the new
  :class:`Finding.upstream_finding_ids` and
  :class:`Finding.independent_evidence_ids` lists.
* M-IDs     — :func:`pipeline.compute_finding_id` is deterministic and
  :func:`pipeline._deduplicate_findings` stamps it on review findings.
* M-Prompt  — :func:`cross_checker._build_cross_check_input` renders the
  ``id`` attribute on each ``<prior>`` block and the cross-check system
  prompt instructs the model on how to cite ids.
* M-Filter  — :func:`pipeline.classify_cross_check_dependencies` partitions
  cross-check findings into kept/suppressed based on cited upstream ids,
  falling back to the heuristic only when the model emitted no ids.
* M-Resume  — the new ``suppressed_findings`` list and per-finding fields
  round-trip through :mod:`resume_state`.

The four directive-9 acceptance scenarios from the plan are covered by
:class:`TestSuppressionFilter`:

* dependency only on a disputed upstream → suppressed,
* multiple dependencies, at least one supported → kept,
* finding with direct raw-spec evidence independent of upstream → kept,
* no cited ids → fallback to the prior (file, section) heuristic.
"""

from __future__ import annotations

import json

import pytest

from src.cross_checker import _build_cross_check_input, _cross_system_prompt
from src.code_cycles import DEFAULT_CYCLE
from src.extractor import ExtractedSpec
from src.pipeline import (
    _deduplicate_findings,
    classify_cross_check_dependencies,
    compute_finding_id,
)
from src.resume_state import (
    deserialize_finding,
    deserialize_review_result,
    serialize_finding,
    serialize_review_result,
)
from src.reviewer import Finding, ReviewResult, _parse_findings
from src.structured_schemas import (
    CROSS_CHECK_FINDINGS_SCHEMA,
    REVIEW_FINDINGS_SCHEMA,
    _CROSS_CHECK_FINDING_OBJECT_SCHEMA,
    _FINDING_OBJECT_SCHEMA,
)
from src.verifier import VerificationResult




def _review_finding(
    *,
    file: str = "A.docx",
    section: str = "2.1",
    issue: str = "Stale code reference",
    verdict: str | None = None,
    code_ref: str | None = "CBC §1234",
    severity: str = "HIGH",
) -> Finding:
    f = Finding(
        severity=severity,
        fileName=file,
        section=section,
        issue=issue,
        actionType="EDIT",
        existingText="old",
        replacementText="new",
        codeReference=code_ref,
        confidence=0.7,
    )
    if verdict is not None:
        f.verification = VerificationResult(verdict=verdict, explanation="")
    return f


def _cross_finding(
    *,
    file: str = "A.docx",
    section: str = "2.1",
    issue: str = "Cross-spec coordination problem",
    upstream_ids: list[str] | None = None,
    independent_ids: list[str] | None = None,
) -> Finding:
    return Finding(
        severity="HIGH",
        fileName=file,
        section=section,
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.6,
        upstream_finding_ids=list(upstream_ids or []),
        independent_evidence_ids=list(independent_ids or []),
    )




class TestCrossCheckSchemaFields:
    def test_cross_check_schema_declares_upstream_and_evidence_arrays(self):
        """Chunk M directive 3: schema can cite upstream + evidence ids."""
        props = _CROSS_CHECK_FINDING_OBJECT_SCHEMA["properties"]
        assert "upstreamFindingIds" in props
        assert "independentEvidenceIds" in props
        for key in ("upstreamFindingIds", "independentEvidenceIds"):
            assert props[key]["type"] == "array"
            assert props[key]["items"] == {"type": "string"}

    def test_cross_check_schema_requires_both_fields_for_strict_sampling(self):
        """Strict-mode constrained sampling needs every field in ``required``
        so the model produces a deterministic shape. Both new fields must
        be in the required list (empty arrays are valid)."""
        required = _CROSS_CHECK_FINDING_OBJECT_SCHEMA["required"]
        assert "upstreamFindingIds" in required
        assert "independentEvidenceIds" in required

    def test_review_schema_unchanged_by_chunk_m(self):
        """Chunk M scope is cross-check only. Review findings should not
        carry the upstream/evidence-id fields — adding them would clutter
        every per-spec review with unused slots."""
        props = _FINDING_OBJECT_SCHEMA["properties"]
        assert "upstreamFindingIds" not in props
        assert "independentEvidenceIds" not in props

    def test_cross_check_schema_preserves_shared_finding_fields(self):
        """Splitting the schema should not drop the shared fields. Spot-
        check that the chunk-K3 evidenceElementId and chunk-L actionType
        are still present so the cross-check tool stays compatible with
        the parser and the rest of the pipeline."""
        props = _CROSS_CHECK_FINDING_OBJECT_SCHEMA["properties"]
        assert "evidenceElementId" in props
        assert props["actionType"]["enum"] == ["ADD", "EDIT", "DELETE", "REPORT_ONLY"]

    def test_cross_check_findings_schema_wires_to_cross_check_item_schema(self):
        """The CROSS_CHECK_FINDINGS_SCHEMA wrapper should use the chunk-M
        finding schema, not the shared one — otherwise the tool would be
        sent without the upstream/evidence-id slots the model needs to fill."""
        assert (
            CROSS_CHECK_FINDINGS_SCHEMA["properties"]["findings"]["items"]
            is _CROSS_CHECK_FINDING_OBJECT_SCHEMA
        )
        assert (
            REVIEW_FINDINGS_SCHEMA["properties"]["findings"]["items"]
            is _FINDING_OBJECT_SCHEMA
        )




class TestParserAcceptsDependencyFields:
    def test_parser_extracts_upstream_and_independent_ids(self):
        """A cross-check-style payload (lists of strings on the new fields)
        round-trips through ``_parse_findings`` into the dataclass slots."""
        payload = [
            {
                "severity": "HIGH",
                "fileName": "A.docx",
                "section": "2.1",
                "issue": "Cross-spec contradiction",
                "actionType": "REPORT_ONLY",
                "existingText": None,
                "replacementText": None,
                "codeReference": None,
                "confidence": 0.7,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": None,
                "upstreamFindingIds": ["rf-aaa111", "rf-bbb222"],
                "independentEvidenceIds": ["p7", "t1r2"],
            }
        ]
        parsed = _parse_findings(payload)
        assert len(parsed) == 1
        f = parsed[0]
        assert f.upstream_finding_ids == ["rf-aaa111", "rf-bbb222"]
        assert f.independent_evidence_ids == ["p7", "t1r2"]

    def test_parser_normalizes_empty_strings_and_missing_fields(self):
        """Empty strings inside the arrays should be dropped (the schema
        permits them but they have no meaning); a missing field should
        default to an empty list so review-side payloads still parse."""
        payload = [
            {
                "severity": "HIGH",
                "fileName": "A.docx",
                "section": "2.1",
                "issue": "X",
                "actionType": "REPORT_ONLY",
                "existingText": None,
                "replacementText": None,
                "codeReference": None,
                "confidence": 0.5,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": None,
                "upstreamFindingIds": ["", "  ", "rf-real"],
            }
        ]
        f = _parse_findings(payload)[0]
        assert f.upstream_finding_ids == ["rf-real"]
        assert f.independent_evidence_ids == []

    def test_parser_tolerates_non_list_dependency_fields(self):
        """A malformed payload (string instead of list) should not crash —
        the parser is robust to fallback / hand-rolled tagged-JSON shapes
        and the new fields just default to empty when malformed."""
        payload = [
            {
                "severity": "HIGH",
                "fileName": "A.docx",
                "section": "2.1",
                "issue": "X",
                "actionType": "REPORT_ONLY",
                "existingText": None,
                "replacementText": None,
                "codeReference": None,
                "confidence": 0.5,
                "anchorText": None,
                "insertPosition": None,
                "evidenceElementId": None,
                "upstreamFindingIds": "rf-not-a-list",
                "independentEvidenceIds": None,
            }
        ]
        f = _parse_findings(payload)[0]
        assert f.upstream_finding_ids == []
        assert f.independent_evidence_ids == []




class TestComputeFindingId:
    def test_id_is_deterministic_across_calls(self):
        """The id is derived from the content via the dedup key, so the
        same finding must produce the same id every call. This is what
        makes the cross-check pass able to reproduce the id later."""
        f1 = _review_finding(file="A.docx", section="2.1", issue="X")
        f2 = _review_finding(file="A.docx", section="2.1", issue="X")
        assert compute_finding_id(f1) == compute_finding_id(f2)

    def test_id_diverges_when_dedup_key_changes(self):
        """Two findings that differ on a dedup-key field must get different
        ids — otherwise the cross-check model could not distinguish them."""
        a = _review_finding(section="2.1")
        b = _review_finding(section="2.2")
        assert compute_finding_id(a) != compute_finding_id(b)

    def test_id_has_stable_human_debuggable_prefix(self):
        """Plan directive 4 (chunk K): IDs should be deterministic within
        a run and human-debuggable. The ``rf-`` prefix tags review findings
        so a stray id in a transcript is obviously a review-finding id."""
        fid = compute_finding_id(_review_finding())
        assert fid.startswith("rf-")
        assert len(fid) == len("rf-") + 12

    def test_dedup_stamps_finding_id_on_singleton(self):
        """Single-occurrence findings still need an id (cross-check may
        depend on them just like deduped ones do)."""
        f = _review_finding(issue="Solo finding", section="3.0")
        deduped = _deduplicate_findings([f])
        assert len(deduped) == 1
        assert deduped[0].finding_id
        assert deduped[0].finding_id == compute_finding_id(f)

    def test_dedup_stamps_finding_id_on_merged_group(self):
        """When two findings dedupe into one, the merged finding inherits
        the representative's id so cross-check ids remain stable across
        whether the issue happened to fire on one spec or many."""
        a = _review_finding(file="A.docx", section="2.1")
        b = _review_finding(file="B.docx", section="2.1")
        deduped = _deduplicate_findings([a, b])
        assert len(deduped) == 1
        assert deduped[0].finding_id == compute_finding_id(a)

    def test_dedup_preserves_existing_finding_id(self):
        """Findings that already carry an id (e.g. from a previous run or
        a resumed session) should keep it rather than being re-stamped."""
        f = _review_finding()
        f.finding_id = "rf-preserved"
        out = _deduplicate_findings([f])
        assert out[0].finding_id == "rf-preserved"




class TestCrossCheckPromptRendersIds:
    def test_prior_block_includes_finding_id_attribute(self):
        """The cross-check pass needs to see review-finding ids in the
        ``<prior>`` blocks so it can cite them via ``upstreamFindingIds``."""
        review = _review_finding(file="A.docx", section="2.1", issue="X")
        review.finding_id = "rf-test123abc"
        specs = [
            ExtractedSpec(filename="A.docx", content="body", word_count=1),
            ExtractedSpec(filename="B.docx", content="body", word_count=1),
        ]
        rendered = _build_cross_check_input(specs, [review])
        assert 'id="rf-test123abc"' in rendered
        assert "<prior" in rendered

    def test_prior_block_includes_section_attribute(self):
        """Section is a useful debugging hint when the model is deciding
        which prior finding to depend on — even without ids it should
        appear so a human reading the prompt can sanity-check the mapping."""
        review = _review_finding(file="A.docx", section="2.1")
        review.finding_id = "rf-abc"
        specs = [
            ExtractedSpec(filename="A.docx", content="body", word_count=1),
            ExtractedSpec(filename="B.docx", content="body", word_count=1),
        ]
        rendered = _build_cross_check_input(specs, [review])
        assert 'section="2.1"' in rendered

    def test_prior_block_without_finding_id_still_renders(self):
        """Pre-Chunk-M findings (no ``finding_id``) should still appear in
        the ``<prior>`` block so the legacy / heuristic-fallback path keeps
        working. The id attribute is simply omitted."""
        review = _review_finding(file="A.docx", section="2.1")
        review.finding_id = ""
        specs = [
            ExtractedSpec(filename="A.docx", content="body", word_count=1),
            ExtractedSpec(filename="B.docx", content="body", word_count=1),
        ]
        rendered = _build_cross_check_input(specs, [review])
        assert "<prior" in rendered
        assert 'id="' not in rendered

    def test_system_prompt_documents_dependency_tracking_section(self):
        """The model has to know what to put in the new fields. The system
        prompt should mention both ``upstreamFindingIds`` and
        ``independentEvidenceIds`` so the schema doesn't surprise it."""
        prompt = _cross_system_prompt(DEFAULT_CYCLE)
        assert "upstreamFindingIds" in prompt
        assert "independentEvidenceIds" in prompt
        assert "<dependency_tracking>" in prompt




class TestSuppressionFilter:
    """Directive 9 acceptance scenarios from the plan."""

    def test_finding_with_only_disputed_upstream_is_suppressed(self):
        """Plan directive 9 case 1: cross-check finding depends only on a
        disputed upstream finding. The ID-based filter drops it (and tags
        the suppression reason so the report can explain)."""
        upstream = _review_finding(verdict="DISPUTED")
        upstream.finding_id = "rf-disputed-1"
        cross = _cross_finding(upstream_ids=["rf-disputed-1"])

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [upstream],
        )
        assert kept == []
        assert len(suppressed) == 1
        assert "rf-disputed-1" in (suppressed[0].suppression_reason or "")

    def test_finding_with_mixed_upstream_is_kept(self):
        """Plan directive 9 case 2: multiple upstream dependencies, one
        disputed and one supported. The cross-check finding survives
        because at least one upstream still stands."""
        bad = _review_finding(file="A.docx", section="2.1", verdict="DISPUTED")
        bad.finding_id = "rf-bad"
        good = _review_finding(
            file="B.docx", section="3.0", issue="other claim", verdict="CONFIRMED",
        )
        good.finding_id = "rf-good"
        cross = _cross_finding(upstream_ids=["rf-bad", "rf-good"])

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [bad, good],
        )
        assert kept == [cross]
        assert suppressed == []

    def test_finding_with_independent_evidence_survives_all_disputed_upstream(self):
        """Plan directive 9 case 3: cross-check finding has independent
        raw-spec evidence (paragraph / cell ids). It must survive even
        when every cited upstream is disputed."""
        upstream = _review_finding(verdict="DISPUTED")
        upstream.finding_id = "rf-disputed"
        cross = _cross_finding(
            upstream_ids=["rf-disputed"],
            independent_ids=["p17", "t2r3"],
        )

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [upstream],
        )
        assert kept == [cross]
        assert suppressed == []

    def test_finding_without_cited_ids_falls_back_to_heuristic(self):
        """Pre-Chunk-M / fallback-parser case: no upstream ids on the
        cross-check finding. The classifier falls back to (file, section)
        overlap and labels the suppression so the report can show the
        decision was made on weaker evidence."""
        upstream = _review_finding(file="A.docx", section="2.1", verdict="DISPUTED")
        upstream.finding_id = ""
        cross = _cross_finding(file="A.docx", section="2.1")

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [upstream],
        )
        assert kept == []
        assert len(suppressed) == 1
        reason = suppressed[0].suppression_reason or ""
        assert "heuristic" in reason.lower() or "fallback" in reason.lower()

    def test_finding_with_no_dependency_is_always_kept(self):
        """A cross-check finding that has neither upstream ids nor a
        (file, section) overlap with a disputed review finding is kept —
        the conservative default since we can't prove it depends on
        anything that fell."""
        upstream = _review_finding(
            file="A.docx", section="2.1", verdict="DISPUTED",
        )
        upstream.finding_id = "rf-disputed"
        cross = _cross_finding(file="Z.docx", section="9.9")

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [upstream],
        )
        assert kept == [cross]
        assert suppressed == []

    def test_unknown_cited_id_is_treated_as_non_disputed(self):
        """If the model cites an id that does not exist in the review
        result (e.g. a stale resume payload, a hallucinated id), the
        classifier should treat the dependency as 'not known to be
        disputed' and keep the finding — better than silently dropping
        a coordination claim because of an id typo."""
        upstream = _review_finding(verdict="CONFIRMED")
        upstream.finding_id = "rf-real"
        cross = _cross_finding(upstream_ids=["rf-hallucinated"])

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [upstream],
        )
        assert kept == [cross]
        assert suppressed == []

    def test_classifier_logs_id_and_fallback_paths_separately(self):
        """Operators want to see which suppression path made each
        decision. Two log lines are emitted when both paths fire so a
        future ID rollout shows up clearly when the fallback count drops
        toward zero."""
        id_upstream = _review_finding(
            file="A.docx", section="2.1", verdict="DISPUTED",
        )
        id_upstream.finding_id = "rf-id-path"
        id_cross = _cross_finding(upstream_ids=["rf-id-path"])
        fb_upstream = _review_finding(
            file="B.docx", section="3.0", verdict="DISPUTED",
        )
        fb_upstream.finding_id = ""
        fb_cross = _cross_finding(file="B.docx", section="3.0")

        log_messages: list[tuple[str, str]] = []

        def log(msg: str, *, level: str = "info") -> None:
            log_messages.append((level, msg))

        kept, suppressed = classify_cross_check_dependencies(
            [id_cross, fb_cross], [id_upstream, fb_upstream], log=log,
        )
        assert kept == []
        assert len(suppressed) == 2
        warning_msgs = [m for lvl, m in log_messages if lvl == "warning"]
        assert any("id-based" in m for m in warning_msgs)
        assert any("heuristic" in m.lower() for m in warning_msgs)




class TestResumeRoundTrip:
    def test_finding_id_round_trips(self):
        f = _review_finding()
        f.finding_id = "rf-roundtrip"
        loaded = deserialize_finding(serialize_finding(f))
        assert loaded.finding_id == "rf-roundtrip"

    def test_upstream_and_independent_ids_round_trip(self):
        cross = _cross_finding(
            upstream_ids=["rf-a", "rf-b"],
            independent_ids=["p5", "t0r1"],
        )
        loaded = deserialize_finding(serialize_finding(cross))
        assert loaded.upstream_finding_ids == ["rf-a", "rf-b"]
        assert loaded.independent_evidence_ids == ["p5", "t0r1"]

    def test_suppression_reason_round_trips(self):
        f = _cross_finding(upstream_ids=["rf-x"])
        f.suppression_reason = "Test reason"
        loaded = deserialize_finding(serialize_finding(f))
        assert loaded.suppression_reason == "Test reason"

    def test_legacy_payload_without_chunk_m_fields_loads_cleanly(self):
        """Pre-Chunk-M resume payloads lack the new fields. The
        deserializer must default them so a session resumed from before
        the upgrade keeps working under the heuristic-fallback path."""
        legacy_payload = {
            "severity": "HIGH",
            "fileName": "A.docx",
            "section": "2.1",
            "issue": "Legacy finding",
            "actionType": "EDIT",
            "existingText": "old",
            "replacementText": "new",
            "codeReference": "CBC §1",
            "confidence": 0.7,
            "affected_files": ["A.docx"],
        }
        loaded = deserialize_finding(legacy_payload)
        assert loaded.finding_id == ""
        assert loaded.upstream_finding_ids == []
        assert loaded.independent_evidence_ids == []
        assert loaded.suppression_reason is None

    def test_review_result_round_trips_suppressed_findings(self):
        """``ReviewResult.suppressed_findings`` carries the report's
        explanation of which coordination claims were dropped. It must
        survive a resume so the report a resumed session generates looks
        the same as the report the original would have generated."""
        dropped = _cross_finding(upstream_ids=["rf-x"])
        dropped.suppression_reason = "All upstream DISPUTED"
        result = ReviewResult(
            findings=[],
            cross_check_status="completed",
            suppressed_findings=[dropped],
        )
        payload = serialize_review_result(result)
        assert payload is not None
        loaded = deserialize_review_result(json.loads(json.dumps(payload)))
        assert loaded is not None
        assert len(loaded.suppressed_findings) == 1
        assert loaded.suppressed_findings[0].suppression_reason == "All upstream DISPUTED"
        assert loaded.suppressed_findings[0].upstream_finding_ids == ["rf-x"]




class TestPipelineWiring:
    def test_dedup_to_cross_check_to_filter_id_path_end_to_end(self):
        """Walk through the moving parts: dedup stamps an id on a review
        finding, cross-check finding cites that id, classifier drops the
        cross-check finding when the upstream becomes DISPUTED. This is
        the happy path the chunk is built to enable."""
        review = _review_finding(file="A.docx", section="2.1", issue="Stale")
        review_deduped = _deduplicate_findings([review])[0]
        assert review_deduped.finding_id

        cross = _cross_finding(upstream_ids=[review_deduped.finding_id])

        kept, suppressed = classify_cross_check_dependencies(
            [cross], [review_deduped],
        )
        assert kept == [cross]
        assert suppressed == []

        review_deduped.verification = VerificationResult(
            verdict="DISPUTED", explanation="",
        )
        kept, suppressed = classify_cross_check_dependencies(
            [cross], [review_deduped],
        )
        assert kept == []
        assert len(suppressed) == 1
        assert review_deduped.finding_id in (suppressed[0].suppression_reason or "")
