# Spec Critic

**v2.11.0** — AI-assisted M&P specification review for California K-12 DSA projects.

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude. It produces structured findings with severity classifications, confidence scores, web-search-backed verification verdicts, optional cross-spec coordination analysis, and either inline edits or yellow-highlighted suggestion annotations on a copy of each spec.

Configured for the **California 2025 code cycle** by default (`src/code_cycles.py`).

## Design Emphasis

- **Evidence-grounded verification.** `CONFIRMED` / `CORRECTED` verdicts require at least one cited URL that the `web_search` tool actually retrieved.
- **Cost-aware defaults.** Sonnet-default verifier with Opus escalation, optional Haiku triage, severity-tiered + profile-aware search budgets, persistent on-disk claim cache.
- **Robust batch processing.** Durable resume across every pipeline phase with content + source-file SHA-256 digests.
- **Safe Word output.** Id-anchored matching when the model cites a paragraph id; surgical edits gated by safety categories; offset revalidation runs immediately before every mutation. Annotate mode is non-destructive.
- **Trust-model report output.** Every finding renders one of seven `ReportStatus` labels and one of four `EditActionLabel` values so the report makes uncertainty visible.

## Pipeline at a Glance

1. **Text Extraction** — `.docx` paragraphs, tables, headers/footers. Cached by file hash. Each element gets a stable `element_id` (`p7`, `t0r2`, `s1h0`, …).
2. **Local Pre-Screening** — Deterministic detectors run before any API call: LEED, placeholders, template markers, stale/invalid code cycles, empty sections, duplicate headings/paragraphs, inconsistent file naming.
3. **Per-Spec Review** — Each spec sent to Claude Opus 4.7 via the `submit_review_findings` tool. Tagged-JSON text parser as fallback.
4. **Deduplication** — Identical findings consolidated across specs; per-file occurrences tracked separately for multi-file edits.
5. **Cross-Spec Coordination** *(optional)* — Chunked by CSI division (21 / 22 / 23 / Controls / 25 + 01) on large projects. Runs in parallel with verification.
6. **Verification** — Findings routed into one of four modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`). Sonnet 4.6 default; CRITICAL/HIGH `UNVERIFIED` escalates to Opus 4.7. Persistent on-disk cache.
7. **Edit Application** *(optional)* — **Edit mode** applies surgical edits to a copy. **Annotate mode** inserts yellow-highlighted suggestions without mutating the original.

## Processing Modes

- **Real-time** — Immediate processing (streaming API, higher cost).
- **Batch** — Queued at 50% cost savings (~45 min – 2 hrs, 24 hrs max).

Both modes share identical prompts, models, tool schemas, output caps, and parsing logic. The 300k extended-output path is batch-only (`output-300k-2026-03-24` beta header is not honored on streaming) and triggers only for inputs ≥200k tokens.

## Model Stack

Defaults (all overridable via env var; see `api_config.py`):

- Review: Claude Opus 4.7
- Cross-check: Claude Sonnet 4.6
- Verification (initial): Claude Sonnet 4.6
- Verification (escalation / deep-reasoning): Claude Opus 4.7
- Synthesis / Triage: Claude Haiku 4.5

Unknown model ids degrade to safe defaults via `api_config.model_capabilities(...)` — a misconfigured `SPEC_CRITIC_*_MODEL` env var produces a smaller request rather than an API rejection.

## Requirements

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- See `requirements.txt`: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`

## Testing

Test suite is hermetic by default — no API key, no network. `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY`. GUI-dependent tests skip when `tkinter` is unavailable.

```
pytest -q              # full hermetic suite
```

Test markers: `token_budget`, `prompt_serialization`, `network`. Fake Anthropic response builders live in `tests/fixtures/fake_anthropic.py`; in-memory DOCX builders in `tests/fixtures/docx_fixtures.py`.

## Further Reading

- **`CLAUDE.md`** — Engineering reference: source layout, module-level invariants, verification routing tables, feature flag table, test conventions.

## Changelog (recent)

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache at `~/.spec_critic/verification_cache.json` (atomic temp-file + rename; database mode by default, optional TTL pruning via `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`)
- Optional Haiku 4.5 verification triage (`SPEC_CRITIC_HAIKU_TRIAGE=1`); hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible)
- Cross-discipline synthesis model exposed (Haiku 4.5; `SPEC_CRITIC_SYNTHESIS_MODEL` override)
- Severity-tiered web-search budgets: CRITICAL/HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k; `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- Cross-check chunking refined (Div 21 / 22 / 23 / Controls / 25 + 01)

Older changelog entries trimmed; see git history for v2.10.0, v2.8.x, and the non-GUI refactor chunks A–P.
