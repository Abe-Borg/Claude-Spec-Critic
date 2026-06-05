# Quality Engineering: Testing & Calibration

The hardest thing to test in Spec Critic is the one thing it is built around. The
review pass, the cross-check, the verification verdicts — every interesting output
flows from a call to a large language model, and a large language model is, by
construction, not a pure function. The same spec submitted twice can come back
with the findings phrased differently, a verdict softened, a citation in a new
order. On top of that, the calls cost real money and ride a network that is
neither free nor reliable inside a CI runner. A test suite that depended on any of
that would be slow, flaky, expensive, and — worst of all for a compliance tool —
unable to run on a contributor's laptop without a funded API key.

So Spec Critic does not test the model. It tests the *seams around* the model: the
parsing that turns a tool-use block into a `Finding`, the routing that decides
whether a finding is worth checking, the grounding that downgrades an
unsubstantiated verdict, the classification that assigns a trust label, the
rendering that puts it all in a Word report. Those seams are deterministic. Given a
fixed model output, they always produce the same result — and a fixed model output
is something you can fake. That single insight is what makes the suite fast (a few
seconds), hermetic (no key, no network), and meaningful (it asserts the invariants
that matter).

This chapter is about how that works, and about a second, complementary kind of
test. The unit suite proves the *plumbing* is sound — that a malformed edit demotes,
that an ungrounded CONFIRMED downgrades, that the cache refuses to persist a transient
failure. The **calibration eval** asks a different question entirely: when the
pipeline emits a verdict, *is it right?* The throughline of this whole handbook is
trust, and trust is something you have to be able to measure. Tests are where the
codebase's invariants stop being prose in `CLAUDE.md` and become executable
guarantees that a refactor cannot quietly break.

## The hermetic contract

Hermetic is the load-bearing word. The suite must run with no real `ANTHROPIC_API_KEY`
and no outbound network, and it must run in seconds. Three small mechanisms in
`tests/conftest.py` enforce that, and they are worth understanding because they are
the foundation everything else stands on.

**A sentinel key, injected before collection.** Production modules read the API key
at import time — `reviewer._get_api_key` raises if it is missing, which would crash
collection before a single test ran. `pytest_configure` sets a placeholder with
`os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")`. Two
deliberate choices hide in that one line. `setdefault` means a developer who *does*
have a real key in their environment keeps it (so the opt-in network tests can run).
And the placeholder is *obviously* fake, so if some code path ever does reach the
network by mistake, it earns an immediate `401` rather than silently authenticating
against — and billing — whatever account the runner happens to be near.

**Network tests skip unless a real key is present.** `pytest_collection_modifyitems`
walks every collected item and, unless it finds a real (non-sentinel) key, attaches a
skip marker to anything tagged `@pytest.mark.network`. This is the escape hatch for a
test that genuinely wants to call the live API. Notably, *no test in the suite
currently carries that marker* — the suite is fully hermetic end to end. The marker
and its skip logic stand ready for a future "does the real SDK still return the shape
we fake?" test, but today every assertion runs offline. That is a feature, not an
oversight: the cost of a real-call test is exactly the slowness and flakiness the
hermetic design exists to avoid, so it is paid only on demand.

**GUI tests skip when `tkinter` is missing.** The desktop shell (see [**Ch 13 — The
Desktop GUI & Its Controller Architecture**](13_gui.md)) imports `tkinter` at module scope, which
is absent in many CI containers. `pytest_ignore_collect` drops a small set of
GUI-dependent files at collection time when `tkinter` can't be imported, so their
import errors never abort the run. There is an honest wrinkle here worth flagging,
because this chapter is about pinning down truth: the set named in `conftest.py`
(`test_core_regressions.py`, `test_gui_refactor_modules.py`) does not exist in the
current tree.[^guifiles] The guard outlived the files it guarded — a small fossil of
the v3.0.0 trimming (foreshadowing [**Ch 17 — Evolution & Lessons: The v3.0.0 Pivot
and the Road Ahead**](17_evolution_and_lessons.md)).

Two more details complete the contract. `pyproject.toml` pins `testpaths = ["tests"]`
and — importantly — `addopts = "-ra --strict-markers"`. Strict markers turn a
*mistyped* marker into a hard error instead of a silent no-op, which keeps the
registered marker set (`token_budget`, `prompt_serialization`, `network`) honest: you
cannot tag a test `@pytest.mark.network` and have it quietly never skip. And CI, in
`.github/workflows/tests.yml`, does exactly one thing on every push to `master` and
every pull request: set up Python 3.11, `pip install -r requirements.txt`, and run
`python -m pytest`. No secrets are configured, because none are needed. The eval
harnesses described later in this chapter are deliberately *not* part of that
workflow — they are developer tools, run by hand.

## Faking the model's shapes

If the suite tests the seams around the model, something has to stand in for the
model's output. That is `tests/fixtures/fake_anthropic.py`, and its design carries
more weight than its size suggests.

The production parsers consume objects that look like the Anthropic SDK's Pydantic
models: attribute access for `message.content`, `message.stop_reason`,
`message.usage`, `content[i].type`, `content[i].name`, `content[i].input`. But the
Message Batches retrieval path (see [**Ch 6 — Batch Processing: The Message Batches
Backbone**](06_batch_processing.md)) can hand back the *same* logical payload as plain dictionaries. A fake
that only modeled one shape would leave the other path untested. So every builder
takes a `dict_shape` flag: pass `dict_shape=False` and you get attribute-accessible
dataclass stand-ins (`FakeMessage`, `FakeToolUseBlock`, `FakeUsage`); pass
`dict_shape=True` and a recursive `_to_dict` flattens the same object into the
dict form the batch path sees. One fixture, both code paths.

The builders cover five cases, and the choice of cases is the whole point — they
include the *awkward* shapes, not just the happy path:

| Builder | Shape it models | What it lets you test |
|---|---|---|
| `review_tool_use_response` | `submit_review_findings` tool call (optionally preceded by prose) | The review happy path: `reviewer._parse_findings` extracts findings from a tool block. |
| `verification_tool_use_response` | `submit_verification_verdict` tool call **plus** a `server_tool_use` (`web_search`) block and a `web_search_tool_result` block | Verdict parsing *and* grounding — the fake supplies real "searched" evidence for the grounding helpers to match against. |
| `verification_text_fallback_response` | JSON-in-text, no tool block | The fallback parser that pulls a verdict out of assistant prose when the model declines the tool. |
| `max_tokens_incomplete_response` | `stop_reason="max_tokens"`, partial text | That parsers degrade `parse_status` to `incomplete` on a truncated response. |
| `batch_*_result` / `batch_errored_result` | The `BatchResult` envelope (`succeeded` / `errored`) with a `custom_id` | The batch collection path, including the errored-request branch. |

The deeper lesson is in the last three rows. **It is the faithful fakes of the
*failure* shapes that make the resilience code testable at all.** A salvage parser
that recovers a verdict from plain text is untestable without a builder that produces
plain text where a tool block should be. A demotion path that fires on a truncated
response is untestable without a `max_tokens` fixture. A batch reconciliation loop
that has to survive an errored item is untestable without an errored envelope. The
production code is full of defensive handling for shapes the real API produces only
occasionally and never on command; `fake_anthropic.py` produces them on demand, every
run.

Notice, too, that `verification_tool_use_response` doesn't just fake a verdict — it
fakes the *evidence*. It emits a `server_tool_use` block (the model asking to search)
and a `web_search_tool_result` block (the URLs that came back), so the grounding
helpers in `verifier.py` have a real searched-source pool to validate citations
against. A fake that returned only the verdict could never exercise the grounding
gate, which is the single most important invariant in the system.

The module also ships two canonical, schema-valid payloads — `sample_review_findings_payload`
and `sample_verification_verdict_payload` — that satisfy the strict tool-use schemas
by construction. One detail there is itself a smuggled invariant: the verification
payload defaults to a *non-empty* `source_quote`, because the verifier demotes a
CONFIRMED/CORRECTED verdict with an empty quote down to UNVERIFIED at parse time. A
fixture that wants to stay grounded has to carry a quote — the fixture's default
encodes the rule.

```
  fake_anthropic builder              deterministic seam               asserted invariant
  ──────────────────────              ──────────────────               ──────────────────
  review_tool_use_response      ──▶  reviewer._parse_findings    ──▶  findings parse; a bad
   (attr- or dict-shaped)              + validate_edit_shape            EDIT demotes to REPORT_ONLY

  verification_tool_use_        ──▶  verifier._apply_source_      ──▶  CONFIRMED with an ungrounded
   response (+ web_search blocks)      grounding / _enforce_…           citation downgrades to UNVERIFIED

  max_tokens_incomplete_        ──▶  batch retrieve /             ──▶  parse_status == "incomplete"
   response                            stop_reason handling

  verification_text_            ──▶  JSON-in-text fallback        ──▶  verdict recovered from prose
   fallback_response                   parser
```

One thing `fake_anthropic.py` does *not* contain is a DOCX builder. Tests that need a
`.docx` build one inline with `python-docx`'s `Document()` — sixteen test modules
import `docx` directly and assemble paragraphs and tables in memory, never touching
disk.[^docxfixtures] The fixtures package exposes exactly one thing,
`fake_anthropic`, and `conftest.py` re-exports it as a top-level fixture so any test
can take `fake_anthropic` as an argument and reach the builders.

## The test map

The suite is 49 test files holding roughly 645 test functions.[^count] Read top to
bottom they look like a grab bag; grouped by the subsystem they protect, they read
as a near one-to-one mirror of the invariants catalogued in `CLAUDE.md`. The table
below maps each cluster to its owning chapter and the contract it locks in.

| Subsystem (owning chapter) | Representative test files | Invariant pinned |
|---|---|---|
| Input & detectors ([**Ch 4**](04_input.md)) | `test_deterministic_checks`, `test_preprocessor_policy`, `test_locally_classified_and_content_loss` | Every deterministic detector fires with a stable `deterministic_rule` id; stale-cycle negation/history phrasing is suppressed; >20% non-text DOCX raises a content-loss warning. |
| Review, schema & serialization ([**Ch 5**](05_review_engine.md)) | `test_prompt_serialization` *(marked)*, `test_parse_time_edit_validation`, `test_source_quote_schema` | Wrapper escaping is injection-proof and the cache-breakpoint prefix is byte-stable; invalid EDIT/DELETE/ADD demote to REPORT_ONLY with a `demotion_reason`; empty-quote supportive verdicts demote at parse time. |
| Orchestration & dedup ([**Ch 7**](07_orchestration.md)) | `test_dedup_edit_identity`, `test_batch_escalation` | `_deduplicate_findings` preserves per-file `occurrence_originals`; real-time and batch escalation share merge/disagreement logic and preserve VERIFIED_CONTESTED. |
| Verification routing ([**Ch 9**](09_verification_routing.md)) | `test_web_fetch`, `test_verified_contested`, `test_locally_classified_and_content_loss` | `web_fetch` is attached only for STANDARD/DEEP modes; `models_disagreed` produces VERIFIED_CONTESTED; the local-skip keyword list is tightened (`formatting` removed, `leed` elevated). |
| Verification grounding & cache ([**Ch 10**](10_verification_grounding.md)) | `test_source_grounding_invariant`, `test_source_quote_schema`, `test_cache_visibility`, `test_verification_cache_serialization` | Supportive verdicts without an accepted citation downgrade; the cache refuses to persist them; cache TTL defaults to 60 days; the persisted/skipped field split covers every `VerificationResult` field. |
| Report, banner, evidence & sidecar ([**Ch 11**](11_trust_model_and_output.md)) | `test_report_status`, `test_diagnostic_banner`, `test_evidence_panel`, `test_edit_sidecar`, `test_unverified_breakdown`, `test_verification_failed_status` | `classify_status`/`classify_edit_action` map verdicts to the nine-label trust model; the diagnostics banner aggregates operational health; the `.edits.json` sidecar carries verdict + status for a downstream applier. |
| Config, capability & tokens ([**Ch 12**](12_configuration_and_models.md)) | `test_capability_policy`, `test_token_budgets` *(marked)*, `test_pinned_standards_editions`, `test_env_flag_overrides`, `test_budget_exhaustion` | Haiku/triage phases never carry a `thinking` key; per-phase output caps clamp to the model ceiling; pinned editions reach the prompts; env flags override defaults; budget exhaustion is a sub-label, never a new status. |
| Tracing & diagnostics ([**Ch 14**](14_observability.md)) | `test_tracing`, `test_verification_token_telemetry` | Trace lifecycle, env-gating, and secret redaction behave; token telemetry round-trips through resume state and the diagnostics summary. |

The marked rows are the two registered markers in action: `@pytest.mark.token_budget`
on `test_token_budgets.py` and `@pytest.mark.prompt_serialization` on
`test_prompt_serialization.py`, so a contributor can run `pytest -m token_budget` to
exercise just the token-accounting regressions.

A few clusters are worth calling out as the canonical examples of "an invariant from
`CLAUDE.md` has a test that pins it." `test_source_grounding_invariant.py` is the
enforcement arm of the grounding invariant: it asserts that a CONFIRMED or CORRECTED
verdict whose cited URLs were never actually searched downgrades to UNVERIFIED, and
that the cache rejects such a verdict at both `put` and load. `test_verified_contested.py`
guards the subtle branch ordering that lets a swapped-in escalation verdict still
classify as VERIFIED_CONTESTED when both verifiers grounded and disagreed.
`test_budget_exhaustion.py` pins the rule that exhausting the search budget is a
*sub-label* on an INSUFFICIENT_EVIDENCE finding, never a promotion to a new top-level
status. And `test_verification_cache_serialization.py` does something quietly clever:
it asserts that the union of the cache's persisted-fields and skipped-fields lists
covers *every* field on `VerificationResult`, so that adding a new field without
deciding whether it should be cached fails a test rather than silently dropping
telemetry. These are not tests of behavior so much as tests of *intent* — they make
the human decision a machine-checked one.

Several of these tests reach for `fake_anthropic` directly: `test_web_fetch`,
`test_tracing`, `test_batch_escalation`, `test_source_quote_schema`, and
`test_verification_token_telemetry` all build response objects from the fake to drive
a parser or telemetry path end to end. The rest assert against the deterministic
helpers directly, no model output required at all.

## Calibration: scoring judgment, not plumbing

The unit suite can tell you the grounding gate *fires*. It cannot tell you whether
the verdict it let through was *correct*. Those are different questions, and the
second one needs a different instrument. That instrument lives in
`evals/calibration/`.

A calibration fixture is a small JSON file with four parts: the **finding** (the same
fields a real `Finding` carries), a **spec-context** slice (a few paragraphs, for a
human reviewer's benefit — it has no effect on scoring), a **captured verifier
response** (exactly what the verifier returned on a real run — verdict, explanation,
cited sources, *and* the URLs `web_search` actually retrieved), and a **ground-truth**
block: a human's judgment of what the verdict *should* have been. The fixture is, in
other words, a frozen real interaction plus an oracle.

The harness (`evals/calibration/harness.py`) is deliberately narrow: **it never calls
the model.** It reconstructs a `VerificationResult` from the captured response, replays
the exact production grounding helpers (`verifier._apply_source_grounding` then
`_enforce_grounding_invariant`), mirrors the production budget-exhaustion detection
(`_apply_budget_exhaustion` — applied *after* grounding, so a downgraded verdict still
picks up the flag when its searches were spent), attaches the result to a real
`Finding`, and asks `report_status.classify_status` what label it would assign. Then
the scorer compares that label and verdict to the oracle. Because it replays captured
output through the real code, it is just as hermetic as the unit suite — the runner
even sets the same sentinel API key before importing production modules.

The `loader.py` is conservative by design: it validates that verdicts and statuses are
spelled from the closed sets, that required keys are present, and that no two fixtures
share an id, raising on any violation so a malformed fixture fails loudly rather than
scoring as a silent pass. The runner's exit codes encode the outcome: `0` if every
fixture matched its oracle, `1` if any fixture's verdict or status disagreed, `2` if
loading failed.

The scorer (`scorer.py`) renders four tables, each chosen to answer a trust question:

1. **Confusion matrix** — rows are the ground-truth verdict, columns are the verdict
   the pipeline emitted *after grounding*, with per-row recall and per-column
   precision. The diagonal is correctness; the off-diagonals are exactly the failure
   modes a tuning pass wants to study.
2. **Per-status accuracy** — for each `ReportStatus` the classifier assigned, how often
   did the fixture's `expected_status` agree?
3. **Confidence calibration** — fixtures bucketed by the model's self-reported
   `confidence` against observed correctness. A well-calibrated model's correctness rate
   sits near each bucket's midpoint; skew reveals over- or under-confidence.
4. **Source-grounding integrity** — the count of supportive verdicts that survived
   *without* an accepted citation. The post-grounding number should always be zero;
   non-zero means the invariant has a hole.

The summary header also reports a `budget_exhausted_count`. That number exists
specifically as an end-to-end telemetry check: the canonical
`tp_unverified_budget_exhausted` fixture spends all seven of its HIGH-severity searches
without grounding, and seeing the count tick to 1 confirms the flag flowed from the
harness's detection, through `FixtureOutcome`, into the scorer's header — proof the
telemetry plumbing is intact.

### The fixture catalog

The nine checked-in fixtures are a catalog of the behaviors that must hold, chosen to
span the verdict space and the routing modes:

| Fixture | Profile / severity | Captured → after grounding | Oracle | What it proves |
|---|---|---|---|---|
| `tp_confirmed_nfpa13` | code_standard / MEDIUM | CONFIRMED → CONFIRMED | VERIFIED_SUPPORTED | A correctly grounded confirmation survives. |
| `tp_corrected_stale_cbc` | california_ahj / HIGH | CORRECTED → CORRECTED | VERIFIED_CONTRADICTED | A grounded stale-cycle correction holds. |
| `tp_critical_corrected_dsa` | california_ahj / CRITICAL | CORRECTED → CORRECTED | VERIFIED_CONTRADICTED | The CRITICAL deep-reasoning (Opus) path grounds an AHJ correction. |
| `tp_disputed_invented_section` | code_standard / HIGH | DISPUTED → DISPUTED | DISPUTED | A real-but-wrong citation produces a clean DISPUTED. |
| `tp_local_skip_placeholder` | internal_coordination / GRIPES | UNVERIFIED (local_skip) → unchanged | LOCALLY_CLASSIFIED | A placeholder is locally skipped — web search adds no signal. |
| `tp_unverified_obscure_product` | manufacturer / MEDIUM | UNVERIFIED → UNVERIFIED | INSUFFICIENT_EVIDENCE | An unfindable model number honestly stays unverified. |
| `tp_unverified_budget_exhausted` | code_standard / HIGH | UNVERIFIED (7/7 searches) → UNVERIFIED | INSUFFICIENT_EVIDENCE | Spending the full budget without grounding sets the exhaustion sub-label, not a new status. |
| `grounding_downgrade_invented_url` | code_standard / HIGH | **CONFIRMED → UNVERIFIED** | UNVERIFIED | The grounding invariant *fires*: a cited URL the search tool never returned is rejected, downgrading the verdict. |
| `fp_overconfident_numeric_swap` | code_standard / HIGH | CORRECTED → CORRECTED | **CONFIRMED (miss)** | The case the pipeline *cannot* catch — a confidently wrong but properly grounded edit. |

The last two rows are the heart of the eval, and they make a point grounding alone
cannot. In `grounding_downgrade_invented_url`, the verifier returned a confident
CONFIRMED citing a plausible-looking SMACNA URL — but that URL was never in the search
results, so `_apply_source_grounding` rejects it and the verdict collapses to
UNVERIFIED. The pipeline gets this right, and the eval records a match. In
`fp_overconfident_numeric_swap`, the verifier "corrected" a 12-foot sprinkler spacing
to 15 feet, citing a *real, grounded* NFPA page that genuinely permits 15 feet for the
hazard class — but the 12-foot value was the project's deliberate design figure, and
the right answer was to CONFIRM it, not edit it. The citation is real. Grounding has
nothing to object to. The pipeline emits CORRECTED, the oracle says CONFIRMED, and the
eval scores it a **miss**.

That miss is not a bug in the eval. It is the eval doing its job. The plan's glossary
states the caveat precisely: *grounding proves the source is real, not that the source
proves the claim.* `fp_overconfident_numeric_swap` is the executable embodiment of that
caveat — the canonical demonstration that a grounded verdict can still be wrong. Its
presence keeps the gap *measured and visible* rather than rationalized away, which is
the entire reason a judgment eval exists separately from the plumbing tests. It also
means the calibration eval is a **scoreboard, not a gate**: with this fixture present,
the runner's exit status is non-zero today, by design.[^bothgreen] Eight of nine
fixtures pass; the ninth is a standing measurement of a limitation the team has chosen
to surface rather than hide.

### The other eval: regression, not judgment

There is a second harness one directory up, `python -m evals.runner`, and it answers the
opposite question. Where calibration asks "is the output *correct*?", the golden-set
regression harness asks "does the parser / detector / grounding helper *still do what it
always did*?" Its nine `GoldenFixture` cases drive the production parsers and detectors
and roll up seven metrics — review recall, false-positive count on clean specs,
duplicate rate, parse-failure rate, edit-proposal validity, citation-acceptance rate,
and sourceless-CONFIRMED survivors — then compares them against a checked-in
`baseline.json`. Drift from the baseline is a *failure* (exit code 2, distinct from a
fixture failure's exit 1), recoverable only by an intentional `--write-baseline`.

The contrast is the clean way to hold the two harnesses in your head. In the regression
harness, **drift is bad** — a changed number means a parser regressed. In the calibration
harness, **drift is good** — a changed number should mean a tuning pass pushed the
pipeline closer to the human oracle. The regression harness has a hard baseline gate; the
calibration harness is a scoreboard you read. Neither is wired into CI; both are run by
hand when a change touches the seams they protect.

## Design tensions & the honest edges

The hermetic design buys speed and determinism, and it pays for them with a real risk:
**the fakes can drift from the API they imitate.** `fake_anthropic.py` hand-models the
SDK's block shapes — `content[i].type`, the `web_search_tool_result` structure, the
`BatchResult` envelope. If Anthropic changes one of those shapes, the fakes keep
returning the old one, the tests stay green, and production breaks against a reality the
suite no longer reflects. The `@pytest.mark.network` escape hatch exists for exactly this
threat — an opt-in test that calls the real API and asserts the shape still matches — but
it is currently unused, so today the mitigation is "a human notices." That is a genuine,
acknowledged gap.

This chapter has also surfaced small instances of docs and code lagging the v3.0.0 trim,
which is fitting for a chapter about pinning down truth. `CLAUDE.md` §9 used to name a
`tests/fixtures/docx_fixtures.py` that does not exist (now corrected — tests build DOCX
inline), while `conftest.py` still guards two GUI test files that are no longer in the
tree. These were residue of the v3.0.0 trimming — when
the surgical-edit / auto-apply stack was removed, a wave of tests that existed only to
guard it went too, and the suite was deliberately pared back to the essentials. The
docs and a guard or two simply lagged the deletions. The full accounting of that pruning —
what went, and why "remove what only existed for auto-apply" was the right call — belongs
to [**Ch 17 — Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead**](17_evolution_and_lessons.md).

The calibration eval's own edge is its size. Nine fixtures is a scoreboard with very few
rows; it can demonstrate that the grounding invariant fires and that the overconfident
numeric swap slips through, but it cannot give a statistically meaningful false-positive
*rate*. The README is candid that a live re-record mode (calling the real verifier to
refresh `captured_verifier_response` as model behavior drifts) and per-category breakdowns
are future work. And because neither eval runs in CI, a judgment regression will not fail a
pull request automatically — catching it depends on a developer running the harness. The
honest framing is that the unit suite is the *gate* and the evals are *instruments* a human
chooses to read.

## How it connects

This chapter is downstream of nearly every other one — it is where their invariants are
enforced. The detectors of [**Ch 4 — Input**](04_input.md), the schemas and serialization of [**Ch 5 — The
Review Engine**](05_review_engine.md), the batch envelope of [**Ch 6 — Batch Processing**](06_batch_processing.md), the dedup and escalation
of [**Ch 7 — Orchestration & State**](07_orchestration.md), the routing of [**Ch 9 — Verification I**](09_verification_routing.md), the grounding
gate and cache of [**Ch 10 — Verification II**](10_verification_grounding.md), the trust labels and sidecar of [**Ch 11 — The
Trust Model & Report Output**](11_trust_model_and_output.md), and the token and capability policy of [**Ch 12 — Configuration,
Models & Token Economics**](12_configuration_and_models.md) each have a test cluster that pins them. The trace lifecycle and
redaction tests belong to [**Ch 14 — Observability: Tracing & Diagnostics**](14_observability.md), including the
byte-identical-summary guarantee that chapter owns. The calibration eval's confusion matrix
and grounding-integrity table are the quantitative cousins of the manual audits in [**Ch 16 —
Trust Under the Microscope: The Audits**](16_trust_under_the_microscope.md). And the story of *why* the suite is the size it is
— the trimming of everything that only existed to gate auto-apply — is [**Ch 17**](17_evolution_and_lessons.md)'s to tell.

## Key takeaways

- **Test the seams, not the model.** Claude's output is non-deterministic and costly; the
  parsing, routing, grounding, classification, and rendering around it are deterministic and
  free. The suite asserts the latter against faithful fakes of the former.
- **Hermetic by construction.** A sentinel key injected in `conftest.py`, a network marker that
  is registered but unused, a `tkinter`-aware collection skip, and `--strict-markers` together
  let the whole suite — 49 files, ~645 tests — run offline in seconds. CI runs only
  `python -m pytest`.
- **Faking the failure shapes is the real value.** `fake_anthropic.py` models truncated,
  fallback, and errored responses (and both the attribute and dict forms) so the resilience
  code — salvage parsing, demotion, batch reconciliation — can be exercised on demand.
- **Tests are executable invariants.** Most `CLAUDE.md` contracts have a test that locks them
  in, down to a cache-serialization test that fails if a new field isn't consciously classified
  as persisted or skipped.
- **Two harnesses, two questions.** The golden-set regression eval guards that the plumbing
  hasn't drifted (baseline gate); the calibration eval scores whether the pipeline's *judgment*
  matches a human oracle (a scoreboard).
- **The eval makes the limit visible.** `fp_overconfident_numeric_swap` is a deliberate,
  standing miss: a grounded verdict that is still wrong, proving that grounding establishes a
  source is *real*, not that it *proves the claim*. Keeping that gap measured rather than hidden
  is the trust discipline in miniature.

---

[^guifiles]: Verified against the working tree: `tests/` contains 49 `test_*.py` files,
    and neither `test_core_regressions.py` nor `test_gui_refactor_modules.py` is among
    them, though `conftest.py`'s `_GUI_DEPENDENT_TESTS` still names both. The skip logic is
    harmless (it ignores files that aren't there) but the reference is stale.

[^docxfixtures]: `CLAUDE.md` §9 used to list "In-memory DOCX builders: `tests/fixtures/docx_fixtures.py`,"
    but no such file exists in the tree and nothing imports it. Tests that need a document build
    one inline with `python-docx`'s `Document()`. Per the handbook's source-over-docs rule the code
    was authoritative here; `CLAUDE.md` §9 has since been corrected to say tests build DOCX inline.

[^count]: Counted as `def test_` definitions across the 49 files in the working tree
    (≈645). pytest's collected item count can differ slightly if any test is parametrized,
    and the figure will move as the suite evolves; treat it as the order of magnitude, not a
    fixed constant. The deliberate reduction from a larger pre-v3.0.0 suite is [**Ch 17**](17_evolution_and_lessons.md)'s story.

[^bothgreen]: This is in mild tension with `evals/calibration/README.md`, which says "both
    [harnesses] should be green before shipping a tuning change." As the fixtures and scorer are
    actually written, `fp_overconfident_numeric_swap` is scored as a miss by construction, so the
    calibration runner returns a non-zero exit today. Since neither eval is a CI gate, nothing is
    blocked by it; the fixture is best understood as a permanent measurement of a known gap. The
    code's behavior, not the README's aspiration, is authoritative.
