"""Module registry, module-identity threading, and prompt-slot contracts.

Phase 1 introduced the registry + identity threading; Phase 2 moved the
prompt content (personas, severity anchors, categories, few-shot examples,
verifier source tiers) onto the module as validated slots. Covers:

1. **Registry** — ``ReviewModule`` is frozen; ``get_module`` resolves known
   ids and degrades unknown / missing ids to the default California module
   (mirroring ``AVAILABLE_CYCLES.get(label, DEFAULT_CYCLE)``); registry
   validation fails fast on duplicate ids, duplicate cycle labels (the
   verification-cache namespace rule), and empty fields.
2. **Persistence round-trip** — ``module_id`` rides
   ``BatchSubmission -> PendingBatch -> pending_batch.json -> to_submission``
   verbatim, and a LEGACY state file written before the field existed loads
   with the default module (no schema bump).
3. **Downstream stamping** — ``finalize_batch_result`` carries the
   submission's module id onto ``PipelineResult``; the trace recorder writes
   it into ``run.json``.
4. **The ``module_for_cycle`` bridge** — content layers still keyed by
   ``cycle=`` resolve their module through the registry's unique-label
   reverse lookup.
5. **Prompt-slot content validation** — registration rejects empty slots,
   templates that don't format, few-shot examples that the real parser
   would demote, and per-request element-id leakage into the cached prefix.
6. **Slots drive output** — a synthetic module's strings actually appear in
   the assembled reviewer / cross-check / verifier prompts (the byte-exact
   CA rendering is separately pinned by ``test_golden_domain_surfaces``).
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from src.batch.batch import BatchJob
from src.core.code_cycles import CALIFORNIA_2025, CodeCycle
from src.modules import (
    AVAILABLE_MODULES,
    CALIFORNIA_K12_MEP,
    DEFAULT_MODULE,
    ReviewModule,
    get_module,
    module_for_cycle,
    validate_module_registry,
)
from src.modules.base import _iter_json_objects
from src.orchestration.batch_resume import (
    PendingBatch,
    load_pending_batch,
    save_pending_batch,
)
from src.orchestration.pipeline import (
    BatchSubmission,
    CollectedBatchState,
    PipelineResult,
    finalize_batch_result,
)
from src.review.reviewer import ReviewResult
from src.tracing.recorder import TraceRecorder


def _cycle(label: str) -> CodeCycle:
    return CodeCycle(
        label=label,
        cbc=label,
        cmc=label,
        cpc=label,
        energy_code=label,
        calgreen=label,
        asce7="7-22",
        asce7_previous="7-16",
    )


_VALID_EXAMPLE_BLOCK = """\
Example 1 — REPORT_ONLY:
{
  "severity": "MEDIUM",
  "fileName": "a.docx",
  "section": "1.01",
  "issue": "Test issue.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "codeReference": null,
  "confidence": 0.5
}
"""


def _module(module_id: str = "test_module", label: str = "9999", **overrides) -> ReviewModule:
    slots = dict(
        module_id=module_id,
        display_name="Test Module",
        description="test",
        cycle=_cycle(label),
        reviewer_persona="You are a test reviewer.",
        review_user_intro="Review the following test document.",
        review_severity_definitions=(
            "CRITICAL — test.\nHIGH — test.\nMEDIUM — test.\nGRIPES — test."
        ),
        review_confidence_high_example="a test high-confidence example",
        review_categories_template="1. Internal contradictions within the spec.",
        review_examples=_VALID_EXAMPLE_BLOCK,
        cross_check_persona="You are a test coordination reviewer.",
        cross_check_severity_definitions=(
            "CRITICAL — test.\nHIGH — test.\nMEDIUM — test.\nGRIPES — test."
        ),
        verifier_persona="You are a test verification assistant.",
        verifier_source_priorities="1. Test sources:\n   example.gov",
    )
    slots.update(overrides)
    return ReviewModule(**slots)


def _submission(**overrides) -> BatchSubmission:
    job = BatchJob(
        batch_id="msgbatch_MODTEST",
        job_type="review",
        request_map={"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
        created_at=1700000000.0,
    )
    base = dict(
        job=job,
        files_reviewed=["a.docx"],
        review_request_ids=["review__a__0"],
        model="claude-opus-4-8",
    )
    base.update(overrides)
    return BatchSubmission(**base)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_default_module_is_california_k12_mep(self):
        assert DEFAULT_MODULE is CALIFORNIA_K12_MEP
        assert DEFAULT_MODULE.module_id == "california_k12_mep"
        assert DEFAULT_MODULE.cycle is CALIFORNIA_2025
        assert AVAILABLE_MODULES == {"california_k12_mep": CALIFORNIA_K12_MEP}

    def test_get_module_resolves_known_id(self):
        assert get_module("california_k12_mep") is CALIFORNIA_K12_MEP

    @pytest.mark.parametrize("bad_id", [None, "", "  ", "not_a_module"])
    def test_get_module_degrades_to_default(self, bad_id):
        assert get_module(bad_id) is DEFAULT_MODULE

    def test_get_module_strips_whitespace(self):
        assert get_module("  california_k12_mep  ") is CALIFORNIA_K12_MEP

    def test_review_module_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            CALIFORNIA_K12_MEP.module_id = "other"  # type: ignore[misc]


class TestRegistryValidation:
    def test_current_registry_validates(self):
        validate_module_registry(AVAILABLE_MODULES.values())

    def test_duplicate_module_id_rejected(self):
        with pytest.raises(ValueError, match="Duplicate module_id"):
            validate_module_registry(
                [_module("dup", "9998"), _module("dup", "9999")]
            )

    def test_duplicate_cycle_label_rejected(self):
        # Cycle labels namespace the verification cache AND back the
        # module_for_cycle bridge; two modules sharing one label would let
        # cached verdicts collide and make the bridge ambiguous.
        with pytest.raises(ValueError, match="Duplicate cycle label"):
            validate_module_registry(
                [_module("first", "9999"), _module("second", "9999")]
            )

    def test_empty_module_id_rejected(self):
        with pytest.raises(ValueError, match="module_id"):
            validate_module_registry([_module("", "9999")])

    def test_unstripped_module_id_rejected(self):
        with pytest.raises(ValueError, match="module_id"):
            validate_module_registry([_module(" padded ", "9999")])

    def test_empty_display_name_rejected(self):
        bad = dataclasses.replace(_module(), display_name="  ")
        with pytest.raises(ValueError, match="display_name"):
            validate_module_registry([bad])

    def test_missing_cycle_label_rejected(self):
        with pytest.raises(ValueError, match="cycle label"):
            validate_module_registry([_module("ok", "")])


# ---------------------------------------------------------------------------
# module_for_cycle bridge
# ---------------------------------------------------------------------------


class TestModuleForCycleBridge:
    def test_resolves_california_cycle(self):
        assert module_for_cycle(CALIFORNIA_2025) is CALIFORNIA_K12_MEP

    def test_unknown_label_degrades_to_default(self):
        assert module_for_cycle(_cycle("0000")) is DEFAULT_MODULE

    def test_label_less_input_degrades_to_default(self):
        assert module_for_cycle(None) is DEFAULT_MODULE
        assert module_for_cycle(object()) is DEFAULT_MODULE


# ---------------------------------------------------------------------------
# Prompt-slot content validation
# ---------------------------------------------------------------------------


class TestContentValidation:
    def test_empty_prompt_slot_rejected(self):
        bad = dataclasses.replace(_module(), reviewer_persona="   ")
        with pytest.raises(ValueError, match="reviewer_persona"):
            validate_module_registry([bad])

    def test_unknown_categories_placeholder_rejected(self):
        bad = dataclasses.replace(
            _module(), review_categories_template="1. Cycle {not_a_placeholder}."
        )
        with pytest.raises(ValueError, match="review_categories_template"):
            validate_module_registry([bad])

    def test_examples_without_json_rejected(self):
        bad = dataclasses.replace(_module(), review_examples="Prose only, no JSON.")
        with pytest.raises(ValueError, match="no parseable JSON"):
            validate_module_registry([bad])

    def test_example_element_id_leak_rejected(self):
        # Per-request concept inside the cached system-prompt prefix.
        bad = dataclasses.replace(
            _module(),
            review_examples=_VALID_EXAMPLE_BLOCK + "\nAlso set evidenceElementId.",
        )
        with pytest.raises(ValueError, match="evidenceElementId"):
            validate_module_registry([bad])

    def test_example_teaching_demotable_edit_rejected(self):
        # An EDIT without replacementText is demoted by the parser — an
        # example teaching that shape would waste model output every run.
        block = """\
Example — bad EDIT:
{
  "severity": "MEDIUM",
  "fileName": "a.docx",
  "section": "1.01",
  "issue": "Bad shape.",
  "actionType": "EDIT",
  "existingText": "old text",
  "replacementText": null,
  "codeReference": null,
  "confidence": 0.5
}
"""
        bad = dataclasses.replace(_module(), review_examples=block)
        with pytest.raises(ValueError, match="demoted"):
            validate_module_registry([bad])

    def test_example_noop_edit_rejected(self):
        # existingText byte-identical to replacementText is the no-op EDIT
        # shape the parser demotes (TRUST_AUDIT P1-1).
        block = """\
Example — no-op EDIT:
{
  "severity": "MEDIUM",
  "fileName": "a.docx",
  "section": "1.01",
  "issue": "No-op.",
  "actionType": "EDIT",
  "existingText": "same text",
  "replacementText": "same text",
  "codeReference": null,
  "confidence": 0.5
}
"""
        bad = dataclasses.replace(_module(), review_examples=block)
        with pytest.raises(ValueError, match="demoted"):
            validate_module_registry([bad])

    def test_example_bad_severity_rejected(self):
        block = _VALID_EXAMPLE_BLOCK.replace('"MEDIUM"', '"BLOCKER"')
        bad = dataclasses.replace(_module(), review_examples=block)
        with pytest.raises(ValueError, match="severity"):
            validate_module_registry([bad])

    def test_example_confidence_out_of_range_rejected(self):
        block = _VALID_EXAMPLE_BLOCK.replace('"confidence": 0.5', '"confidence": 1.5')
        bad = dataclasses.replace(_module(), review_examples=block)
        with pytest.raises(ValueError, match="confidence"):
            validate_module_registry([bad])

    def test_california_examples_carry_multiple_json_shapes(self):
        # The CA module registered successfully at import (so its examples
        # already passed the real contract); pin that the block actually
        # contains the three JSON teaching shapes.
        examples = list(_iter_json_objects(CALIFORNIA_K12_MEP.review_examples))
        assert len(examples) == 3
        assert [e["actionType"] for e in examples] == ["EDIT", "ADD", "REPORT_ONLY"]


# ---------------------------------------------------------------------------
# Slots drive the assembled prompts
# ---------------------------------------------------------------------------


class TestSlotsDriveOutput:
    def test_synthetic_module_slots_appear_in_prompts(self, monkeypatch):
        from src.cross_check.cross_checker import _cross_system_prompt
        from src.modules import registry as registry_mod
        from src.review.prompts import get_single_spec_user_message, get_system_prompt
        from src.verification.verifier import _get_verification_system_prompt

        mod = _module(
            "synthetic",
            "8888",
            reviewer_persona="You are a synthetic persona for slot testing.",
            review_user_intro="Synthetic intro line.",
            review_confidence_high_example="a synthetic high example",
            review_categories_template="1. Synthetic category for cycle {cbc}.",
            cross_check_persona="Synthetic cross-check persona.",
            verifier_persona="Synthetic verifier persona.",
            verifier_source_priorities="1. Synthetic sources:\n   synthetic.example",
        )
        monkeypatch.setitem(registry_mod._MODULES_BY_CYCLE_LABEL, "8888", mod)

        sp = get_system_prompt(mod.cycle)
        assert sp.startswith("You are a synthetic persona for slot testing.\n\n<task>")
        assert "1. Synthetic category for cycle 8888." in sp
        assert "(e.g., a synthetic high example)" in sp
        assert "Example 1 — REPORT_ONLY:" in sp

        um = get_single_spec_user_message(
            "body", "a.docx", "", cycle=mod.cycle,
            paragraph_map=None, pre_detected_alerts=None,
        )
        assert um.startswith("Synthetic intro line.\n\n")

        cp = _cross_system_prompt(mod.cycle)
        assert cp.startswith("Synthetic cross-check persona.\n\n")

        vp = _get_verification_system_prompt(mod.cycle, include_verdict_tool=True)
        assert "Synthetic verifier persona." in vp
        assert "   synthetic.example" in vp
        # Engine protocol stays regardless of module.
        assert "Prefer authoritative sources in this priority order:" in vp


# ---------------------------------------------------------------------------
# Persistence round-trip (submission -> pending state -> submission)
# ---------------------------------------------------------------------------


class TestModuleIdRoundTrip:
    def test_submission_defaults_carry_the_default_module(self):
        sub = _submission()
        assert sub.module_id == DEFAULT_MODULE.module_id
        assert PipelineResult(review_result=None).module_id == DEFAULT_MODULE.module_id

    def test_pending_batch_round_trips_module_id(self, tmp_path):
        state_path = tmp_path / "pending_batch.json"
        pending = PendingBatch.from_submission(_submission())
        assert pending.module_id == DEFAULT_MODULE.module_id

        save_pending_batch(pending, path=state_path)
        loaded = load_pending_batch(path=state_path)
        assert loaded is not None
        assert loaded.module_id == DEFAULT_MODULE.module_id

        # to_submission resolves the module and re-stamps identity. Files are
        # absent, so reconstruction is the findings-only path (no re-extract).
        sub = loaded.to_submission(log=lambda *a, **k: None)
        assert sub.module_id == DEFAULT_MODULE.module_id
        assert sub.cycle_label == DEFAULT_MODULE.cycle.label
        assert sub.job.request_map == {
            "review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}
        }

    def test_legacy_state_file_without_module_id_defaults(self, tmp_path):
        """A pending_batch.json written before Phase 1 has no module_id key.

        It must load (same schema version — the field was additive) and
        resolve to the default California module, the only configuration a
        legacy file could have been written by.
        """
        state_path = tmp_path / "pending_batch.json"
        legacy = {
            "batch_id": "msgbatch_LEGACY",
            "model": "claude-opus-4-8",
            "request_map": {},
            "review_request_ids": [],
            "files_reviewed": [],
            "input_dir": "",
            "files": [],
            "cycle_label": "2025",
            "project_context": "",
            "cross_check_enabled": False,
            "submitted_at": 1700000000.0,
            "run_id": "",
            "app_version": "2.9.0",
            "schema_version": 1,
        }
        state_path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = load_pending_batch(path=state_path)
        assert loaded is not None
        assert loaded.module_id == DEFAULT_MODULE.module_id

    def test_new_state_file_keeps_schema_version_1(self, tmp_path):
        # Additive field, defensive loader: old readers ignore the key, so
        # the schema version intentionally does NOT bump.
        state_path = tmp_path / "pending_batch.json"
        save_pending_batch(PendingBatch.from_submission(_submission()), path=state_path)
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["module_id"] == DEFAULT_MODULE.module_id


# ---------------------------------------------------------------------------
# Downstream stamping
# ---------------------------------------------------------------------------


class TestDownstreamStamping:
    def test_finalize_carries_module_id_onto_pipeline_result(self):
        state = CollectedBatchState(
            submission=_submission(),
            review_result=ReviewResult(findings=[]),
        )
        result = finalize_batch_result(state)
        assert result.module_id == DEFAULT_MODULE.module_id
        assert result.cycle_label == DEFAULT_MODULE.cycle.label

    def test_finalize_defaults_when_submission_lacks_the_field(self):
        class _BareJob:
            created_at = 1700000000.0

        class _BareSubmission:
            job = _BareJob()
            cycle_label = "2025"
            cross_check_enabled = False
            prepared_specs = None
            files_reviewed: list = []

        state = CollectedBatchState(
            submission=_BareSubmission(),  # type: ignore[arg-type]
            review_result=ReviewResult(findings=[]),
        )
        assert finalize_batch_result(state).module_id == DEFAULT_MODULE.module_id

    def test_trace_run_meta_records_module_id(self, tmp_path):
        trace_dir = tmp_path / "trace"
        rec = TraceRecorder(run_id="mod1", trace_dir=trace_dir, capture_level="default")
        rec.start(
            mode="batch",
            model="claude-opus-4-8",
            cycle_label="2025",
            module_id="california_k12_mep",
        )
        rec.stop()
        meta = json.loads((trace_dir / "run.json").read_text(encoding="utf-8"))
        assert meta["module_id"] == "california_k12_mep"
