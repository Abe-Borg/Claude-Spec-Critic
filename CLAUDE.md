# CLAUDE.md — Spec Critic Technical Reference

This document is the engineering/operator reference for the Spec Critic codebase. It is intentionally implementation-focused and should be kept aligned with the actual runtime behavior in `src/`.

---

## 1) System Purpose

Spec Critic is a Python desktop application that performs AI-assisted review of California mechanical/plumbing specification documents (`.docx`).

Primary outcomes:

- identify likely code/compliance and coordination issues,
- classify findings with severity + confidence,
- verify findings with web-search-backed evidence,
- generate stakeholder-readable reports,
- optionally apply precise edits back to Word source files.

---

## 2) Runtime Topology

### User Interface Layer

- `src/gui.py`
  - Main application window and user flow control.
  - File selection, mode toggles, code cycle selection, start/resume controls.
  - Bridges user actions to orchestration functions in `pipeline.py`.
- `src/widgets.py`
  - Composite UI components:
    - token gauge,
    - file panel,
    - enhanced log,
    - report windows,
    - edit selection/summary dialogs,
    - diagnostics viewer.

### Orchestration Layer

- `src/pipeline.py`
  - Central sequencing for extraction → review → dedup → cross-check → verification → finalize.
  - Contains both convenience wrappers and explicit phased APIs used for resumable batch flow.

### Model Interaction Layer

- `src/reviewer.py`
  - Primary Claude review calls.
  - Streaming response assembly and JSON array extraction.
- `src/cross_checker.py`
  - Multi-spec coordination analysis.
- `src/verifier.py`
  - Secondary verification adjudication, including batch-wave retries/continuations.
- `src/prompts.py`
  - Prompt builders (system + user).
- `src/verification_config.py`
  - Shared verification constants/configuration.

### Batch and Recovery Layer

- `src/batch.py`
  - Message Batches API submit/poll/retrieve/cancel wrappers.
- `src/batch_runtime.py`
  - Poll policy and bounded waiting (elapsed/no-progress/errors).
- `src/resume_state.py`
  - Durable state serialization/deserialization for resumable workflows.

### Document/Editing Layer

- `src/extractor.py`
  - DOCX text + structure extraction (including table and header/footer harvesting).
- `src/preprocessor.py`
  - Local static checks (LEED mentions, unresolved placeholders).
- `src/edit_candidates.py`
  - Selection/classification of findings suitable for automatic edits.
- `src/edit_locator.py`
  - Matching finding text to concrete paragraph/cell locations.
- `src/spec_editor.py`
  - Performs safe edit actions (replace/delete/add in paragraphs and tables).
- `src/apply_edits.py`
  - Orchestrates end-to-end edit plan execution across files.

### Reporting + Diagnostics Layer

- `src/report_exporter.py`
  - Produces final `.docx` reports with structured sections, severity tables, verification blocks, and optional verbose detail.
- `src/diagnostics.py`
  - Timestamped diagnostic events and phase duration summaries.

### Utility Domain Layer

- `src/tokenizer.py`
  - Token counting and context-budget heuristics.
- `src/code_cycles.py`
  - California code-cycle metadata and defaults.
- `src/__init__.py`
  - Package version source of truth for app runtime.

---

## 3) Key Domain Objects

### `ExtractedSpec` (`extractor.py`)
Represents one extracted specification unit with full content and metadata, including paragraph mapping used by edit-location logic.

### `Finding` (`reviewer.py`)
Canonical issue object containing:

- severity,
- source file/section,
- issue statement,
- action type,
- existing/replacement text,
- code reference,
- confidence,
- optional verification payload.

### `ReviewResult` (`reviewer.py`)
Aggregates findings and model run metadata:

- raw model content,
- token usage,
- elapsed time,
- stop reason,
- parse status,
- optional error context.

### `BatchJob` / `BatchStatus` (`batch.py`)
Tracks batch submission IDs and retrieved status fields from Anthropic.

### `VerificationResult` (`verifier.py`)
Structured post-review verdict and evidence summary attached per finding.

### `PipelineResult` (`pipeline.py`)
Final object delivered to UI/report export, combining reviewed files, alerts, findings, and optional cross-check data.

---

## 4) Primary Execution Flows

## A. Real-time Flow

1. UI selects files and options.
2. `extractor.extract_multiple_specs()` loads specs.
3. `preprocessor.preprocess_spec()` emits local alerts.
4. `reviewer.review_single_spec()` runs per-spec primary review.
5. `pipeline._deduplicate_findings()` merges duplicates.
6. Optional `cross_checker.run_cross_check()`.
7. `verifier.verify_findings()` (or verification batch path depending call site).
8. `PipelineResult` is rendered/exported.

## B. Batch Flow (Phased + Resumable)

1. `pipeline.start_batch_review()` prepares specs and submits review batch.
2. `pipeline.collect_review_batch_results()` retrieves/parses results.
3. Optional `pipeline.run_cross_check_for_batch()`.
4. `pipeline.prepare_verification_work()` collects all findings.
5. `pipeline.start_batch_verification()` submits verification batch job.
6. `pipeline.collect_batch_verification_results()` executes wave logic until completion/limits.
7. `pipeline.finalize_batch_result()` returns `PipelineResult`.
8. Resume support is driven by `resume_state.build_resume_state()` + `deserialize_resume_state()`.

---

## 5) Prompting and Code-Cycle Behavior

- Prompt system text is produced by `prompts.get_system_prompt(cycle=...)`.
- Per-spec user content is produced by `prompts.get_single_spec_user_message(...)`.
- Cross-check prompt text is cycle-aware and generated in `cross_checker.py`.
- Verification prompt/system content is generated in `verifier.py` and references the selected `CodeCycle`.
- `code_cycles.py` currently defines California **2022** and **2025** cycles, defaulting to **2025**.

---

## 6) Token/Context Budgeting

`tokenizer.py` defines operational budgets, including:

- model context maximum,
- output token ceilings,
- recommended conservative maxima,
- cross-check overhead and output reserves.

Use `analyze_token_usage()` before expensive multi-spec operations when extending pipeline behavior.

---

## 7) Verification Wave Semantics

`verifier.py` includes wave-based follow-up logic for partially successful verification batches.

Important behavior:

- classification of completed vs retryable vs continuation-needed outcomes,
- bounded by `MAX_VERIFICATION_WAVES`,
- uses batch helper functions in `batch.py`,
- supports progress/log callbacks for UX transparency.

---

## 8) Edit Automation Architecture

Edit flow is intentionally segmented:

1. **Candidate classification** (`edit_candidates.py`) filters findings that can be safely converted into actionable edits.
2. **Location resolution** (`edit_locator.py`) attempts exact, normalized, fuzzy, and section-anchored matches with confidence scoring.
3. **Action construction + conflict resolution** (`spec_editor.build_edit_actions()` and conflict helpers).
4. **Document mutation** (`spec_editor.apply_edits_to_spec()`), including table cell support and overlap handling.
5. **Cross-file plan execution** (`apply_edits.execute_edit_plan()`).

This layering allows testing, explainability, and safer fallback behavior when text matches are ambiguous.

---

## 9) Reporting Contract

`report_exporter.export_report(...)` produces a Word document containing:

- title/context metadata,
- files reviewed,
- methodology note,
- summary tables (severity and timing),
- LEED/placeholder alerts,
- findings section (compact/verbose formatting),
- optional cross-check section,
- optional model thinking/narrative sections where applicable.

Styling helpers include table cell shading, heading outline levels, and collapsed sections for readability in Word.

---

## 10) GUI and State Persistence Notes

`gui.py` contains:

- app state directory selection via platformdirs,
- API key loading helpers,
- batch state save/load/delete wrappers,
- `.docx` support enforcement,
- phase-aware resume behavior hooked into `resume_state.py`.

The GUI is the canonical integration surface; changes in `pipeline.py` should be validated against GUI callbacks and report window assumptions in `widgets.py`.

---

## 11) Test Coverage Map

- `tests/test_edit_candidates.py` → candidate selection/classification logic.
- `tests/test_edit_locator.py` → locator confidence/match strategy behavior.
- `tests/test_spec_editor.py` → action application and edit safety semantics.
- `tests/test_core_regressions.py` → orchestration and regression guardrails.

Add tests first when changing matching, parsing, or orchestration behavior.

---

## 12) Dependencies

Declared via `requirements.txt` and `pyproject.toml` (setuptools build backend).

Primary runtime dependencies:

- `anthropic`
- `python-docx`
- `tiktoken`
- `customtkinter`
- `platformdirs`
- `tkinterdnd2`
- `lxml` (runtime dependency in `requirements.txt`)

---

## 13) Operational Risks / Invariants

### Important invariants

- Input extension support is restricted to `.docx`.
- `Finding` schema compatibility must be preserved across reviewer, verifier, exporter, and edit modules.
- Resume-state serialization fields must remain backward-compatible or be explicitly migrated.
- Verification parsing should fail closed (explicit uncertain/error verdicts), not silently drop malformed content.

### Common failure surfaces

- upstream API/network transient errors,
- malformed/non-standard source documents,
- long-running batch poll starvation,
- overly ambiguous replacement spans during edit location.

---

## 14) Maintenance Checklist for Engineers

When making meaningful changes:

1. Update or add tests.
2. Validate both real-time and batch paths (if affected).
3. Validate resume/restart behavior for batch phases (if affected).
4. Validate report export for findings + verification rendering.
5. Keep README (user-facing) and CLAUDE (engineering-facing) aligned.
6. If release-impacting, sync versions in `src/__init__.py` and `pyproject.toml`.

---

## 15) Fast Navigation Guide

- **Need to change orchestration?** → `src/pipeline.py`
- **Need to change prompting/output parsing?** → `src/reviewer.py`, `src/prompts.py`
- **Need to change verification behavior?** → `src/verifier.py`, `src/batch.py`, `src/batch_runtime.py`
- **Need to change edit automation?** → `src/edit_candidates.py`, `src/edit_locator.py`, `src/spec_editor.py`, `src/apply_edits.py`
- **Need to change UI?** → `src/gui.py`, `src/widgets.py`
- **Need to change Word export?** → `src/report_exporter.py`

