# Chunk H Implementation Notes

## Goal

Make verification source grounding stricter and more trustworthy by:

1. Validating model-cited sources against the URLs the API actually
   fetched (Directive 1-3).
2. Tracking searched / cited / accepted / rejected sources as four
   distinct concepts (Directive 4).
3. Routing verification by *kind of claim* (verification profile), not
   just by severity, so internal-coordination findings never burn the
   full code-standard search budget and California/AHJ findings get
   more rope than generic constructability checks (Directive 5-7).
4. Surfacing the partition in reports and diagnostics so a downstream
   user can see what was grounded versus what was rejected.

## What was already in place

| Capability | Pre-Chunk H state |
| --- | --- |
| Web-search URL collection | `_collect_search_evidence(message)` returned a flat list of every URL the search tool retrieved. |
| Cited-source extraction | `_verdict_from_tool_use(message)` pulled the model's `sources` list off the `submit_verification_verdict` payload. |
| Grounding gate | `_enforce_grounding_invariant` downgraded `CONFIRMED` / `CORRECTED` to `UNVERIFIED` when `grounded=False`. That gate fired when **zero** web_search blocks succeeded; it did not validate that the model's cited URLs actually appeared in the fetched results. |
| Severity-tiered budget | `web_search_max_uses_for_severity(severity)` (Phase 10) — CRITICAL/HIGH = 7, MEDIUM = 5, GRIPES = 3. No notion of "kind of claim". |
| Public sources list | `VerificationResult.sources` already held only the model's curated citations (the Phase 10 source-trimming work removed the bulk merge of every retrieved URL). |
| Report sources block | One flat `Sources` heading with all of `vr.sources` rendered as bullet-separated URLs. |

The bug Chunk H closes: there was no programmatic check that a URL in
`vr.sources` actually appeared anywhere in the search-tool's
`web_search_tool_result` blocks. The verifier system prompt says
"do not invent URLs," but a model that did invent one would have been
silently accepted and the invented URL would have shown up in both the
final report and the verification cache.

## What this chunk added

### 1. `src/source_grounding.py` — URL normalization + validator

Pure-function module (no I/O, no network):

| Symbol | Purpose |
| --- | --- |
| `normalize_url(url)` | Fold `http`/`https`, lowercase host, drop default ports, drop trailing slash, drop fragment, sort query params, strip well-known tracking params (`utm_*`, `gclid`, `fbclid`, …), strip wrapping angle brackets and cosmetic trailing punctuation. |
| `validate_cited_sources(cited, searched)` | Partition cited URLs into accepted (matched a searched URL after normalization) and rejected (`{"url", "reason"}`). Reasons: `"ungrounded"`, `"malformed"`, `"empty"`. |
| `is_grounded_against_search_results(cited, searched)` | Convenience boolean — True iff at least one cited URL is grounded. |
| `SearchedSource` dataclass | `(url, title, normalized)` record for a retrieved search result. Replaces the flat URL list inside the verifier so reports can show titles. |
| `dedupe_searched_sources(...)` | Collapse equivalent retrieved URLs to a single record while preserving order (first occurrence wins). Accepts records, dicts, or bare strings. |
| `REJECT_UNGROUNDED` / `REJECT_MALFORMED` / `REJECT_EMPTY` | Stable reason sentinels — they appear in reports and the verification cache, so renaming them would invalidate persisted entries. |

The chosen normalization rules are deliberately conservative — only
**tracking** query params are dropped, never semantic ones (`?page=2`
vs `?page=3` must stay distinct). Non-default ports stay attached.
Credentials in the netloc are stripped because no public search result
ever carries them. Bare hosts (`dgs.ca.gov/foo`) are recovered as
`https://dgs.ca.gov/foo` so a sloppy model citation still matches a
real URL.

### 2. `src/verification_profiles.py` — profile classifier + per-profile budgets

| Symbol | Purpose |
| --- | --- |
| `VerificationProfile` | `str` enum: `code_standard`, `california_ahj`, `manufacturer`, `constructability`, `internal_coordination`. |
| `classify_finding_profile(finding)` | Pure-function classifier over `codeReference` + `issue` + `existingText` + `replacementText` + `section`. Decision order is internal-coordination → California → manufacturer → code/standard (or non-empty `codeReference`) → default constructability. |
| `profile_max_uses(profile, severity)` | Per-`(profile, severity)` `max_uses`. Internal-coordination gets the smallest budget (2 / 2 / 1 / 1); California/AHJ gets the largest at CRITICAL (8). Severity is monotonic within profile. |
| `profile_label(profile)` | Pretty label for reports / diagnostics. |
| `profile_priority_domains(profile)` | Per-profile authoritative-source paragraph that can be appended to the verifier system prompt (registered for future use; not currently appended to the live prompt to keep the stable-prefix cache breakpoint intact). |
| `profile_web_search_required(profile)` | False only for `internal_coordination`. |

Classification keyword sets are tuned for the M&P / California K-12 DSA
domain (DSA, HCAI, Title 24 explicit; common manufacturer names
included; LEED / placeholder / typo / duplicate / internal-contradiction
phrases pull a finding into internal-coordination even when severity is
HIGH). The classifier is intentionally cheap so it runs on every
verification request without measurable cost.

### 3. `VerificationResult` extension (`src/verifier.py`)

Five new fields, all backward-compatible (default to empty
list / empty string):

```python
searched_sources: list[str]         # URLs the web_search tool fetched
cited_sources: list[str]            # URLs the model emitted in the verdict
accepted_sources: list[str]         # cited URLs that matched a searched URL
rejected_sources: list[dict]        # [{"url", "reason"}] for ungrounded citations
verification_profile: str           # the VerificationProfile.value used
```

The public `sources` list is replaced with `accepted_sources` so
reports and the cache never echo invented URLs. The pre-Chunk-H
contract that `sources` = "URLs the model cited" is preserved at the
cited level (`cited_sources`), so callers that want the raw list still
have it.

### 4. `_apply_source_grounding` helper (`src/verifier.py`)

Single helper called from both `_run_verification_call` (real-time)
and `_classify_wave_results` (batch waves) right after the canonical
parser produces a verdict. It:

1. Stamps `searched_sources` from the deduped retrieved URLs.
2. Stamps `cited_sources` from the verdict tool's `sources` payload.
3. Runs `validate_cited_sources` and stamps `accepted_sources` +
   `rejected_sources`.
4. Replaces `sources` (the public list) with `accepted_sources`.
5. Downgrades `CONFIRMED` / `CORRECTED` to `UNVERIFIED` if the model
   supplied citations but **every** citation missed the search set.
   The downgrade explanation gets a `(downgraded: model cited sources
   that did not appear in web_search results)` suffix so the report
   reader can tell why.

Verdicts with no citations are not touched here — the pre-existing
`_enforce_grounding_invariant` continues to handle "no citations AND
no searched sources." The two helpers compose: ungrounded with no
citations → invariant downgrade; cited but ungrounded → grounding
helper downgrade.

### 5. Detailed search-evidence collection

`_collect_search_evidence_detailed(message)` returns
`(list[SearchedSource], success_count, error_count)`, preserving the
search-result title alongside the URL. The legacy
`_collect_search_evidence` is now a thin wrapper that drops the
titles, so existing callers (the Phase 3 grounding gate, the legacy
test that grep-checks for the old loop) keep working.

`_maybe_attr(item, name)` lets the helper read fields off both SDK
Pydantic objects and plain dicts (the batch-results path returns
plain dicts after `_content_block_to_plain`).

### 6. Profile-aware tool builder (`src/batch.py`)

`build_verification_tools_for_profile(profile, severity)` is the
profile-aware variant of `build_verification_tools(severity)`. The
verdict tool inclusion still respects
`verification_request_includes_verdict_tool()`, so flipping
`SPEC_CRITIC_STRUCTURED_OUTPUTS=0` has the same effect on both helpers.
`_build_verification_request_params` and `submit_verification_batch`
in `batch.py` now accept an optional `profile` keyword; when supplied
the request routes through the profile-aware builder, and
`request_map[custom_id]["profile"]` is stamped so the wave loop can
thread it into retry / continuation requests.

`verifier._build_retry_request` and `verifier._build_continuation_request`
gained the same optional `profile` keyword. `_classify_wave_results`
re-classifies each finding before scheduling its retry / continuation,
so the second-wave web_search budget stays consistent with the initial
call even if the wave context payload doesn't pre-carry the profile.

### 7. `src/verification_cache.py` round-trip

`_result_to_dict`, `_clone_for_store`, `_clone_for_hit`, and
`VerificationCache.load_from_disk` all carry the new fields. Legacy
entries (pre-Chunk-H) load with safe defaults (`[]` / `""`) so the
disk cache does not need a one-time migration.

### 8. `src/resume_state.py` round-trip

`serialize_verification_result` and `deserialize_verification_result`
emit / accept the new fields. Pre-Chunk-H resume payloads still load
because every new field is read with a default value via `.get(...)`.

### 9. `src/report_exporter.py` rendering

The Word report's per-finding `Sources` heading now splits into two
labelled paragraphs:

- **Accepted sources (cited and found in search results):** green
  bullet-separated list, same color/sizing as before.
- **Rejected sources (cited by the model but not present in
  web_search results):** italic gray URL followed by `[reason]` in red.

Both sections live under the same collapsed-by-default
`<w15:collapsed>` heading so the open-time collapse zone hides
everything until the user expands it. The pre-Chunk-H section is a
strict subset of the new layout (when no rejected sources exist, the
output is byte-identical to the old format), so existing snapshot or
rendering tests aren't perturbed.

### 10. `_local_skip_result` stamps a profile

Locally-skipped findings carry
`verification_profile = "internal_coordination"` so reports and
diagnostics label them consistently with everything that flowed
through the web-verification path.

### 11. Test additions — `tests/test_chunk_h_source_grounding.py`

71 new tests, marked `source_grounding`. The structure mirrors Chunk H
Directive 8:

| Test class | Scope |
| --- | --- |
| `TestNormalizeUrl` (15 tests) | empty/None, http/https fold, trailing-slash collapse, host case, default-port stripping (both 80 and 443), non-default port preserved, fragment dropped, tracking-param stripping, semantic-param preserved, query-param sort, angle-bracket strip, trailing-punctuation strip, credentials strip, malformed input, bare-host recovery. |
| `TestValidateCitedSources` (9 tests) | valid cited URL, unknown cited URL, normalization-equivalent trailing slash, tracking-param difference accepted, semantic-param difference rejected, no web search used, no citations + no search, empty/whitespace citation marked `empty`, duplicate cited URLs collapse. |
| `TestVerificationResultEvidenceFields` (1) | All new fields default safely. |
| `TestApplySourceGrounding` (4) | accepted/rejected partition, all-citations-rejected downgrades CONFIRMED, same for CORRECTED, no-citations leaves verdict untouched, UNVERIFIED not affected. |
| `TestVerificationProfiles` (8) | each profile classifies correctly, California precedence over code-standard, internal precedence over code (even when `codeReference` is set), default to constructability. |
| `TestProfileMaxUses` (5) | internal-coordination smallest, California-AHJ largest at CRITICAL, severity monotonic within profile, unknown severity → MEDIUM row, unknown profile → constructability. |
| `TestProfilePromptGuidance` (2) | each profile distinct, unknown returns empty. |
| `TestProfileLabel` (3) | pretty labels exist, None → "", string round-trip. |
| `TestBuildVerificationToolsForProfile` (5) | profile budget used over severity, verdict tool gated on `SPEC_CRITIC_STRUCTURED_OUTPUTS`, string profile name accepted, None falls back to constructability. |
| `TestDedupeSearchedSources` (3) | trailing-slash collapse, order-preserving + first-wins, mixed input shapes. |
| `TestLocalSkipStampsProfile` (1) | `_local_skip_result()` carries `internal_coordination`. |
| `TestResumeStateRoundTrip` (2) | new fields round-trip, legacy payload still deserializes. |
| `TestVerificationCacheRoundTrip` (1) | full disk-save-then-load preserves new fields. |
| `TestBatchInitialUsesProfileAwareBudget` (2) | batch request uses profile budget when supplied; severity-only callers still work. |
| `TestRetryAndContinuationAcceptProfile` (2) | retry / continuation request builders accept profile and use profile-aware budget. |
| `TestCollectSearchEvidenceDetailed` (3) | URL + title collected, legacy URL-only helper still works, dict shape accepted. |
| `TestBatchWaveIntegration` (3) | end-to-end through `_classify_wave_results`: grounded citation accepted, ungrounded citation downgrades verdict, trailing-slash difference still accepts. |

All 535 pre-Chunk-H tests pass unchanged. Two additional smoke entries
pin the new modules in the import-sanity sweep. New total: 608 passing
(535 baseline + 2 smoke + 71 Chunk H).

## Tradeoffs and decisions

- **Source grounding lives in two helpers, not one.**
  `_enforce_grounding_invariant` (Phase 3) handles "no citations + no
  search succeeded → downgrade." `_apply_source_grounding` (Chunk H)
  handles "citations supplied but every citation is ungrounded →
  downgrade." Composing them keeps each helper's responsibility
  legible. Both run on every verification call, in that order, so a
  result that violates either invariant gets caught.
- **Normalization is conservative.** Only well-known tracking params
  are dropped. Semantic query params, non-default ports, and
  intermediate path segments are preserved. The verifier system prompt
  already asks for authoritative sources; the grounding helper is the
  safety net for the rare case where the model invents a URL or copies
  an outdated link from training data.
- **Profile classification is keyword-based, not LLM-based.** The
  signal in finding text is usually unambiguous, and an LLM call here
  would burn tokens before the verification call itself. A wrong
  classification at worst routes the model to a slightly different
  `max_uses` budget; the grounding invariant is the real safety net.
- **The verifier system prompt is unchanged byte-for-byte.** Profile-
  specific authoritative-source guidance is implemented in
  `profile_priority_domains(profile)` and **registered for future
  use**, but not yet appended to the live system prompt. Appending it
  would invalidate the prompt-cache breakpoint per-profile (it lives
  in the variable section, but the stable prefix tests would need to
  be re-pinned per profile). The grounding validator is sufficient on
  its own; surfacing the per-profile language in the prompt is a
  follow-up.
- **`build_verification_tools_for_profile` lives in `batch.py`, not a
  new module.** `verifier.py` already imports from `batch.py`; adding
  a new module just for this helper would have introduced a circular
  import (verifier → new_module → batch_helpers). The helper is
  exported alongside `build_verification_tools` to make their
  symmetry obvious.
- **Backward compatibility preserved everywhere.** Every new function
  parameter is optional with a safe default; every new
  `VerificationResult` field has a safe default; every new
  serialization key has a `.get(..., default)` reader. A pre-Chunk-H
  resume payload, cache entry, or test fixture deserializes cleanly
  with no migration.
- **`sources` (the public list) is replaced with `accepted_sources`.**
  This is the **only** field-shape change visible to existing report /
  cache callers. The pre-Chunk-H semantics of `sources` ("URLs the
  model cited") still hold at `cited_sources`; what `sources` now
  holds is a strict subset (the *grounded* model citations). Anyone
  relying on `sources` to see the model's exact citation list should
  switch to `cited_sources`; that's a deliberate, audit-visible
  rename.
- **Severity remains a per-profile modifier, not a global multiplier.**
  Plan Directive 7 explicitly says "preserve severity-aware search
  depth, but make it subordinate to verification profile." The
  per-profile budget table satisfies that — within any profile, the
  severity ordering is monotonic; across profiles, internal-coordination
  CRITICAL still has a smaller budget than code_standard GRIPES.

## Risks

- **Sonnet might cite valid URLs that the search tool didn't
  retrieve.** The grounding gate is intentionally strict: a cited URL
  that doesn't appear in the search results is rejected even if it's a
  real authoritative page. In practice the search tool tends to
  retrieve the URL the model wants to cite (the model uses search
  results to build the citation), but operators should watch the
  `verification_evidence.ungrounded` counter to make sure the
  rejection rate stays low. If a future audit shows valid URLs being
  rejected, the threshold inside `_apply_source_grounding` (currently
  "every citation must miss before downgrading") can be loosened to
  "majority must miss" without touching the normalization layer.
- **Profile classification keyword sets will drift over time.** New
  manufacturers, new code sections, and new California authorities
  will need to be added. The classifier is co-located with the
  enum so a single-file edit is enough; the test suite includes one
  test per profile that pins the canonical decision pattern.

## Deferred / out of scope

- **Verification modes and model routing** are Chunk I's job. Chunk H
  routes the *search budget* by profile; Chunk I will route the
  *model* and the *retry policy* by profile.
- **Stable element IDs for finding/edit targeting** are Chunk K's job.
  Source grounding does not need IDs because the matching unit is the
  URL, not the spec text.
- **Per-profile prompt guidance is registered, not yet wired into the
  live system prompt.** Adding it requires re-pinning the
  prompt-cache breakpoint tests per profile and was deferred to keep
  the prompt's stable prefix byte-identical for this chunk.
- **`source_grounding.SearchedSource.title` is collected but not yet
  rendered in the report.** The report sources block still shows URLs
  only. Switching to `[title] (url)` is a one-line change once the
  title-rendering convention is decided.
- **Diagnostics counters for accepted vs rejected.** The diagnostics
  module's `verification_evidence` section still rolls everything
  into grounded / ungrounded. Adding per-profile counters and an
  accepted-vs-rejected split would be a follow-up in the diagnostics
  expansion (Chunk J telemetry).

## How to verify

```
pytest -q                                                  # full suite, 608 pass
pytest -m source_grounding                                  # Chunk H tests only, 71 pass
pytest tests/test_chunk_h_source_grounding.py -q            # same set, explicit path
```

The `source_grounding` pytest marker is registered in `pyproject.toml`.
