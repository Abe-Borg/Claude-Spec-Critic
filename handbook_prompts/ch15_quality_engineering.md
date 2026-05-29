# Agent Prompt — Chapter 15: Quality Engineering — Testing & Calibration

**Full title:** *Quality Engineering: Testing & Calibration*

## Your mission
Explain how the team keeps a model-driven, network-dependent program *testable
and trustworthy*: the **hermetic** test suite (no API key, no network, runs in
seconds), the fake-Anthropic and in-memory-DOCX fixtures that make it possible,
the map of what each test guards, and the **calibration eval** that scores the
verification/grounding system against curated fixtures as ground truth. The
through-line: tests are where the codebase's invariants are *pinned down* so they
can't silently regress.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts), §7 (calibration eval / fixture / scorer).
2. `CLAUDE.md` — **§9 Test Harness** in full, and `pyproject.toml`'s `[tool.pytest]`
   markers.
3. Source you own:
   - `tests/conftest.py` — the placeholder `ANTHROPIC_API_KEY`, the `network`
     marker skip, GUI-skip-when-no-tkinter.
   - `tests/fixtures/fake_anthropic.py` — the response builders (tool-use,
     JSON-text fallback, `max_tokens` incomplete; `dict_shape=True` for the batch
     retrieval path).
   - The **test files** (~28) — read their names and a few to build a *map* of
     what each guards (e.g. `test_source_grounding_invariant`,
     `test_verified_contested`, `test_budget_exhaustion`,
     `test_deterministic_checks`, `test_diagnostic_banner`,
     `test_dedup_edit_identity`, `test_capability_policy`, `test_token_budgets`,
     `test_prompt_serialization`, `test_tracing`, `test_edit_sidecar`, …). Don't
     document every assertion — group them by subsystem and explain what
     invariant each cluster locks in.
   - `evals/` — `harness.py`, `runner.py`, `fixtures.py`, `baseline.json`.
   - `evals/calibration/` — `harness.py` (note `_apply_budget_exhaustion`),
     `loader.py`, `runner.py`, `scorer.py`, `README.md`, and the `fixtures/*.json`
     (the canonical cases: `tp_confirmed_nfpa13`, `tp_corrected_stale_cbc`,
     `tp_critical_corrected_dsa`, `tp_disputed_invented_section`,
     `tp_local_skip_placeholder`, `tp_unverified_budget_exhausted`,
     `tp_unverified_obscure_product`, `fp_overconfident_numeric_swap`,
     `grounding_downgrade_invented_url`).
4. `pyproject.toml` (markers, testpaths) and the `.github/workflows/tests.yml` CI.

## In scope (what you own)
- **The hermetic philosophy.** Why the suite runs with *no* real key and *no*
  network: speed, determinism, and the ability to test model-shaped behavior
  without paying for or depending on the API. How `conftest.py` injects a
  placeholder key and how `@pytest.mark.network` tests skip unless a real key is
  present; how GUI tests skip when `tkinter` is unavailable.
- **The fixtures that make it possible.** `fake_anthropic.py`'s builders model the
  *shapes* the real API returns — including the awkward ones (truncated
  `max_tokens` responses, JSON-in-text fallback, plain-dict batch-retrieval
  variants). Explain that good fakes of the *failure* shapes are what let the
  resilience code (salvage parser, demotion, reconciliation) be tested at all.
- **The test map.** A subsystem-organized tour of what the suite guards — group
  the ~28 files under the chapters they protect (input/detectors, review/schema/
  serialization, orchestration/dedup, routing, grounding/contested/budget/
  failed-status, report/banner/evidence/sidecar, config/capability/token-budgets,
  tracing). Emphasize that each invariant from `CLAUDE.md` tends to have a test
  that pins it.
- **The calibration eval.** The `evals/calibration/` harness as a *second* kind
  of test: fixtures encode a known finding + a captured verifier response +ground-
  truth expectation; the harness mirrors production grounding/budget logic
  (`_apply_budget_exhaustion`); the **scorer** reports outcomes (and the
  `budget_exhausted_count` in the summary header) so a recheck confirms telemetry
  flows end-to-end. Walk the canonical fixtures as a catalog of the
  behaviors-that-must-hold (a true CONFIRMED, a CORRECTED stale-CBC, a DISPUTED
  invented section, an over-confident numeric swap that must *not* confirm, an
  invented-URL grounding downgrade).
- **CI.** How the hermetic suite runs in `.github/workflows/tests.yml`.

## Explicitly OUT of scope (owned elsewhere)
- The production code under test → its owning chapter (you explain *what the test
  guarantees*, not re-explain the feature).
- The trace silo's byte-identical-summary guarantee → **Ch 14** (reference the
  test that enforces it).

## Narrative beats to hit
- *Testing a non-deterministic dependency.* The central challenge: Claude's output
  isn't fixed, and the network isn't free or reliable in CI. The answer is to test
  the *deterministic seams* — the parsing, routing, grounding, classification,
  rendering — against faithful fakes of the model's output shapes. Tell this as
  the key insight that makes the suite fast and meaningful.
- *Tests as executable invariants.* Many `CLAUDE.md` invariants (grounding gate,
  contested branch order, budget-exhausted non-promotion, dedup text-digest)
  exist as tests; the suite is the enforcement mechanism that keeps refactors
  honest. Note the suite was deliberately *trimmed* (601→448 tests) to the
  essentials (foreshadow Ch 17's "remove what only existed for auto-apply").
- *Calibration vs. unit tests.* Why a separate fixture-scored eval exists: unit
  tests prove the plumbing; calibration proves the *judgment* (grounding
  downgrades, false-positive resistance) behaves on realistic cases.

## Invariants & facts you MUST get right
- Hermetic by default; placeholder key in `conftest`; `network` tests skip
  without a real key; GUI tests skip without `tkinter`.
- Markers: `token_budget`, `prompt_serialization`, `network`.
- `fake_anthropic.py` covers tool-use, JSON-text fallback, `max_tokens`
  incomplete, and `dict_shape=True` batch variants.
- The calibration scorer surfaces `budget_exhausted_count`;
  `tp_unverified_budget_exhausted` is the canonical exhaustion fixture.

## Diagrams & tables
- A **test-map table**: subsystem/chapter → representative test files → invariant
  guarded.
- A **calibration-fixture catalog table**: fixture → scenario → expected outcome.
- A small diagram: fake response builders → deterministic seam under test →
  asserted invariant.

## Cross-references to make
- To every chapter whose invariants are tested (Ch 4/5/7/9/10/11/12/14), and to
  **Ch 17** (the test-suite trimming / dead-code-removal story).

## Deliverable
- Write to **`handbook/15_quality_engineering.md`**. H1 = the full title. Target
  **3,000–4,500 words**.

## Quality bar
- A reader understands how to test a model-driven app hermetically, what the suite
  guarantees, and how calibration differs from unit testing. The test map and
  fixture catalog match the actual files.
