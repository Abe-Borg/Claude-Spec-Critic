# CLAUDE.md — Spec Critic v2.3.0

Technical reference for AI-assisted development. This file describes architecture, module responsibilities, data flow, and conventions for the current codebase.

## Application Overview

Spec Critic is a Python desktop application for reviewing mechanical and plumbing construction specifications for California K-12 DSA projects using Claude. It extracts text from `.docx` files, performs local preprocessing, runs per-spec reviews (real-time or batch), optionally runs cross-spec coordination checks, verifies findings via web search, and presents results in-app or as exported Word reports.

## Module Map

```
src/
├── __init__.py          # Package version (2.3.0)
├── gui.py               # CustomTkinter GUI — all user interaction
├── widgets.py           # Reusable UI components
├── pipeline.py          # Core orchestration and phased batch flow
├── report_exporter.py   # Word document (.docx) report generation
├── cross_checker.py     # Cross-spec coordination check (Opus 4.6)
├── batch.py             # Anthropic Message Batches API wrapper
├── verifier.py          # Web search verification (default Opus 4.6; Sonnet routing opt-in)
├── verification_router.py  # Phase 3: model routing + local pre-classification
├── verification_cache.py   # Phase 3: per-run verdict cache (claim-keyed)
├── extractor.py         # Text extraction (DOCX-only)
├── preprocessor.py      # Local LEED/placeholder detection
├── tokenizer.py         # Token counting and limits
├── prompts.py           # System + user prompt builders
├── reviewer.py          # Anthropic API client with streaming
├── code_cycles.py       # California code cycle definitions (2022, 2025)
└── resume_state.py      # Durable batch resume-state serialization/deserialization
```

## Data Flow

```
User selects .docx files
         │
         ▼
    extractor.py
    extract_text(filepath) → ExtractedSpec
         │
         ▼
    preprocessor.py
    preprocess_spec() → LEED alerts, placeholder alerts
         │
         ▼
    tokenizer.py
    count_tokens() → per-spec token counts
         │
         ├──── Real-time path ────┐     ├──── Batch path ────┐
         ▼                        │     ▼                     │
    reviewer.py                   │  batch.py                 │
    review_single_spec()          │  submit_review_batch()    │
         │                        │     │                     │
         ▼                        │     ▼                     │
    pipeline.py                   │  collect_review_batch_results()
    _deduplicate_findings()       │     │
         ├────────────────────────┘     ├─────────────────────┐
         ▼                              ▼                     │
    run_cross_check_for_batch() or run_cross_check()          │
         │                                                    │
         ▼                                                    │
    verifier.py (sequential or batch)                         │
         │                                                    │
         ▼                                                    │
    finalize_batch_result() / PipelineResult                  │
         │                                                    │
         ├──── View in App ──── widgets.py (ReportWindow)     │
         └──── Export Report ── report_exporter.py (.docx)    │
```

## Key Data Structures

### ExtractedSpec (extractor.py)
```python
@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
```

### Finding (reviewer.py)
```python
@dataclass
class Finding:
    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str
    existingText: str | None
    replacementText: str | None
    codeReference: str | None
    confidence: float = 0.5
    verification: VerificationResult | None = None
    affected_files: list[str] = field(default_factory=list)
```

### ReviewResult (reviewer.py)
```python
@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = MODEL_OPUS_46
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    parse_status: str | None = None
    cross_check_status: str | None = None

    @property
    def critical_count(self) -> int: ...
    @property
    def high_count(self) -> int: ...
    @property
    def medium_count(self) -> int: ...
    @property
    def gripe_count(self) -> int: ...
    @property
    def total_count(self) -> int: ...
```

### BatchSubmission and CollectedBatchState (pipeline.py)
```python
@dataclass
class BatchSubmission:
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    review_request_ids: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = MODEL_OPUS_46
    project_context: str = ""
    prepared_specs: list[ExtractedSpec] | None = None
    cycle_label: str = DEFAULT_CYCLE.label
    cross_check_enabled: bool = False
    export_mode: bool = False

@dataclass
class CollectedBatchState:
    submission: BatchSubmission
    review_result: ReviewResult
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cross_check_skipped_due_to_missing_specs: bool = False
```

## Module Details

### extractor.py — DOCX-only text extraction

**Public API:**
- `extract_text(filepath: Path) -> ExtractedSpec` — dispatcher for supported extensions
- `extract_text_from_docx(filepath: Path) -> ExtractedSpec`
- `extract_multiple_specs(filepaths: list[Path]) -> list[ExtractedSpec]`
- `SUPPORTED_EXTENSIONS = {".docx"}`

Implementation notes:
- Preserves body order by iterating `doc.element.body`
- Flattens DOCX tables into pipe-delimited rows
- No PDF extraction path

### prompts.py — Prompt builders

**Public API:**
- `get_system_prompt(cycle: CodeCycle) -> str`
- `get_single_spec_user_message(spec_content, filename, project_context, *, cycle) -> str`

`get_system_prompt()` is cycle-aware and injects selected California code references from `CodeCycle`.

### code_cycles.py — California Code Cycle Definitions

Defines `CodeCycle` dataclass with edition references (CBC, CMC, CPC, Energy, CALGreen, ASCE 7).

**Public API:**
- `CodeCycle` — frozen dataclass with `label`, `cbc`, `cmc`, `cpc`, `energy_code`, `calgreen`, `asce7`, `asce7_previous`, `cbc_previous`
- `CALIFORNIA_2022`
- `CALIFORNIA_2025`
- `AVAILABLE_CYCLES`
- `DEFAULT_CYCLE` (`CALIFORNIA_2025`)

### resume_state.py — Durable Batch Resume State

Serializes/deserializes pipeline state for crash recovery and app restart resume.

**Public API:**
- Phase constants: `PHASE_REVIEW_POLL`, `PHASE_REVIEW_COLLECT`, `PHASE_VERIFICATION_POLL`, `PHASE_FINALIZE`
- `SUPPORTED_PHASES`
- `build_resume_state(...) -> dict`
- `deserialize_resume_state(payload) -> dict`

Key design:
- Dataclasses round-trip through serializer/deserializer helpers
- `build_resume_state()` stamps version and ISO UTC timestamp
- GUI `load_batch_state()` uses `deserialize_resume_state()` and retains legacy v1 fallback migration logic

### batch.py — Anthropic Batches API

- `submit_review_batch(specs, *, project_context="", model=..., cycle: CodeCycle = DEFAULT_CYCLE) -> BatchJob`
- `poll_batch(batch_id) -> BatchStatus`
- `retrieve_review_results(job, *, model) -> dict[str, ReviewResult]`
- `submit_verification_batch(findings, build_prompt_fn) -> BatchJob`
- `retrieve_verification_results(job, findings, parse_response_fn) -> list[Finding]`
- `cancel_batch(batch_id) -> str`

### cross_checker.py — Cross-spec coordination

- `run_cross_check(specs, existing_findings, *, project_context="", ..., cycle: CodeCycle = DEFAULT_CYCLE) -> ReviewResult`
- Skips with explicit status for <2 specs or over-limit combined input

### verifier.py — Verification

- `verify_findings(findings, *, progress=..., cycle=..., cache=None) -> list[Finding]`
- `verify_findings_batch(findings, *, log=..., progress=..., poll_interval=..., cycle=..., cache=None) -> list[Finding]`
- `verify_finding(finding, *, max_retries=2, cycle=..., model=None, cache=None, escalated=False) -> VerificationResult`
- `prepare_findings_for_verification(findings, *, cycle, cache=None, log=...) -> list[Finding]` — Phase 3 pre-pass that resolves local-skip and cache-hit findings in place; returns the remainder to verify remotely.
- `start_verification_batch(findings, *, cycle, model=None) -> BatchJob`
- `collect_verification_batch_results(job, findings, *, log, progress, poll_interval, cycle, cache=None, realtime_fallback_threshold=None) -> list[Finding]`

`VerificationResult` includes the Phase 3 evidence model (`grounded`, `model_used`, `escalated`, `cache_status`, `web_search_requests`, `successful_source_count`, `search_error_count`). The verifier never marks a verdict CONFIRMED/CORRECTED unless `grounded` is True; ungrounded verdicts are downgraded to UNVERIFIED.

### verification_router.py — Phase 3 routing

- `initial_verification_model()` / `escalation_verification_model()`
- `should_escalate_verification(finding, *, verdict, grounded, ...)` — returns True for CRITICAL/HIGH UNVERIFIED findings when Sonnet is the initial verifier.
- `classify_finding_for_verification(finding) -> "web_required" | "local_skip"` — gated by `SPEC_CRITIC_LOCAL_VERIFICATION_SKIP=1`.

### verification_cache.py — Phase 3 per-run cache

- `VerificationCache.get/put` — keyed by code cycle + actionType + codeReference + claim digest. Only grounded results are cached; cache hits are tagged `cache_status="hit"` so reports show evidence reuse.

### pipeline.py — Orchestration and phased batch APIs

Primary phased batch APIs used by GUI:
- `collect_review_batch_results(submission) -> CollectedBatchState`
- `run_cross_check_for_batch(state, specs, ...) -> CollectedBatchState`
- `prepare_verification_work(state) -> list[Finding]`
- `start_batch_verification(findings, *, cycle, log, progress, cache=None) -> BatchJob | None` — returns `None` when every finding resolved via local-skip / cache hit; callers must skip the collect step in that case.
- `collect_batch_verification_results(job, findings, *, cache=None, ...) -> list[Finding]`
- `finalize_batch_result(state) -> PipelineResult`

Convenience wrapper still available:
- `collect_batch_results(submission, ...) -> PipelineResult`

### report_exporter.py — Word export

- `export_report(result: PipelineResult, output_path, *, project_context="", cross_check_enabled=False, cycle_label="2025")`

### tokenizer.py — Token limits

- `MAX_CONTEXT_TOKENS = 1_000_000`
- `MAX_OUTPUT_TOKENS_OPUS = 128_000`
- `MAX_OUTPUT_TOKENS_SONNET = 64_000`
- `RECOMMENDED_MAX = 500_000`
- `CROSS_CHECK_OVERHEAD = 50_000`
- `CROSS_CHECK_OUTPUT_BUDGET = 128_000`
- `CROSS_CHECK_RECOMMENDED_MAX = 822_000`

### gui.py — Key UX/flow behaviors

- Code cycle selector segmented control (`2022` / `2025`)
- Mode labels: `Real-time (FAST: Expensive!)` and `Batch (SLOW: Cheap!)`
- Real-time cost confirmation dialog with batch-switch option
- Resume state uses `resume_state.py` serializers/deserializers
- File browser filter restricted to `.docx`

## Dependencies

```
anthropic
python-docx
tiktoken
customtkinter
platformdirs
```
