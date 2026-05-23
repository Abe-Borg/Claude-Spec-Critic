# Calibration eval

A hand-labeled, hermetic eval that measures how often the Spec Critic
verifier + classifier pipeline emits the verdict / status / edit-action
a human reviewer agrees with. The goal is to replace "I think AUTO_EDIT
at 0.7 is about right" intuitions with measured false-positive rates.

This is the foundation for Chunks 2 – 13: every later tuning decision
should re-run this eval and report the delta.

## How to run

```bash
python -m evals.calibration.runner            # markdown report on stdout
python -m evals.calibration.runner --json     # JSON for downstream tools
python -m evals.calibration.runner --output report.md   # also write to a file
```

Exit codes:

| code | meaning |
|---|---|
| 0 | every fixture's verdict / status / edit-action matched ground truth |
| 1 | at least one fixture's classifier output disagreed with ground truth |
| 2 | fixture loading failed (bad JSON, duplicate id, missing required key) |

The runner is hermetic — it sets a sentinel `ANTHROPIC_API_KEY` before
importing production modules, so CI doesn't need real credentials.

## How to add a fixture

Drop a new `.json` file under `fixtures/` with the schema below. Use the
existing fixtures as templates — they cover several outcome shapes.

```json
{
  "fixture_id": "tp_corrected_stale_cbc",
  "category": "california_ahj",
  "severity": "HIGH",
  "description": "Short human-readable summary of the case.",

  "finding": {
    "severity": "HIGH",
    "fileName": "23 21 13 - Hydronic Piping.docx",
    "section": "1.01",
    "issue": "Specification cites 2019 CBC; current cycle is 2025.",
    "actionType": "EDIT",
    "existingText": "2019 CBC",
    "replacementText": "2025 CBC",
    "codeReference": "CBC 2025",
    "confidence": 0.9,
    "anchorText": null,
    "insertPosition": null,
    "evidenceElementId": "p3"
  },

  "spec_context": {
    "filename": "23 21 13 - Hydronic Piping.docx",
    "cycle_label": "California 2025",
    "paragraph_map_slice": [
      {"index": 3, "id": "p3", "text": "A. Comply with 2019 CBC ..."}
    ]
  },

  "captured_verifier_response": {
    "verdict": "CORRECTED",
    "explanation": "DSA adopted 2025 CBC ...",
    "sources": ["https://www.dgs.ca.gov/DSA/..."],
    "correction": "2025 CBC",
    "confidence": 0.95,
    "model_used": "claude-sonnet-4-6",
    "verification_mode": "standard_reasoning",
    "verification_profile": "california_ahj",
    "web_search_requests": 3,
    "successful_source_count": 3,
    "search_error_count": 0,
    "searched_urls": ["https://www.dgs.ca.gov/DSA/...", "..."],
    "grounded": true,
    "cache_status": "miss"
  },

  "ground_truth": {
    "correct_verdict": "CORRECTED",
    "correct_correction_text": "2025 CBC",
    "expected_status": "VERIFIED_CONTRADICTED",
    "expected_edit_action": "AUTO_EDIT_CANDIDATE",
    "notes": "Pithy reasoning so a future reader understands the label."
  }
}
```

### Required keys

- `fixture_id` — unique slug; use the file stem for readability.
- `category` — verification profile (`california_ahj`, `code_standard`,
  `manufacturer`, `constructability`, `internal_coordination`). Drives
  per-category breakdown when you eventually slice the scorer output.
- `severity` — one of `CRITICAL` / `HIGH` / `MEDIUM` / `GRIPES`.
  Bucketing severity matters because the verifier's search budget and
  routing both depend on it.
- `finding` — every field on `src.review.reviewer.Finding` that affects
  classification (`severity`, `fileName`, `section`, `issue`,
  `actionType`, `existingText`, `replacementText`, `codeReference`,
  `confidence`). Nullable fields may be omitted.
- `spec_context` — `filename` is required; `cycle_label` and
  `paragraph_map_slice` are for human reviewers and have no effect on
  the score.
- `captured_verifier_response.verdict` — one of `CONFIRMED`, `CORRECTED`,
  `DISPUTED`, `UNVERIFIED`. Everything else on the response is
  optional but should be filled in from a real run when possible.
- `ground_truth.correct_verdict` — one of the four verdict values.
  This is the oracle.

### Optional ground-truth keys

- `correct_correction_text` — what the corrected text *should* have been
  when `correct_verdict == "CORRECTED"`. Future scorer additions can
  compare it to the model's `correction`.
- `expected_status` — what `report_status.classify_status` should return.
  When set, contributes to the per-status accuracy table.
- `expected_edit_action` — what `report_status.classify_edit_action`
  should return. When set, contributes to fixture pass/fail signal.
- `notes` — anything that explains the label to a future reader.

### Source-grounding labels

To exercise the grounding invariant, populate
`captured_verifier_response.sources` with URLs the model *cited* and
`captured_verifier_response.searched_urls` with URLs the web_search tool
*actually returned*. A cited URL that does not appear in
`searched_urls` (after URL normalization) is rejected as ungrounded;
when every cited URL is rejected, the grounded verdict downgrades to
UNVERIFIED.

To replay a real failure mode, capture both lists from the run that
produced the fixture. The `grounding_downgrade_invented_url.json`
fixture is the canonical example.

### Budget-exhaustion labels

The harness automatically applies the Chunk 13 budget-exhaustion
detection after grounding: when a fixture's
`captured_verifier_response.web_search_requests` reaches the
severity-tiered budget (CRITICAL=8 / HIGH=7 / MEDIUM=5 / GRIPES=3)
AND the grounded verdict is `UNVERIFIED`, the result picks up
`VerificationResult.budget_exhausted=True`. Fixtures don't need to
label the flag explicitly — set the search count to the budget and
the harness mirrors what production would surface. The
`tp_unverified_budget_exhausted.json` fixture is the canonical
example (HIGH-severity DSA bulletin lookup that consumed all 7
searches without grounding).

## How to interpret the report

The markdown report has six sections:

1. **Summary header** — total fixtures, verdict accuracy rate, pass /
   fail count, **budget-exhausted findings count** (Chunk 13 — counts
   fixtures whose `web_search_requests` reached the severity budget
   with an UNVERIFIED grounded verdict).
2. **Confusion matrix** — rows are ground-truth verdict; columns are
   the verdict the pipeline emitted after grounding. The diagonal is
   correctness; off-diagonals are the failure modes you want to study.
   Per-row recall and per-column precision call out which verdicts the
   pipeline tends to over- or under-emit.
3. **Per-status accuracy** — for each `ReportStatus` the pipeline
   assigned, how often did the fixture's `expected_status` agree?
   "Assigned" is the count for that status in any fixture; "expected"
   is the count of fixtures that asked for that status. Precision and
   recall fall out of the two counts.
4. **False-positive auto-edit rate at thresholds 0.70 / 0.80 / 0.85 /
   0.90** — at each threshold, count fixtures the pipeline would
   auto-edit (supportive status + threshold met) and split them by
   whether the ground-truth verdict matched the pipeline's verdict.
   The FP rate is the single number tuning chunks should optimize
   for. A 0% FP rate at 0.7 means the floor is well-placed; a non-zero
   FP rate at 0.9 means even the most confident auto-edits are wrong
   sometimes.
5. **Confidence calibration** — fixtures bucketed by
   `finding.confidence` and reported with their observed correctness
   rate. A well-calibrated model has correctness rates near the
   bucket midpoint. Skew indicates over- or under-confidence.
6. **Source-grounding integrity** — count of CONFIRMED/CORRECTED
   verdicts that survived (or were caught by) the grounding invariant.
   The "final without accepted citation" count should always be 0;
   non-zero means the invariant has a hole and needs a fix.

### What the fixture pass/fail signal means

A fixture *passes* when:

- the post-grounding verdict matches `ground_truth.correct_verdict`,
- AND (if `expected_status` was provided) the classifier assigned that
  status,
- AND (if `expected_edit_action` was provided) the classifier picked
  that edit action.

A fixture fails when any of those checks disagree. The runner's exit
code is 1 in that case, and the markdown report enumerates the failing
fixtures with their specific issues so you can re-label or fix the
production code.

## Calibration vs. regression

The Chunk 12 harness at `python -m evals.runner` is a *regression*
suite: it asks "do the parser / locator / unsafe-markup detector keep
returning exactly the same outputs they always have?" Numerical drift
is a failure signal there.

This Chunk 1 harness is a *calibration* suite: it asks "are the
pipeline's outputs *correct* against a human label?" Drift here is
intentional — every later chunk should push numbers in a better
direction (lower FP rate, fewer ungrounded CONFIRMED survivors, etc.).
The two harnesses are complementary; both should be green before
shipping a tuning change.

## Future enhancements (out of Chunk 1 scope)

- Live re-record mode that calls the real verifier and overwrites a
  fixture's `captured_verifier_response` so labels stay calibrated to
  current model behavior.
- Per-category and per-severity breakdowns in the scorer (the
  outcomes are already tagged, the rendering just needs sub-tables).
- Cross-run drift comparison against a checked-in baseline (mirroring
  the Chunk 12 runner's `--write-baseline` workflow).
