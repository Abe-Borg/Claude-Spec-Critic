# CLAUDE.md — Spec Critic v2.1.0

Technical reference for AI-assisted development. This file describes the codebase architecture, module responsibilities, data flow, and conventions so that future sessions can pick up where the last one left off.

## Application Overview

Spec Critic is a Python desktop application that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude AI models. It extracts text from `.docx` and `.pdf` specification files, sends each spec to Claude for independent review, deduplicates findings across specs, optionally runs a cross-spec coordination check, verifies findings via web search, and presents results either in-app or as an exported Word document.

## Module Map

```
src/
├── __init__.py          # Package version (2.0.0)
├── gui.py               # CustomTkinter GUI — all user interaction
├── widgets.py           # Reusable UI components (TokenGauge, FileListPanel, etc.)
├── pipeline.py          # Core orchestration — SINGLE SOURCE OF TRUTH for workflow
├── report_exporter.py   # Word document (.docx) report generation
├── cross_checker.py     # Cross-spec coordination check (Sonnet 4.6)
├── batch.py             # Anthropic Message Batches API wrapper
├── verifier.py          # Web search verification (Sonnet 4.6)
├── extractor.py         # Text extraction from DOCX and PDF files
├── preprocessor.py      # Local LEED/placeholder detection (no API)
├── tokenizer.py         # Token counting with tiktoken (cl100k_base)
├── prompts.py           # System prompt for Claude review
└── reviewer.py          # Anthropic API client with streaming + model constants
```

## Data Flow

```
User selects .docx/.pdf files
         │
         ▼
    extractor.py ─────────────────────────────────────────────────┐
    extract_text(filepath) → ExtractedSpec                        │
    Routes to extract_text_from_docx() or extract_text_from_pdf() │
    based on file extension                                       │
         │                                                        │
         ▼                                                        │
    preprocessor.py                                               │
    preprocess_spec() → LEED alerts, placeholder alerts           │
         │                                                        │
         ▼                                                        │
    tokenizer.py                                                  │
    count_tokens() → per-spec token counts                        │
         │                                                        │
         ├──── Real-time path ────┐     ├──── Batch path ────┐   │
         ▼                        │     ▼                     │   │
    reviewer.py                   │  batch.py                 │   │
    review_single_spec()          │  submit_review_batch()    │   │
    Per-spec API calls            │  Single batch submission  │   │
         │                        │     │                     │   │
         ▼                        │     ▼                     │   │
    pipeline.py                   │  poll_batch()             │   │
    _deduplicate_findings()       │  retrieve_review_results()│   │
         │                        │     │                     │   │
         ├────────────────────────┘     ├─────────────────────┘   │
         ▼                                                        │
    cross_checker.py (optional)                                   │
    run_cross_check() → coordination findings                     │
         │                                                        │
         ▼                                                        │
    verifier.py                                                   │
    verify_findings() or verify_findings_batch()                  │
         │                                                        │
         ▼                                                        │
    PipelineResult                                                │
         │                                                        │
         ├──── View in App ──── widgets.py (ReportWindow)         │
         └──── Export Report ── report_exporter.py (.docx)        │
```

## Key Data Structures

### ExtractedSpec (extractor.py)
```python
@dataclass
class ExtractedSpec:
    filename: str   # e.g. "23 21 13 - Hydronic Piping.docx" or "23 05 00.pdf"
    content: str    # Full text, paragraphs separated by \n\n
    word_count: int  # Approximate (split on whitespace)
```

This is the **universal interface** between extraction and all downstream modules. The pipeline, preprocessor, tokenizer, reviewer, verifier, cross-checker, batch module, and report exporter all work with `ExtractedSpec` — they never need to know whether the source was DOCX or PDF.

### Finding (reviewer.py)
```python
@dataclass
class Finding:
    severity: str        # CRITICAL | HIGH | MEDIUM | GRIPES
    fileName: str
    section: str
    issue: str
    actionType: str      # ADD | EDIT | DELETE
    existingText: str
    replacementText: str
    codeReference: str
    confidence: float    # 0.0–1.0
    verification: Optional[VerificationResult] = None
```

### ReviewResult (reviewer.py)
```python
@dataclass
class ReviewResult:
    findings: list[Finding]
    raw_response: str
    thinking: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str = ""
```

### PipelineResult (pipeline.py)
```python
@dataclass
class PipelineResult:
    review_result: Optional[ReviewResult]
    files_reviewed: list[str]
    leed_alerts: list[dict]
    placeholder_alerts: list[dict]
    cross_check_result: Optional[ReviewResult] = None
```

## Module Details

### extractor.py — Text Extraction (DOCX + PDF)

**Public API:**
- `extract_text(filepath: Path) -> ExtractedSpec` — Format-agnostic dispatcher. Routes to DOCX or PDF based on extension.
- `extract_text_from_docx(filepath: Path) -> ExtractedSpec` — DOCX extraction via python-docx
- `extract_text_from_pdf(filepath: Path) -> ExtractedSpec` — PDF extraction via pymupdf
- `extract_multiple_specs(filepaths: list[Path]) -> list[ExtractedSpec]` — Batch convenience wrapper
- `SUPPORTED_EXTENSIONS: set[str]` — `{".docx", ".pdf"}`

**DOCX extraction:**
- Iterates `doc.element.body` children to preserve document order (paragraphs and tables interleaved correctly)
- Tables flattened to pipe-delimited rows: `"Cell 1 | Cell 2 | Cell 3"`
- Uses python-docx (`Document` class, `Paragraph`, `Table`)

**PDF extraction (v1.9.0, simplified in v2.0.0):**
- Extracts page text via `page.get_text("text")` (pymupdf)
- Table extraction via `find_tables()` removed in v2.0.0 (was duplicating content already captured by `get_text`)
- Only native (text-selectable) PDFs are supported — no OCR
- Scanned PDF detection: if >50% of pages yield fewer than 10 words, a warning is prepended to the content
- pymupdf import is deferred (inside the function) to avoid import errors when pymupdf is not installed

**Key constants:**
- `_MIN_WORDS_PER_PAGE = 10` — threshold for scanned page detection
- `SUPPORTED_EXTENSIONS = {".docx", ".pdf"}`

### pipeline.py — Core Orchestration

**Public API:**
- `run_review()` — Real-time mode (streaming per-spec calls)
- `start_batch_review()` → `BatchSubmission` — Submit batch
- `collect_batch_results(submission)` → `PipelineResult` — Collect after polling

**Internal flow:**
1. `_prepare_specs()` — Extract, preprocess, token-check (shared by real-time and batch)
2. Per-spec review (real-time: `review_single_spec()`, batch: `submit_review_batch()`)
3. `_deduplicate_findings()` — Group by normalized issue + code reference
4. Cross-check (optional): `run_cross_check()` with section headers + findings
5. Verification: `verify_findings()` (real-time) or `verify_findings_batch()` (batch)
6. Return `PipelineResult`

**v2.0.0 changes:**
- GRIPES findings are now verified (no longer excluded from verification)
- Verification wrapped in try/except — failures produce UNVERIFIED verdicts instead of crashing
- Removed dead `_combine_specs()` and `review_specs` import

### gui.py — User Interface

**v2.0.0 changes:**
- Removed dead `ReportPanel` references and report expand/collapse mode
- Widget state snapshotted before threads (thread-safe reads)
- Batch polling bounded (max 300 attempts, 5 consecutive error limit)
- Batch ID validated on resume (must start with "msgbatch_")
- Export cancel/failure falls back to pop-out window instead of losing data
- Combined-total token gate removed (only per-file gating remains)

**Key patterns:**
- All API calls run in `threading.Thread(daemon=True)` to avoid GUI freezing
- Callbacks via `self.after(0, lambda: ...)` for thread-safe UI updates
- Persistent batch state via `save_batch_state()` / `load_batch_state()` in user state directory

### batch.py — Anthropic Batches API

Wraps the Anthropic Message Batches API for both review and verification.

- `submit_review_batch(specs, ...)` → `BatchJob`
- `submit_verification_batch(findings)` → `BatchJob`
- `poll_batch(batch_id)` → `BatchStatus`
- `retrieve_review_results(job, *, model)` → `dict[str, ReviewResult]` (model is required keyword arg)
- `retrieve_verification_results(job, findings, parse_response_fn)` → `list[Finding]`
- `cancel_batch(batch_id)` → `str` (processing status)

### verifier.py — Finding Verification

- `verify_findings(findings, ...)` — Sequential real-time verification (Sonnet 4.6 + web_search tool)
- `verify_findings_batch(findings, ...)` — Batched verification via Batches API (50% savings), with fallback to sequential

Verifies ALL findings (including GRIPES as of v2.0.0). Mutates `finding.verification` in-place.

**Error handling (v1.9.1+):**
- `verify_finding()` has a generic `except Exception` catch-all so unexpected errors produce UNVERIFIED instead of crashing
- `verify_findings()` wraps each individual finding verification in try/except so a single failure cannot abort the remaining findings

**Batch polling (v2.0.0):**
- `verify_findings_batch()` bounded to `max_poll_attempts=240` with consecutive error tracking
- Falls back to sequential verification after 5 consecutive poll errors or timeout

### cross_checker.py — Cross-Spec Coordination

- `run_cross_check(specs, findings, ...)` → `ReviewResult` with coordination findings
- Uses Sonnet 4.6 with enriched condensed input (section headers + key numeric values + cross-references + existing findings)
- Returns empty result if <2 specs or input exceeds token limit
- Header pattern tightened in v2.0.0 (excludes body text with "shall"/"must"/etc.)

### report_exporter.py — Word Document Export

- `export_report(result: PipelineResult, output_path, ...)` — Generates formatted `.docx`
- Accepts same `PipelineResult` as in-app rendering
- Word-native formatting: heading styles, Table Grid, List Bullet, color-coded severity
- Findings sorted by severity then confidence

### reviewer.py — API Client

- `review_single_spec(spec_content, filename, ...)` → `ReviewResult`
- Model constants: `MODEL_OPUS_46`, `MODEL_SONNET_46`, `REVIEW_MODELS` (label→ID map)
- JSON parsing: `<FINDINGS_JSON>` sentinel tags with heuristic fallback
- Field validation in `_parse_findings()`: severity must be valid, actionType defaults to EDIT, text fields coerced to str (v2.0.0)

### preprocessor.py — Local Detection

- `preprocess_spec(content, filename)` → `PreprocessResult` with LEED alerts and placeholder alerts
- No API calls — pure regex/string matching
- Runs before review to avoid wasting tokens on locally-detectable issues

### tokenizer.py — Token Counting

- `count_tokens(text)` → `int` using tiktoken cl100k_base
- `RECOMMENDED_MAX = 150_000` — per-call token limit
- `PER_CALL_PADDING = 200` — overhead padding for message framing
- `exceeds_per_call_limit(spec_tokens, overhead_tokens)` → `bool` — shared check used by GUI and pipeline

### prompts.py — System Prompt

- `get_system_prompt()` → `str` — Returns the full system prompt for Claude review
- California K-12 DSA focused, mechanical and plumbing scope
- Instructs JSON output with `<FINDINGS_JSON>` sentinel tags

## Conventions

- **Version**: Bump in `__init__.py`, `pyproject.toml`, `CLAUDE.md`, `README.md`
- **Imports**: Relative within `src/` package (e.g., `from .extractor import ...`)
- **GUI threading**: All API/IO work in daemon threads, UI updates via `self.after(0, ...)`
- **Error handling**: Per-spec errors collected in `errors` list, partial results still returned
- **Config/state files**: Stored in OS-appropriate directories via `platformdirs`
- **Dependencies**: Listed in `pyproject.toml` `[project.dependencies]`

## Dependencies

```
anthropic          # Claude API client
python-docx        # DOCX text extraction + report export
pymupdf            # PDF page-text extraction only (no OCR, no table reconstruction)
tiktoken           # Token counting
customtkinter      # GUI framework
platformdirs       # OS-appropriate config/state directories
```

## Future Development Notes

- **OCR support**: Not currently supported. Scanned PDFs are detected and warned about. Adding OCR would require `pytesseract` or similar and would be a separate feature.
- **PDF form fields**: Not extracted. If specs use fillable PDF forms, the form field content won't be captured.
- **PDF annotations/comments**: Not extracted. Only the main page text content is processed.
- **Encrypted PDFs**: Not supported. pymupdf will raise an error which is caught and reported as a ValueError.