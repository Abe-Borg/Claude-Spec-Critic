"""Chunk 11 tests — cache-aware prompt tightening.

The chunk adds two narrow improvements to the review prompt while preserving
the cache-prefix invariant pinned by earlier chunks:

* **System prompt** — an ``<examples>`` block carrying 3–5 compact reference
  findings (EDIT, ADD, REPORT_ONLY, plus a negative "do not report"
  example). The examples are part of the cacheable system prompt because
  they do not vary per spec. They MUST NOT mention ``evidenceElementId``
  or ``<para id="…">`` — those are per-request concepts and the Chunk K
  test ``test_system_prompt_unchanged_after_chunk_k`` pins the rule.

* **User message** — a short ``<final_task>`` block appended *after* the
  spec body (and after ``<pre_detected>`` when alerts fire) reminding the
  model to review only the document above, submit findings once, drop
  unsupported findings, keep edit fields consistent with ``actionType``,
  and avoid duplicating pre-detected alerts. The "cite evidence element
  ids" line is conditional on the Chunk K2 id-rendering path being
  active, so the legacy / element-ids-off path stays byte-stable for
  ``evidenceElementId``.

The chunk explicitly preserves prompt-cache breakpoints — every test in
this file that adds a new assertion also re-validates the existing
prefix invariants from Chunk G / Chunk K2 / Chunk D4.1.
"""
from __future__ import annotations

import pytest

from src.code_cycles import CALIFORNIA_2025
from src.extractor import ParagraphMapping
from src.prompts import get_single_spec_user_message, get_system_prompt
from src.review_modes import ReviewMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paragraph_map() -> list[ParagraphMapping]:
    """Minimal paragraph map that activates the Chunk K2 id-rendering path."""
    return [
        ParagraphMapping(
            body_index=0,
            element_type="paragraph",
            text="PART 1 - GENERAL",
            table_index=None,
            row_index=None,
            cell_index=None,
            element_id="p0",
            section_id="",
        ),
        ParagraphMapping(
            body_index=1,
            element_type="paragraph",
            text="A. Comply with the current CBC.",
            table_index=None,
            row_index=None,
            cell_index=None,
            element_id="p1",
            section_id="PART 1 - GENERAL",
        ),
    ]


# ---------------------------------------------------------------------------
# 1. System prompt: <examples> block lives in the stable, cacheable area.
# ---------------------------------------------------------------------------


class TestSystemPromptExamples:
    def test_examples_block_is_present(self) -> None:
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        # The block sits in the system prompt so it is cached, not paid
        # for per spec.
        assert "<examples>" in prompt
        assert "</examples>" in prompt

    def test_examples_block_appears_in_every_review_mode(self) -> None:
        # Every mode reuses the same example shapes — the examples are
        # about the *schema*, not the scope. If a future mode skips them
        # we lose a cacheable consistency point.
        for mode in (
            ReviewMode.STRICT,
            ReviewMode.COMPREHENSIVE,
            ReviewMode.SAFE_EDIT,
        ):
            prompt = get_system_prompt(CALIFORNIA_2025, mode=mode)
            assert "<examples>" in prompt, mode
            assert "</examples>" in prompt, mode

    def test_examples_appear_between_output_and_review_scope(self) -> None:
        # Ordering matters for cache stability: ``<output>`` documents the
        # tool contract, ``<examples>`` makes that contract concrete, then
        # ``<review_scope>`` constrains what the model is allowed to
        # report. Reshuffling the blocks would re-cost the whole cache
        # entry on first call.
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        out_close = prompt.index("</output>")
        ex_open = prompt.index("<examples>")
        ex_close = prompt.index("</examples>")
        scope_open = prompt.index("<review_scope>")
        assert out_close < ex_open < ex_close < scope_open

    def test_all_required_example_kinds_are_covered(self) -> None:
        # The plan lists EDIT / ADD / REPORT_ONLY / "do not report" plus
        # an optional stale-code-cycle example. We satisfy stale-cycle by
        # folding it into the EDIT example so the prompt stays compact.
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        # actionType labels for the three executable forms.
        assert '"actionType": "EDIT"' in prompt
        assert '"actionType": "ADD"' in prompt
        assert '"actionType": "REPORT_ONLY"' in prompt
        # Negative example: explicitly tells the model NOT to report
        # generic boilerplate.
        assert "DO NOT REPORT" in prompt
        # The stale-code-cycle case is the EDIT example.
        assert "superseded California Building Code" in prompt

    def test_add_example_shows_anchor_and_insert_position(self) -> None:
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        # The ADD example must demonstrate both the anchor text and the
        # before/after insert position — that's the whole point of
        # showing it.
        assert '"anchorText": "PART 1 - GENERAL"' in prompt
        assert '"insertPosition": "after"' in prompt

    def test_report_only_example_nulls_executable_fields(self) -> None:
        # REPORT_ONLY findings carry no executable text. The example
        # demonstrates the contract so the model does not stuff edit
        # fields with apology text.
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        idx = prompt.index('"actionType": "REPORT_ONLY"')
        report_only_block = prompt[idx : idx + 400]
        assert '"existingText": null' in report_only_block
        assert '"replacementText": null' in report_only_block
        assert '"anchorText": null' in report_only_block
        assert '"insertPosition": null' in report_only_block

    def test_examples_do_not_leak_per_request_concepts(self) -> None:
        # Chunk K2 — the system prompt MUST NOT mention
        # ``evidenceElementId`` or the ``<para id="…">`` wrapper because
        # those are per-spec concepts and would break the cache prefix
        # for any spec that lacks the K1 paragraph map.
        prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        assert "evidenceElementId" not in prompt
        assert "<para id=" not in prompt
        assert "<row id=" not in prompt
        assert "<heading id=" not in prompt

    def test_system_prompt_is_deterministic_for_same_inputs(self) -> None:
        # The cache prefix is exact-match. Two calls with identical
        # (cycle, mode) must return byte-identical prompts.
        a = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        b = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        assert a == b


# ---------------------------------------------------------------------------
# 2. User message: spec text still appears AND the final task block lives
#    after it.
# ---------------------------------------------------------------------------


class TestUserMessageFinalTaskBlock:
    def test_spec_body_is_present(self) -> None:
        # Regression: the final-task wrapper sits *after* the spec body —
        # the spec content itself must still flow into the user message.
        msg = get_single_spec_user_message(
            "Comply with the current CBC.",
            "23 05 00 Common.docx",
            cycle=CALIFORNIA_2025,
        )
        assert "Comply with the current CBC." in msg

    def test_final_task_block_present(self) -> None:
        msg = get_single_spec_user_message(
            "Body", "f.docx", cycle=CALIFORNIA_2025,
        )
        assert "<final_task>" in msg
        assert "</final_task>" in msg

    def test_final_task_block_sits_after_spec_body(self) -> None:
        # The final-task block must come AFTER the spec body so the
        # stable instruction prefix in front of ``<spec `` is unchanged.
        msg = get_single_spec_user_message(
            "Body", "f.docx", cycle=CALIFORNIA_2025,
        )
        spec_close = msg.rindex("</spec>")
        final_open = msg.index("<final_task>")
        assert final_open > spec_close

    def test_final_task_block_sits_after_pre_detected_block(self) -> None:
        # When alerts fire, the order is: <spec> … </spec> →
        # <pre_detected> … </pre_detected> → <final_task> … </final_task>.
        # That way the final task can refer to the alerts as a known
        # data block rather than introducing them.
        alerts = [{
            "filename": "f.docx",
            "type": "leed",
            "match": "LEED Gold",
            "context": "",
            "position": 0,
            "deterministic_rule": "leed_reference",
        }]
        msg = get_single_spec_user_message(
            "Body", "f.docx",
            cycle=CALIFORNIA_2025,
            pre_detected_alerts=alerts,
        )
        pre_close = msg.index("</pre_detected>")
        final_open = msg.index("<final_task>")
        assert final_open > pre_close

    def test_final_task_lists_required_reminders(self) -> None:
        # Each bullet from the plan must be expressible as a substring
        # search — the wording is not pinned exactly so editors can
        # tighten phrasing without breaking the test, but the
        # *intent* of each bullet must be present.
        msg = get_single_spec_user_message(
            "Body", "f.docx", cycle=CALIFORNIA_2025,
        )
        lowered = msg.lower()
        # "review only the document above"
        assert "review only the document above" in lowered
        # "submit findings once"
        assert "submit findings once" in lowered
        # "remove findings lacking concrete evidence"
        assert "concrete evidence" in lowered
        # "ensure edit fields match actionType"
        assert "actiontype" in lowered
        # "avoid duplicating pre-detected alerts"
        assert "pre_detected" in lowered or "pre-detected" in lowered

    def test_id_hint_in_final_task_only_when_ids_enabled(self) -> None:
        # Legacy path (no paragraph_map): the final task must NOT mention
        # evidenceElementId — keeping it out is required by the Chunk K
        # ``test_user_message_legacy_path_omits_id_hint_when_no_map``
        # invariant.
        legacy = get_single_spec_user_message(
            "Body", "f.docx", cycle=CALIFORNIA_2025,
        )
        assert "evidenceElementId" not in legacy

        # Modern path (paragraph_map supplied): the final task gains the
        # "cite evidenceElementId" reminder. Note: the upstream id_hint
        # line near the top of the message also mentions
        # ``evidenceElementId`` — we are checking the final task itself
        # carries the reminder so the model sees it near the request to
        # submit findings.
        modern = get_single_spec_user_message(
            "Body", "f.docx",
            cycle=CALIFORNIA_2025,
            paragraph_map=_paragraph_map(),
        )
        final_block = modern[modern.index("<final_task>") : modern.index("</final_task>") + len("</final_task>")]
        assert "evidenceElementId" in final_block

    def test_id_hint_in_final_task_respects_env_toggle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``SPEC_CRITIC_ELEMENT_IDS=0`` reverts to the legacy rendering.
        # The final task block must mirror that — no ``evidenceElementId``
        # leakage when ids are off.
        monkeypatch.setenv("SPEC_CRITIC_ELEMENT_IDS", "0")
        msg = get_single_spec_user_message(
            "Body", "f.docx",
            cycle=CALIFORNIA_2025,
            paragraph_map=_paragraph_map(),
        )
        assert "evidenceElementId" not in msg


# ---------------------------------------------------------------------------
# 3. Cache-prefix invariants — adding the new blocks must not move bytes
#    before the variable spec payload.
# ---------------------------------------------------------------------------


class TestCachePrefixInvariants:
    """The stable instruction prefix in front of the spec payload must
    not vary with payload content. Earlier chunks pinned this rule for
    the pre-detected block and the K2 id rendering; Chunk 11 must
    preserve it for the final-task block too.
    """

    def test_user_message_prefix_invariant_across_payloads(self) -> None:
        a = get_single_spec_user_message(
            "alpha", "f.docx",
            cycle=CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE,
        )
        b = get_single_spec_user_message(
            "very different beta payload",
            "f.docx",
            cycle=CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE,
        )
        # Everything before ``<spec `` must match. The final-task block
        # comes after the spec body so it does not move the prefix.
        assert a.split("<spec ")[0] == b.split("<spec ")[0]

    def test_user_message_prefix_invariant_with_and_without_alerts(self) -> None:
        # Belt-and-suspenders re-run of Chunk D4.1's invariant with the
        # final-task block in the mix.
        alerts = [{
            "filename": "f.docx",
            "type": "leed",
            "match": "LEED Gold",
            "context": "",
            "position": 0,
            "deterministic_rule": "leed_reference",
        }]
        without = get_single_spec_user_message(
            "alpha", "f.docx", cycle=CALIFORNIA_2025,
        )
        with_alerts = get_single_spec_user_message(
            "alpha", "f.docx",
            cycle=CALIFORNIA_2025,
            pre_detected_alerts=alerts,
        )
        assert without.split("<spec ")[0] == with_alerts.split("<spec ")[0]

    def test_system_prompt_prefix_invariant_across_modes(self) -> None:
        # Modes share the same opening preamble through ``<task>``;
        # mode-specific text starts inside ``<task>`` and downstream
        # blocks. The examples block is mode-independent so it does not
        # shift bytes between modes.
        strict = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.STRICT)
        comp = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
        # The header banner up to ``<review_mode>`` is mode-independent.
        prefix_marker = "<review_mode>"
        assert strict.split(prefix_marker)[0] == comp.split(prefix_marker)[0]


# ---------------------------------------------------------------------------
# 4. Request builder integration — the prompt changes flow through the
#    central builder, so a request-shape capture sees them.
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_count_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the tiktoken-backed token counter to keep these tests offline.

    Mirrors the helper in ``tests/test_request_payload_shape.py``. The
    real tokenizer lazily downloads the cl100k_base BPE merge tables on
    first call, which fails in fully offline environments. The proxy
    keeps the request-builder integration tests hermetic — they only
    care about prompt content, not exact token counts.
    """
    def _fake_count(text: str | None) -> int:
        return len((text or "").split()) * 2

    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr(
        "src.review_request_builder.count_tokens", _fake_count, raising=False
    )


class TestRequestBuilderCarriesPromptChanges:
    """The Chunk 3 central request builder is the single path that
    materializes the prompts onto the wire. Confirm the new examples
    block and final-task block survive that round-trip — if a future
    refactor split the builders again, these tests should catch it.
    """

    def test_review_request_system_carries_examples(
        self, _stub_count_tokens
    ) -> None:
        from src.review_request_builder import (
            ReviewRequestSpec,
            build_review_request,
        )
        from src.api_config import MODEL_OPUS_47

        spec = ReviewRequestSpec(
            spec_content="Body.",
            filename="a.docx",
            project_context="",
            cycle=CALIFORNIA_2025,
            mode=ReviewMode.COMPREHENSIVE,
            paragraph_map=None,
            pre_detected_alerts=None,
            model=MODEL_OPUS_47,
            batch=True,
        )
        built = build_review_request(spec)
        # The system prompt the builder hands the SDK must carry the
        # examples — that's what the model sees, not whatever the
        # tests-only call to ``get_system_prompt`` returns.
        assert "<examples>" in built.system_prompt
        assert "</examples>" in built.system_prompt
        assert '"actionType": "EDIT"' in built.system_prompt
        assert '"actionType": "ADD"' in built.system_prompt
        assert '"actionType": "REPORT_ONLY"' in built.system_prompt
        assert "DO NOT REPORT" in built.system_prompt

    def test_review_request_user_message_carries_final_task(
        self, _stub_count_tokens
    ) -> None:
        from src.review_request_builder import (
            ReviewRequestSpec,
            build_review_request,
        )
        from src.api_config import MODEL_OPUS_47

        spec = ReviewRequestSpec(
            spec_content="Body.",
            filename="a.docx",
            project_context="",
            cycle=CALIFORNIA_2025,
            mode=ReviewMode.COMPREHENSIVE,
            paragraph_map=None,
            pre_detected_alerts=None,
            model=MODEL_OPUS_47,
            batch=True,
        )
        built = build_review_request(spec)
        # The user message handed to the SDK carries the final task
        # block, and that block sits AFTER the spec body.
        assert "<final_task>" in built.user_message
        assert "</final_task>" in built.user_message
        spec_close = built.user_message.rindex("</spec>")
        final_open = built.user_message.index("<final_task>")
        assert final_open > spec_close
