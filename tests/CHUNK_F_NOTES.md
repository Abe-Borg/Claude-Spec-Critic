# Chunk F Implementation Notes

## Goal

Prevent wrong-span replacements when the editor's recorded
`EditLocation.match_start` / `match_end` are stale. The previous
revalidation helper (`_precondition_holds_for_paragraph`) accepted an
edit when the expected text appeared uniquely *somewhere* in the live
paragraph, but the calling code then replaced the slice at the
**original** offsets. If the live text had shifted, that meant
overwriting the wrong span.

## What was already in place

The audit Section 8.3 work landed a basic precondition revalidation:

- `_precondition_holds_for_paragraph` confirmed text presence before
  mutation.
- Both the paragraph EDIT path and the table-cell EDIT path called the
  precondition before `_replace_in_paragraph`.
- The whole-paragraph DELETE path also revalidated.
- `apply_edits_to_spec` already re-snapshotted the body before each apply
  pass and applied edits in a deterministic order
  (in-place replacements → ADDs → whole-paragraph DELETEs in descending
  body_index).

What was missing for Chunk F:

1. The fallback "single substring presence" branch returned `True` but
   not the corrected offsets. The caller still passed the stale
   `action.location.match_start` / `match_end` to `_replace_in_paragraph`,
   so the replacement landed on the stale slice.
2. There was no rejection of duplicated-text cases — the precondition
   silently succeeded only when count was exactly 1 in the recorded
   branch and at the fallback, but there was no explicit "ambiguous"
   classification distinguishing duplicates from missing text.
3. `_resolve_cell_and_offsets` picked the first `paragraph.text.find()`
   hit inside the target cell with no uniqueness check; if the expected
   text appeared in two cell paragraphs (or twice in one), it silently
   guessed the first.

## What changed

### `src/spec_editor.py`

- Added a frozen `PreconditionResult` dataclass carrying
  `(ok, match_start, match_end, detail)`. The match offsets returned by
  the helper are now the offsets the caller should use for the actual
  mutation, so a corrected-offset branch is no longer ambiguous.
- Rewrote `_precondition_holds_for_paragraph` to a 3-branch contract:
  1. Recorded offsets still slice out the expected text → return them.
  2. Expected text uniquely present at a different offset → return the
     corrected offsets.
  3. Expected text missing or appearing more than once → return
     `ok=False` with a clear diagnostic. The caller skips the edit
     instead of guessing.
- Updated all three call sites
  (`apply_edits_to_spec` paragraph EDIT, table-cell EDIT, whole-paragraph
  DELETE) to feed `precondition.match_start` / `precondition.match_end`
  to `_replace_in_paragraph` rather than the stale
  `action.location.match_start` / `action.location.match_end`.
- Tightened `_resolve_cell_and_offsets`:
  - Enumerate every occurrence of the expected text across all
    paragraphs of the target cell.
  - Require exactly one. Multiple occurrences return a "skipped" status;
    zero occurrences also return "skipped" rather than "failed" since
    that's a deliberate refusal to apply rather than a data shape error.
  - Boundary / shape problems still return "failed".
- Added a `status` field to the `_resolve_cell_and_offsets` return so
  the caller can record `EditOutcome.status="skipped"` for the safety
  refusals.

### `tests/test_chunk_f_offset_safety.py`

13 new regression tests:

- **Direct precondition unit tests** (6): recorded-offsets accepted,
  shifted-earlier corrected, shifted-later corrected, duplicate skipped,
  missing skipped, empty-expected-text skipped.
- **End-to-end tests through `apply_edits_to_spec`** (5):
  - Stale offsets + unique target → replacement lands on the live span,
    not the stale slice. This is the test that proves the bug from the
    plan ("paragraph.count(text) == 1 fallback then replaces wrong
    span") cannot recur.
  - Stale offsets + duplicated target → skip with "appears N times"
    detail.
  - Stale offsets + missing target → skip with "no longer present"
    detail.
  - Two non-conflicting same-paragraph edits both apply (regression
    guard for the descending-start ordering).
  - Sanity: a fresh, unambiguous edit still applies via the happy path.
- **Table-cell tests** (2): mirror the corrected-offsets and duplicate
  cases for the table-cell code path.

All 30 pre-existing edit tests
(`tests/test_spec_editor.py` + `tests/test_phase4_safe_edit.py`) pass
unchanged. Full suite: 479 passing.

## Tradeoffs and deferred work

- The duplicate-target end-to-end scenario is constructed with a single
  edit whose recorded offsets don't match the live text (e.g. the
  locator was computed against an older extraction). In normal pipeline
  operation, multi-edit-in-one-paragraph cases sort by descending
  `match_start`, so the higher-offset edit runs first and lower-offset
  edits' offsets are not shifted by it. The bug therefore manifests
  primarily when offsets are stale for *external* reasons
  (locator/extract divergence, the doc has been edited since the
  locator ran, etc.) — but the safety net now catches all of those
  classes of staleness uniformly.
- Whole-paragraph DELETE still removes the entire paragraph element
  regardless of the corrected offsets returned by the precondition,
  because deletion is by element rather than by offset. The
  precondition's role there is presence-revalidation only; the
  offset-correction return is harmless on that path.
- The cell resolver still does a linear `cell.text` scan to attribute
  the row-coordinate `match_start` to a specific cell. That heuristic
  is unchanged because the row-coordinate translation is an
  approximation upstream of Chunk F's scope. The post-cell uniqueness
  check is the new safety layer.
- No production behavior change for happy-path edits. Edits that the
  previous implementation would have silently mis-applied on stale
  offsets now either land correctly (unique target) or are skipped with
  a diagnostic the user can act on. Callers reading `EditReport`
  outcomes don't need to change; the new skips appear with
  `status="skipped"` exactly like the existing audit-8.3 skips.
