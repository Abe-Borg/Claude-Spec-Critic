# Spec Critic — Remediation & Cleanup Plan

A modular, à la carte plan for cleaning up the issues found in the deep-dive audit.
Each module is **independently selectable** — pick the ones you want; skip the rest.
Dependencies between modules are called out explicitly where they exist.

> **No code in this document by design.** It describes *what* to change and *why*.
> A coding agent will implement the modules you select.

---

## Two standing constraints (from you)

1. **Remove the resume subsystem entirely** ("resume batch and all related code"). This is Module **M1** and it changes the calculus on several other items (it *moots* the resume bug and removes the worst serializer-drift offender). Notes elsewhere assume M1 is happening.
2. **Keep all "emit for a future program" code.** Anything that writes to the report or the `.edits.json` sidecar for a downstream applier **stays**. No telemetry field on `VerificationResult` gets deleted. The only thing being removed is the *durable resume persistence* of that data — not the data itself.

---

## Legend

**Importance** — how much this matters as a fix:
- 🔴 **Critical** — correctness/feature bug or actively misleading; fix it.
- 🟠 **High** — significant risk or leverage; strongly recommended.
- 🟡 **Medium** — real improvement, no active breakage; do it when convenient.
- ⚪ **Low** — hygiene/cosmetic; nice to have.

**Safe to skip?**
- **No** — you asked for it / it's a real bug.
- **For now** — inert today, but it will mislead you or the next agent until fixed.
- **Yes** — purely cosmetic or internal; skipping costs you nothing functional.

---

## At a glance

| Module | What | Importance | Safe to skip? | Effort | Depends on | Status |
|---|---|---|---|---|---|---|
| **M1** | Remove the resume / durable-state subsystem | 🟠 (Directed) | **No** | Large | — | — |
| **M2** | Resolve the orphaned cross-check suppression feature | 🟠 High | For now | Small (delete) / Med (wire) | needs your decision | — |
| **M3** | Purge the auto-apply / locator fossils | 🟡 Medium | Yes | Medium | — | 🟡 Implemented — PR open |
| **M4** | Sweep the remaining dead symbols | ⚪ Low | Yes | Small | — | ✅ **Done** — PR #194 (merged) |
| **M5** | Add minimal CI | 🟠 High | For now | Small | — | ✅ **Done** — PR #197 (merged) |
| **M6** | Fix CLAUDE.md / README staleness | 🟡 Medium | For now | Small | M1, M2, M3 (doc tail) | 🟡 Step 1 done — PR #195 |
| **M7** | Unify `VerificationResult` serialization | ⚪ Low | Yes | Medium | easier after M1 | — |
| **M8** | Split the `verifier.py` god-module | ⚪ Low | Yes | Large/risky | — | — |
| **M9** | Cosmetic: chunk-comments, test names, router/routing rename | ⚪ Low | Yes | Medium (mechanical) | — | — |

**If you want the highest value for the least work:** do **M1 + M2 (decide) + M5 + M6**. Everything else is hygiene you can take or leave.

### Progress log

- **M4 — Sweep dead symbols** — ✅ **merged** via PR #194. Removed confirmed zero-caller symbols across `widgets.py`, `api_config.py`, `extraction_cache.py`, `prompt_serialization.py`, `spans.py`, `config.py`, `verification_routing.py`. Two deliberate deviations from the literal list, both within the module's stated latitude: **kept** `clear_token_cache` (live test-fixture use — the documented "keep both" option) and **relocated** `_tools_include_web_fetch` into its test file rather than deleting the behavior tests that consume it. Hermetic suite green (677 passed).
- **M6 step 1 — Stale resume-retry stub docs** — 🟡 **open** in PR #195. Removed the `resume_retry_failed_only` / `SPEC_CRITIC_RESUME_RETRY_FAILED_ONLY` references (already gone from `src/`) from CLAUDE.md (Chunk 12 paragraph + §8 row) and README.md (the "(Stub)" section + the Chunk 12 changelog sentence). M6 steps 2–5 remain — they are the doc tail of M1 / M2 / M3.
- **M3 — Purge auto-apply / locator fossils** — 🟡 **implemented, PR open**. Net −308 lines across 10 files. `diagnostics.py`: deleted the dead auto-apply counter cluster (13 fields, the `record_skipped_spec` / `record_edit_skip` / `record_edit_report` / `record_locator_method` recorders, `_auto_apply_quality_lines`, the "AUTO-APPLY QUALITY" rendering, and the matching `summary()` keys); kept the live `record_api_call` / `record_failed_spec`. `widgets.py`: removed the permanently-zero edit-stats panel. `extractor.py`: removed `run_count` / `distinct_formatting_runs` / `run_format_map` and the now-orphaned `_summarize_paragraph_formatting` (kept `element_id` / `section_id`). Reworded the 3 phantom `:func:composite_edit_confidence` refs (router ×2, verifier) to describe the live `requires_elevated_confidence` telemetry, plus the stale locator/apply-edits docstrings in `reviewer.py`, `structured_schemas.py`, `pipeline.py`, `report_status.py`, `extractor.py`, and the deleted-`spec_editor.py` comment in `tests/fixtures/docx_fixtures.py`. **Two notes:** (1) also removed `skipped_specs` (its only writer `record_skipped_spec` was zero-caller) — slightly beyond the literal field range but squarely the "purge fossils" intent; (2) the 6 `make_paragraph_with_*` unsafe-markup fixture builders are now confirmed zero-caller dead code from the removed edit-safety stack — left in place (comment reworded only) as a candidate for a follow-up sweep. CLAUDE.md / README already describe the emit-only model correctly, so no M6 doc tail was needed. Hermetic suite green (677 passed); the GUI diagnostics view couldn't be launched live (no tkinter in the sandbox), so that render is verified by compile + logic review only.
- **M5 — Add minimal CI** — ✅ **merged** via PR #197. Added `.github/workflows/tests.yml` (step 1): on push to `master` and on every PR, it sets up Python 3.11, `pip install -r requirements.txt`, and runs the hermetic `python -m pytest` suite (no API key / network — conftest injects a placeholder key and skips `@pytest.mark.network`). Verified green locally: **677 passed**, both with tkinter absent (3.11) and present-without-display (3.12), so the run is hermetic regardless of runner Tk state. Optional step 2 (eval-harness job) and step 3 (cross-check suppression integration test) were **deliberately skipped**: step 2 needs an API key/network secret, and step 3 ties to M2a — the suppression feature is still unwired (M2 undecided), so that test would fail today. Both remain easy follow-ups once M2 is resolved.

---

## M1 — Remove the resume / durable-state subsystem  🟠 Directed · Safe to skip: **No**

**Why it matters.** You asked for it, and it's a sound call: the resume layer is a large, bug-prone surface. It already harbored a latent bug (escalation history silently lost on resume — see the note at the bottom), and it is the single worst offender in the serializer-drift problem (one of three hand-maintained projections of a 35-field dataclass). Removing it shrinks the codebase and deletes an entire *class* of "field added here but not there" bugs.

**The one thing not to get wrong.** There are two different things tangled together:
- **Durable resume persistence** — saving pipeline state to disk at each phase boundary and offering to resume on next launch. **This is what gets deleted.**
- **In-memory batch state objects** — `BatchSubmission`, `CollectedBatchState`, `BatchJob`, etc. The *live* forward-running pipeline uses these every run. **These stay.** Confirm at implementation time that these dataclasses are defined outside `resume_state.py` (they appear to live in `batch.py`/`pipeline.py`); if any live type is defined inside `resume_state.py`, relocate it rather than delete it.

**Scope / steps:**
1. **Preserve the emit dependency first.** `src/output/edit_sidecar.py` imports `serialize_edit_proposal` from `resume_state.py`. Relocate that function (and any private helpers it needs) into `edit_sidecar.py` itself, or a small new `src/output/edit_serialization.py`. The `.edits.json` sidecar must keep working — it's exactly the "emit for a future program" code you want kept.
2. **Delete the modules:** `src/orchestration/resume_state.py` and `src/batch/batch_state_store.py`.
3. **Gut resume logic from `src/gui/batch_controller.py`** (the main consumer): remove every `save_batch_state(build_resume_state(...))` call at the phase boundaries (≈ lines 112, 173, 182, 266, 340, 400, 424…), the `PHASE_*` imports, `build_resume_state`, the `resume_batch()` / `is_valid_verification_resume_state()` / `_resume_*` handlers, and the "resume available / Resume" UI prompt (≈ lines 455–521, 544–680). The batch flow becomes forward-only.
4. **`src/gui/review_run_controller.py`:** remove the `delete_batch_state` import and its call.
5. **`src/orchestration/pipeline.py`:** remove any `deserialize_resume_state` / phase-constant usage; keep all forward orchestration intact.
6. **Tests:** several test files import `resume_state` serializers *purely* to assert telemetry survives a resume round-trip (`test_chunk_2/3/5/10/11/12/13`, `test_verification_token_telemetry`, the resume tests in `test_tracing`, and `test_chunk_7/8` Finding round-trips, `test_chunk_o` submission round-trips). For each: if its sole purpose is resume survival, delete it; if it also asserts non-resume behavior, strip only the resume part. Coverage that still matters (telemetry surviving the *cache*) can be re-pointed at the cache serializer — see M7.
7. **Docs tail** lives in **M6** (delete CLAUDE.md §9 and resume mentions).

**Risk & verification.** This touches the live GUI batch flow, so it's the highest-blast-radius module. Minimum bar: the hermetic pytest suite passes and `python main.py` still imports/launches. **Full** confidence needs a manual end-to-end batch run (one real review start-to-report) to confirm the forward flow completes without the state-save calls — flag if the environment can't do a real run, and treat that as a known verification gap.

---

## M2 — Resolve the orphaned cross-check suppression feature  🟠 High · Safe to skip: **For now** · *needs your decision*

**Why it matters.** `classify_cross_check_dependencies` (`pipeline.py:908`, ~140 lines) is documented in CLAUDE.md §2 as a *live invariant* and backed by a 632-line test file — **but it has zero production callers.** The live cross-check path `run_cross_check_for_batch` (`pipeline.py:1124`) never calls it. Because the only production writes to `Finding.suppression_reason` are *inside* this dead function, the entire suppression path is inert: the `SUPPRESSED` label, the "suppressed by cross-check" `MANUAL_REVIEW_REQUIRED` branch, the report's suppression/dependency notes, and the banner's "suppressed" count are all unreachable. This is the worst kind of dead code — it's *documented and tested as if it works*. Leaving it as-is is the only truly bad option.

**This needs a one-time decision from you. Pick M2a or M2b:**

### M2a — Wire it in (you actually want the feature)
- Invoke `classify_cross_check_dependencies` on the cross-check findings inside `run_cross_check_for_batch`, stash the suppressed findings per the documented contract, and confirm the report renders them.
- **Why pick this:** the feature suppresses cross-check findings whose premises were all disputed — a real false-positive guard. If that's behavior you intended, this realizes it.
- **Risk:** it *changes report output* (findings that currently surface may now be suppressed). Validate with the eval harness / a manual review before trusting it. Effort: **Medium**.

### M2b — Delete it (you don't need the feature)
- Remove the function, its 632-line test file, the two `suppression_reason` writes, and then prune everything that becomes unreachable: the `SUPPRESSED` `EditActionLabel`, the suppression-based `MANUAL_REVIEW_REQUIRED` branch in `report_status.py`, `report_exporter._write_suppression_reason` / `_write_dependency_note`, and the banner "suppressed" count.
- **Why pick this:** less code, honest docs. It's more deletion than it first looks because suppression threads through `report_status` → `report_exporter` → diagnostics. Effort: **Small–Medium**.

**Either way**, update CLAUDE.md §2 (M6). **Safe to skip for now** only in the sense that nothing breaks today — but the docs are actively lying until you resolve it.

---

## M3 — Purge the auto-apply / locator fossils  🟡 Medium · Safe to skip: **Yes**

**Why it matters.** These are the "fragments of old implementations" you reacted to — leftovers from the auto-apply/locator stack removed in v3.0.0. Nothing here is a functional bug, but they mislead every reader, the GUI shows a permanently-zero stats panel, and there's a little wasted compute on every extraction. A prior commit (`28e6381`) claimed to do this cleanup but missed most of it.

**Scope / steps:**
1. **`src/orchestration/diagnostics.py`** — delete the dead auto-apply cluster: the ~13 counter/state fields (≈ lines 237–312: `edits_applied_total`, `edits_skipped_total`, `edits_failed_total`, `ambiguous_locator_count`, `locator_methods`, `replacement_text_normalized_count`, `punctuation_boundary_fixed_count`, `add_demoted_missing_position_count`, …); the zero-caller methods `record_skipped_spec`, `record_edit_skip`, `record_edit_report`, `record_locator_method`; `_auto_apply_quality_lines`; the "AUTO-APPLY QUALITY" rendering in `to_text()` (≈ 1159–1186) and the corresponding emissions in `summary()` (≈ 930–935). (`record_api_call` and `record_failed_spec` are live — keep them.)
2. **`src/gui/widgets.py`** — remove the GUI edit-stats panel (≈ lines 586–607, "Edits: applied=… ambiguous_locators=…") that renders the now-deleted counters.
3. **`src/input/extractor.py`** — remove the `run_count`, `distinct_formatting_runs`, `run_format_map` fields on `ParagraphMapping` and the part of `_summarize_paragraph_formatting` that computes them (they are never read anywhere; leftover from the locator's safety-downgrade pass). **Keep** the `element_id` / `section_id` logic — that's live.
4. **Phantom function reference** — `composite_edit_confidence` does not exist anywhere, yet three Sphinx `:func:` cross-references point at it (`verification_router.py:73`, `:149`; `verifier.py:261`). Reword those to describe the live `requires_elevated_confidence` telemetry without naming a deleted function.
5. **Stale "locator"/"apply_edits" docstrings** — reword to the emit-only model in `reviewer.py` (≈ 107, 143–144, 197, 457), `structured_schemas.py` (≈ 121–122), `extractor.py` (≈ 39–46, 60, 76, 94, 133–143), `pipeline.py` (≈ 381–387), `verification_router.py` (≈ 72–79). Also the stale comment in `tests/fixtures/docx_fixtures.py:104` referencing the deleted `spec_editor.py`.

**Risk & verification.** Low. The deleted methods/fields have no callers; pytest should stay green. Sanity-check the GUI still renders the diagnostics view (minus the edit-stats panel).

> **Interaction with M2b:** if you delete the suppression feature, the diagnostics "suppressed" count is part of that work — coordinate so you don't touch the same `summary()` block twice.

---

## M4 — Sweep the remaining dead symbols  ⚪ Low · Safe to skip: **Yes** · ✅ DONE (PR #194, merged)

**Why it matters.** Pure hygiene — a smaller, more honest surface. Good "warm-up" batch; each is a confirmed zero-caller symbol.

**Scope (delete, after re-confirming zero callers at implementation time):**
- `src/gui/widgets.py` — `_confidence_color`, `_confidence_label`, `CONFIDENCE_COLORS`, `_VERDICT_ICONS`.
- `src/core/api_config.py` — `WEB_SEARCH_TOOL` (its comment claims "preserved for backward compatibility," but nothing imports it), `EFFORT_LOW`, `EFFORT_XHIGH`.
- `src/input/extraction_cache.py` — `extract_text_cached`; `clear_token_cache` (test-only — delete its test too, or keep both).
- `src/review/prompt_serialization.py` — `TAG_CHUNK_FINDINGS`, `TAG_CHUNK`.
- `src/tracing/spans.py` — `SPAN_KINDS`, `EVENT_TOOL_RESULT`, `STATUS_SKIPPED`; `src/tracing/config.py` — `CAPTURE_LEVELS`.
- `src/verification/verification_routing.py` — `_tools_include_web_fetch` (test-only — delete its test too).

**Risk & verification.** Low. A couple are test-only, so decide delete-the-test vs keep. Run pytest after.

---

## M5 — Add minimal CI  🟠 High · Safe to skip: **For now** (but high leverage)

**Why it matters.** There is no CI anywhere (no `.github/`, Makefile, tox). ~982 tests and two eval harnesses run only when someone remembers. The orphaned cross-check feature (M2) is *exactly* the kind of regression a thin integration test + CI would have caught — the unit tests call the function directly, so they stay green even though production stopped calling it. CI protects every other module you ship here.

**Scope / steps:**
1. Add a GitHub Actions workflow that installs `requirements.txt` and runs the hermetic `pytest` suite (it needs no API key/network — see CLAUDE.md §10).
2. *Optional:* a separate, manually-triggered job for the eval harnesses (`python -m evals.runner`, `python -m evals.calibration.runner`) — note these may need an API key/network, so don't gate PRs on them.
3. *Optional but recommended (ties to M2a):* add one **integration** test asserting `run_cross_check_for_batch` actually invokes suppression — i.e., a test that fails if a feature gets silently unwired. This directly closes the gap that hid M2.

**Risk & verification.** Low. The workflow either goes green or tells you what's broken — which is the point. Do this early so the rest of the plan lands on a safety net.

---

## M6 — Fix CLAUDE.md / README staleness  🟡 Medium · Safe to skip: **For now** · 🟡 Step 1 DONE (PR #195)

**Why it matters.** CLAUDE.md is the engineering reference the next agent (including future-me) reads first. Stale entries don't just clutter — they actively cause wrong decisions.

**Scope / steps:**
1. ✅ **Done (PR #195).** Removed the `resume_retry_failed_only` stub description from the Chunk 12 section and the `SPEC_CRITIC_RESUME_RETRY_FAILED_ONLY` row from the §8 env-var table — both described a function/flag already deleted from `src/`. Also removed the matching references in README.md (the "(Stub)" section + the Chunk 12 changelog sentence).
2. **After M1:** delete §9 "Resume State" entirely, and scrub resume mentions from §1, the high-level flow, and §8.
3. **After M2:** if you chose M2b, delete the §2 "Cross-check dependency suppression" block and the `MANUAL_REVIEW_REQUIRED`/`SUPPRESSED` rows that depend on it; if M2a, keep but verify the description matches the now-wired behavior.
4. **After M3:** fix any invariant text that still implies an internal applier/locator.
5. **README.md** — scrub resume mentions to match.

**Risk & verification.** None (docs only). This is the "documentation tail" of whatever you do in M1/M2/M3 — cheap, high value-per-minute. Do it in the same PR as the code it documents.

---

## M7 — Unify `VerificationResult` serialization  ⚪ Low · Safe to skip: **Yes**

**Why it matters.** The 35-field dataclass is hand-serialized in multiple projections that have drifted apart (this is what produced the resume bug). **After M1**, the worst projection (resume) is gone, leaving only the verification cache and the tracing hooks — so the urgency drops a lot. Optionally finish the job so the drift class can't come back.

**Scope / steps:**
- Replace the cache's hand-written `_result_to_dict` / `_clone_for_hit` / `_clone_for_store` with a single `dataclasses.asdict`-based round-trip, plus an explicit field policy: the cache intentionally persists only a subset and has grounding/`verification_failed`/`budget_exhausted` guards — keep those as an explicit allow-list + post-filter rather than as the implicit "whatever fields I remembered to copy."
- **Keep every emit field** — this is a *how-we-serialize* change, not a *what-we-keep* change.

**Risk & verification.** Medium (cache round-trip is load-bearing for verdict replay). Covered by the existing cache tests; add a "round-trips every field" test so future fields are caught automatically.

---

## M8 — Split the `verifier.py` god-module  ⚪ Low · Safe to skip: **Yes**

**Why it matters.** `verifier.py` is 3,176 lines and ~50 functions spanning five concerns: the `VerificationResult` data model, prompt construction, response parsing, real-time orchestration, and batch orchestration. It's hard to navigate. **Purely a maintainability play — zero behavior change.**

**Scope / steps:** extract along the existing seams into e.g. `verification_result.py` (the dataclass), `verification_prompts.py`, `verification_parsing.py`, `verifier_realtime.py`, `verifier_batch.py`; keep the public import surface stable via re-exports if other modules import from `verifier`.

**Risk & verification.** This is a *large, broad-touch refactor* for no functional gain — highest risk-to-reward in the plan. **Do it last, only if you'll keep heavily maintaining the verifier.** Verify by diffing behavior: full pytest green + an eval-harness run showing identical metrics before/after.

---

## M9 — Cosmetic: chunk-comments, test names, router/routing rename  ⚪ Low · Safe to skip: **Yes**

**Why it matters.** Development-process fossils. They don't break anything but they clutter and mislead.

**Scope / steps:**
1. **163 "Chunk N / Trust Upgrade:" comment prefixes** across ~20 source files — strip the chunk label, **keep the explanatory *why*** (git blame already records *when*; the chunk number means nothing to a reader).
2. **Test file names** — rename `tests/test_chunk_*` to feature names (two clashing schemes today: numeric `2–13` with gaps, plus lettered `b/d4/e/g/m/n/o`). E.g. `test_chunk_m_cross_check_deps.py` → `test_cross_check_dependencies.py`.
3. **`verification_router.py` vs `verification_routing.py`** — the two-letter-different names force a reader to hold both in their head. Rename the *router* (local-skip pre-classification) to something like `local_skip_classifier.py` and update imports.

**Risk & verification.** Low but touches many files; do it as its own PR so it doesn't drown out substantive diffs. Pytest green after the renames (watch for the test-file and module-import renames).

---

## Suggested orderings

- **Protect first:** land **M5 (CI)** early so everything after it has a safety net.
- **Directed work:** **M1**, with its documentation tail in **M6**, in the same PR.
- **Decision work:** **M2** (your pick of a/b), with its doc tail in **M6**.
- **Hygiene, anytime:** **M3**, **M4** — independent and safe; good to batch together.
- **Optional / last:** **M7** (after M1), **M8** (riskiest — only if needed), **M9** (own PR).

**Dependency notes:**
- **M6** depends on whichever of **M1 / M2 / M3** you actually do — it's their doc tail, not standalone work (except the resume-retry stub fix in M6 step 1, which is stale *today*).
- **M7** is much easier and lower-risk *after* **M1**.
- **M3** and **M4** both touch `widgets.py` dead helpers — I split them (edit-stats panel → M3; confidence/verdict-icon helpers → M4). If you do both, do them together to avoid two passes over the same file.
- **M2b** interacts with **M3** (the diagnostics "suppressed" count) and `report_status.py`.

---

## Note on the resume escalation bug (why it's not its own module)

The audit found a real bug: on resume, `escalation_attempted` / `initial_verdict` / `initial_model` / `escalation_changed_verdict` / `escalation_reason` are **not** serialized, so a resumed report shows the `⚡ VERIFIED_CONTESTED` badge but drops all explanation of *why*. **M1 deletes the resume subsystem, which eliminates this bug outright** — so there is no separate fix to make.

*Contingency:* if you ever decide **not** to do M1 and keep any resume capability, this becomes a standalone 🔴 **Critical** fix — add those five fields to `serialize_verification_result` / `deserialize_verification_result` in `resume_state.py` (and ideally adopt **M7** so the next field can't drift the same way).
