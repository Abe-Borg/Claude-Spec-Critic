# Spec Critic

Spec Critic is a desktop QA/review tool for California MEP specification teams. It ingests CSI-style `.docx` specification sections (mechanical/plumbing focus), runs LLM-assisted compliance and constructability checks, verifies each finding against code references and web evidence, and produces both an interactive in-app report and a formal Word deliverable.

The project is optimized for DSA-oriented K-12 workflows and California code cycle selection (2022/2025), with an emphasis on:

- high-signal findings,
- explicit severity and confidence scoring,
- deterministic batch/resume behavior,
- and optional “apply edits back to source spec” automation.

---

## Current Status

- **App version:** `2.8.3` (package `src.__version__`).
- **Packaging version:** `2.8.0` in `pyproject.toml` (if you cut a release, sync this with `src/__init__.py`).
- **Runtime:** Python 3.11+ desktop app (CustomTkinter + TkinterDnD2).
- **Model stack:** Anthropic Claude Opus 4.6 for review/cross-check/verification.

---

## What the Application Does (End-to-End)

1. **Load spec files (`.docx` only).**
2. **Extract body + table + header/footer text** while preserving useful paragraph mapping metadata.
3. **Run local pre-screen checks** (LEED references, unresolved placeholders) without API calls.
4. **Run primary compliance review** per spec (real-time or batch mode).
5. **Deduplicate findings** across specs.
6. **Optionally run cross-spec coordination check** to catch contradictions, scope gaps, and interface misses.
7. **Run verification phase** with web-search-backed adjudication for each finding.
8. **Present results** in GUI report windows.
9. **Optionally export `.docx` review report.**
10. **Optionally generate and apply surgical edits** back into source Word documents.

---

## Core Capabilities

### 1) Review Modes

- **Real-time mode**
  - Immediate streaming review responses.
  - Higher immediate cost profile.
- **Batch mode**
  - Uses Anthropic Message Batches API.
  - Lower-cost asynchronous execution.
  - Durable resume-state support across app restarts.

### 2) Code Cycle Awareness

- Built-in support for:
  - **California 2022 code cycle**
  - **California 2025 code cycle**
- Cycle selection drives prompt framing and code reference expectations across review + verification.

### 3) Verification Wave Engine

Verification is not a trivial post-process; it includes:

- structured verdict parsing,
- evidence/source extraction,
- retry/continuation logic,
- bounded polling policies,
- and multi-wave completion handling.

### 4) Editable Output Pipeline

When enabled, findings can be converted to edit candidates and applied to source docs through:

- exact/normalized/fuzzy/section-anchored matching,
- conflict resolution,
- safe paragraph/table operations,
- and per-file edit reporting.

### 5) Reporting

- **Interactive GUI report** with collapsible finding cards and severity grouping.
- **Word export** with heading hierarchy, summary tables, alerts, verification outcomes, and optional verbose finding detail.

---

## Repository Structure

```text
.
├── main.py                       # PyInstaller-friendly app entry point
├── README.md                     # Project overview and operations guide
├── CLAUDE.md                     # Deep technical architecture reference
├── pyproject.toml                # Packaging metadata
├── requirements.txt              # Runtime dependency pins/ranges
├── tests/
│   ├── test_core_regressions.py
│   ├── test_edit_candidates.py
│   ├── test_edit_locator.py
│   └── test_spec_editor.py
└── src/
    ├── __init__.py               # app version string
    ├── gui.py                    # main desktop application + orchestration hooks
    ├── widgets.py                # reusable UI components and report windows
    ├── pipeline.py               # orchestration for review/cross-check/verify
    ├── reviewer.py               # primary Claude review client + parsing
    ├── verifier.py               # verification engine + batch wave logic
    ├── verification_config.py    # verification model/tool constants
    ├── batch.py                  # Message Batches submit/poll/retrieve/cancel
    ├── batch_runtime.py          # bounded poll policy/runtime helpers
    ├── resume_state.py           # serialize/deserialize durable batch state
    ├── extractor.py              # DOCX extraction + paragraph mapping
    ├── preprocessor.py           # local non-LLM alert detection
    ├── tokenizer.py              # token counting and budget checks
    ├── prompts.py                # system/user prompt construction
    ├── cross_checker.py          # cross-spec coordination analysis
    ├── diagnostics.py            # event timeline + phase durations
    ├── report_exporter.py        # formal Word report export
    ├── edit_candidates.py        # finding -> edit-candidate classification
    ├── edit_locator.py           # locating text spans in source docs
    ├── spec_editor.py            # low-level Word edit application
    ├── apply_edits.py            # orchestrated edit-plan execution
    └── code_cycles.py            # cycle definitions + defaults
```

---

## Installation

### Prerequisites

- Python **3.11+**
- A valid **Anthropic API key**
- Desktop environment with Tk support

### Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Environment

Set your Anthropic key before launch:

```bash
export ANTHROPIC_API_KEY="your_key_here"
```

(Windows PowerShell)

```powershell
$env:ANTHROPIC_API_KEY="your_key_here"
```

---

## Running the App

```bash
python main.py
```

`main.py` is intentionally minimal and delegates to `src.gui.main()` so the same entry pattern works cleanly in both local and packaged/PyInstaller contexts.

---

## Typical User Workflow

1. Launch app.
2. Select one or more `.docx` spec files (drag/drop or browser).
3. Select code cycle (2022 or 2025).
4. Add optional project context.
5. Choose mode (real-time or batch).
6. Start review.
7. (Optional) Enable cross-spec check.
8. Let verification complete.
9. Review findings in report window.
10. Export report and/or apply suggested edits.

---

## Data Model Snapshot

Important logical objects in the codebase:

- `ExtractedSpec` — extracted source text + metadata.
- `Finding` — normalized finding payload with severity/action/replacement details.
- `ReviewResult` — parsed findings + token/accounting/error status.
- `BatchJob`/`BatchStatus` — asynchronous batch handles.
- `VerificationResult` — verdict + explanation + evidence URLs.
- `PipelineResult` — end-state output consumed by UI/export.

For field-level details, see `CLAUDE.md`.

---

## Reliability, Safety, and Operational Guards

- Retryable transient connection handling during streaming.
- Bounded polling policies for long-running batch operations.
- Explicit no-progress and elapsed-time ceilings.
- Resume-state snapshots persisted for restart recovery.
- Finding deduplication to suppress duplicate noise.
- Edit conflict resolution for overlapping replacements.

---

## Testing

Run the unit/regression suite:

```bash
pytest -q
```

Focused test sets include:

- edit candidate classification,
- edit location matching,
- spec editing behavior,
- core regression protection for orchestration paths.

---

## Known Constraints

- Input format is currently **`.docx` only**.
- Quality of findings is still bounded by source spec clarity and completeness.
- LLM-assisted QA is an expert-support system, not a substitute for licensed design responsibility.
- API/network availability impacts turnaround for model-dependent phases.

---

## Release / Maintenance Checklist

When cutting a release:

1. Update `src/__init__.py` version.
2. Sync `pyproject.toml` version.
3. Update changelog/release notes.
4. Run tests.
5. Smoke test GUI flow (review + verify + export).

---

## Contributing

If you are modifying behavior, strongly prefer this sequence:

1. Add/adjust tests in `tests/`.
2. Implement in `src/`.
3. Validate with `pytest`.
4. Verify GUI integration points in `src/gui.py` and `src/widgets.py`.
5. Keep `README.md` (user-facing) and `CLAUDE.md` (engineer-facing) synchronized.

---

## Quick File Guide

- Start at **`README.md`** if you need user/ops context.
- Start at **`CLAUDE.md`** if you need architecture and internal contracts.
- Start at **`src/pipeline.py`** if you need orchestration flow.
- Start at **`src/gui.py`** if you need actual runtime UX pathing.
- Start at **`tests/`** if you need expected behavior and guardrails.
