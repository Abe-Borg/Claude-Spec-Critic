# Chunk D Implementation Notes

## Goal

Make every verification result path parse structured tool-use verdicts
through a single canonical parser. Before Chunk D, three different
parsing call sites existed and one of them (the legacy text-only batch
parser) actively misclassified the modern `stop_reason="tool_use"` case
as incomplete. The legacy parser had no callers in production code but
remained as dead code that future maintainers could have wired back in.

## Parsing-path inventory (before Chunk D)

| Call site | Status | Stop-reason handling | Parser |
| --- | --- | --- | --- |
| `verifier._run_verification_call` (real-time initial + retries) | Live | Accepts `tool_use` and `end_turn` | Inline: per-message `_verdict_from_tool_use` loop, then `_parse_verification_response` on concatenated text |
| `verifier._classify_wave_results` (batch initial + retry + continuation) | Live | Accepts `tool_use` and `end_turn` | Inline: `_verdict_from_tool_use(message)` then `_parse_verification_response(response_text)` |
| `batch.retrieve_verification_results` | **Dead code, no callers** | Accepts ONLY `end_turn` — would have misclassified `tool_use` as incomplete | Text-only via injected `parse_response_fn`; never read `tool_use` blocks |
| `_verdict_from_tool_use` / `_parse_verification_response` | Live helpers | n/a | Called from both live paths |

The two live paths already had the right stop-reason handling thanks to
earlier work. Chunk D's job was to:

1. Formalize a single canonical parser (Directive 2).
2. Route both live paths through it (Directives 1, 4).
3. Centralize stop-reason classification (Directive 5).
4. Delete the dead legacy parser (Directive 6).
5. Harden normalization across verdict names, source lists, and
   correction fields (Directive 7).
6. Make malformed payloads visibly conservative rather than silently
   trusted (Directive 8).
7. Cover the regression matrix in tests (Directive 9).

## What this chunk added

### 1. `src/verifier.py` — canonical parser

Three new public helpers, deliberately small and composable:

* `classify_verification_stop_reason(stop_reason) -> str`. Returns one of
  `STOP_CLASS_COMPLETE`, `STOP_CLASS_PAUSE`, `STOP_CLASS_INCOMPLETE`.
  `tool_use` and `end_turn` collapse to `complete`; `pause_turn` is its
  own bucket; everything else (including `max_tokens`, `stop_sequence`,
  `None`, and future Anthropic-side additions) degrades safely to
  `incomplete`. Both live paths now branch on the classification rather
  than a hand-rolled allowlist, so future stop reasons can be onboarded
  in one place.
* `parse_verification_response(messages) -> VerificationParseOutcome`.
  The canonical parser. Accepts a single message or a list. Returns a
  `VerificationParseOutcome(verdict, parse_status)` where
  `parse_status` is one of `PARSE_STATUS_STRUCTURED`,
  `PARSE_STATUS_TEXT`, `PARSE_STATUS_TEXT_PARSE_ERROR`,
  `PARSE_STATUS_NO_CONTENT`. Tries the structured
  `submit_verification_verdict` tool input across every message
  (reversed so the most recent verdict wins), then falls back to a
  strict JSON-in-text parse of the concatenated text, then reports
  `no_content` when neither path produced a verdict.
* `_normalize_verdict(value)` and `_normalize_sources(value)`. Extracted
  helpers used by both `_verdict_from_tool_use` and
  `_parse_verification_response` so every code path normalizes the
  same way: unknown verdicts → `UNVERIFIED`; `sources=None` → empty
  list; `sources=str` → one-element list (not character-iterated);
  `sources=dict` → empty list.

### 2. `src/verifier.py` — both live paths routed through canonical parser

* `_run_verification_call` (real-time): the old inline tool-then-text
  loop is gone; `parse_verification_response(all_responses)` handles
  both the per-message structured search and the concatenated text
  fallback. Stop-reason decisions inside the continuation loop now use
  `classify_verification_stop_reason`. The `no_content` branch retains
  the existing "Verification produced no text response." explanation
  and the search-count evidence fields.
* `_classify_wave_results` (batch initial + retry + continuation): the
  old inline parser is gone; the same canonical parser runs on each
  message. `text_parse_error` outcomes become `terminal_unverified`
  with the parse-error explanation as the unverified reason (so the
  retry loop doesn't re-run on a deterministically broken response
  and the result is never cached as a supported verdict).
  `no_content` becomes `terminal_unverified` with the existing
  "Verification produced no text response." reason. Stop-reason
  decisions now use `classify_verification_stop_reason`.

### 3. `src/verifier.py` — `_parse_verification_response` hardened

* Source list normalization through `_normalize_sources`. Previously a
  payload with `sources=None` would crash the text path with
  `TypeError: 'NoneType' object is not iterable`. Now `None`, bare
  strings, dicts, and mixed lists all coerce predictably.
* "No JSON object in text" path now always emits the recognizable
  prefix `"Verification response did not contain structured JSON."`
  (raw text is preserved truncated to 200 chars for debugging). The
  canonical parser's `text_parse_error` detection can match this
  prefix reliably; before the fix, the explanation was the raw text
  itself, and parse failures looked indistinguishable from successful
  text-fallback `UNVERIFIED` verdicts.
* New "JSON was not an object" path: `json.loads` of a top-level array
  or scalar previously bound `data` to a non-dict and `data.get(...)`
  would crash. Now it returns `UNVERIFIED` with an explicit
  parse-error explanation.

### 4. `src/batch.py` — legacy parser deleted

`retrieve_verification_results(job, findings, parse_response_fn)` is
gone. The function was dead code (no callers anywhere in `src/` or
`tests/`) and it pre-dated structured tool use:

* It treated every non-`end_turn` stop reason as incomplete, so a
  modern `tool_use` response would have been silently rejected.
* It never read `tool_use` blocks at all — `parse_response_fn` only
  consumed the concatenated text.

A block comment now marks where the function used to live and explains
why; the related `retrieve_verification_results_detailed` helper that
returns raw batch envelopes (and is actively used by
`_classify_wave_results`) survives unchanged.

### 5. `tests/test_chunk_d_parser_unification.py` — 34 regression tests

Marked with `@pytest.mark.parser_unification` and grouped into the
following classes:

* `TestClassifyVerificationStopReason` — `tool_use`, `end_turn`,
  `pause_turn`, `max_tokens`, and several unknown / `None` stop reasons.
* `TestCanonicalParserStructuredVerdict` — Directive 9 cases 1, 2, 4.
  Real-time response, batch envelope, `end_turn` and `tool_use` stops,
  `CORRECTED` verdict normalization, dict-shape responses (the batch
  retrieval path can return either form).
* `TestCanonicalParserTextFallback` — Directive 9 case 3. JSON text
  fallback, fenced-code-block JSON.
* `TestCanonicalParserErrorCases` — Directive 9 cases 5, 6.
  `max_tokens` truncated text, invalid JSON, empty content, non-JSON
  text, missing-fields tool payload, missing-verdict payload, unknown
  verdict normalization.
* `TestSourceListNormalization` — Directive 9 case 7. `None`, bare
  string, mixed-type list with falsy entries, dict, and the text path
  with `None` sources.
* `TestCanonicalParserMultipleMessages` — pause/continue flow.
  Verdict tool in final message, verdict tool in earlier message,
  text concatenated across messages, empty list, `None` input.
* `TestWaveParserIntegration` — end-to-end through
  `_classify_wave_results`: `tool_use` stop reason now produces
  `success` (the legacy bug); broken JSON becomes
  `terminal_unverified` with the parse-error explanation;
  `max_tokens` becomes `terminal_unverified` with the stop-reason
  explanation.
* `TestLegacyParserRemoved` — repo-wide guard that the legacy
  `batch.retrieve_verification_results` is gone and won't quietly
  return.

### 6. `pyproject.toml`

New marker `parser_unification`.

## Tradeoffs and decisions

### Canonical parser lives in `verifier.py`, not a new module

`verifier.py` already owns `VerificationResult`, `_verdict_from_tool_use`,
`_parse_verification_response`, and `_extract_message_text`. Splitting
the canonical parser into a third module would have created a new
dependency that both `verifier.py` and `batch.py` would import. Keeping
everything in `verifier.py` means `batch.py` doesn't import the parser
at all — wave results flow through `batch.retrieve_verification_results_detailed`
(raw envelopes only) and the parser lives in the same module as the
wave classifier that calls it.

### Parser is content-only; stop-reason classification is separate

`parse_verification_response` deliberately does **not** consult
`stop_reason`. The right action for a given stop reason differs by
path: real-time runs continuations inline (and breaks the
continuation loop on `complete`), the wave path schedules a follow-up
batch wave. Bundling stop-reason logic into the parser would have
forced one of those callers to ignore part of the parser's output.
The two helpers compose: the caller classifies the stop reason, and
only consults the parser when the stop reason is `complete`.

### `VerificationParseOutcome` is a dataclass, not a tuple

A `(verdict, status)` tuple is shorter but obscures the meaning at the
call site. A dataclass also makes it easy to add fields later (e.g. a
parse-time diagnostic) without breaking unpacking at every call site.

### Parse-error explanations are sentinel-matched, not flagged

`_parse_verification_response` already returns `UNVERIFIED` with a
specific explanation when JSON parsing fails. The canonical parser
detects parse errors by matching three explanation prefixes:

* `"not valid json"` (when `json.loads` raises),
* `"did not contain structured json"` (when no `{` is present),
* `"not an object"` (when the JSON value is not a dict).

The alternative — returning a dedicated parse-error result type from
`_parse_verification_response` — would have forced every existing
caller (including tests in `test_chunk_a_fixtures.py` and
`test_remaining_unimplemented.py`) to update. The sentinel approach
preserves the existing API of `_parse_verification_response` while
giving the canonical parser a single point of decision.

### `parse_verification_response` accepts a single message OR a list

The real-time path passes the full `all_responses` list so the parser
can look across pause/continue cycles for the verdict tool. The wave
path passes a single message. Supporting both shapes in one function
keeps the call sites readable (no `[msg]` wrapping at the wave caller).

### Multi-message text concatenation

When no tool block is present in any message, the parser concatenates
the text of every message and tries the JSON fallback once. This lets
the real-time path recover when the model splits its JSON output
across a pause_turn boundary (rare but possible).

### Fallback parsing is preserved

Chunk D directive 8 explicitly says fallback parsing must remain
because thinking-enabled tool use cannot force the model to call the
verdict tool. The canonical parser preserves all three layers
(structured → text → conservative classification) and the text
fallback continues to ship verdicts when the model emits valid JSON
without invoking the tool.

### No behavior change for missing verdict fields

The text path and the tool path both default `verdict` to `UNVERIFIED`
when the field is missing. A payload that omits `verdict` cannot
become CONFIRMED. A payload that ships an out-of-enum verdict
(`MAYBE`, `POSSIBLY`, etc.) is normalized to `UNVERIFIED`. The
existing grounding invariant
(`_enforce_grounding_invariant`) then re-checks every CONFIRMED /
CORRECTED verdict against `grounded=True`, so malformed payloads
cannot become supported findings even if the test suite missed an
edge case.

## Acceptance criteria coverage

| Plan acceptance criterion | Where covered |
| --- | --- |
| There is one canonical verification parser or a clearly shared parser module | `parse_verification_response` in `verifier.py`; `TestCanonicalParserStructuredVerdict`, `TestCanonicalParserTextFallback`, `TestCanonicalParserMultipleMessages` |
| Legacy batch verification parsing does not contradict structured tool-use behavior | `TestLegacyParserRemoved::test_retrieve_verification_results_is_removed` + the `_classify_wave_results` refactor (was already correct for `tool_use`; now routed through the canonical parser) |
| Tests prove that structured verdicts are accepted from all verification paths | `TestCanonicalParserStructuredVerdict::test_structured_tool_use_verdict_from_realtime_response`, `..._from_batch_response`, `TestWaveParserIntegration::test_wave_accepts_tool_use_stop_reason` |
| Parse failures are visible and conservative, not silently converted into supported findings | `TestCanonicalParserErrorCases::test_invalid_json_text_is_text_parse_error`, `..._missing_verdict_field_normalizes_to_unverified`, `..._unknown_verdict_normalizes_to_unverified`, `TestWaveParserIntegration::test_wave_text_parse_error_is_terminal` |

## Deferred / out of scope

* Verification routing changes (Chunk I). Chunk D unifies parsing but
  doesn't change which model handles which finding or when escalation
  fires.
* Source grounding policy (Chunk H). Chunk D preserves the existing
  `_collect_search_evidence` / grounding-invariant behavior unchanged.
  Source-list normalization here is purely defensive against malformed
  model output, not a new grounding rule.
* Real-time vs. wave divergence on `no_content`. The real-time path
  emits `_make_unverified("Verification produced no text response.")`
  with full search-count fields; the wave path emits a
  `terminal_unverified` outcome with the same explanation but no
  evidence stamping (the wave caller doesn't have a
  `VerificationItemOutcome` slot for those fields). This asymmetry
  pre-dates Chunk D and is intentional — the wave-path result is
  later folded into a fresh `VerificationResult` by
  `collect_verification_batch_results`, which is where evidence
  fields get applied for that path.

## How to verify

```
# Full suite (421 tests, hermetic).
python -m pytest -q

# Chunk D regression tests only (34 tests).
python -m pytest -m parser_unification -v

# Or by file:
python -m pytest tests/test_chunk_d_parser_unification.py -v
```
