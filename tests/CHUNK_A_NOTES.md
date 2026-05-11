# Chunk A Implementation Notes

## What was already in place

- A working `tests/` directory with 234 passing tests, organized roughly by
  past phase (`test_phase3_verification.py`, `test_phase4_safe_edit.py`,
  `test_phase8_review_modes.py`, etc.).
- Test patterns that monkeypatch `_get_client()` (e.g.
  `test_api_config.py::TestRequestShapeWiring`) for narrow request-shape checks.
- The structured tool extraction helper `extract_tool_use_block` already
  tolerates SDK Pydantic models, plain dicts, and Pydantic-model `input`
  payloads — the fake-response fixtures here exercise both.

## What this chunk added

1. **`pyproject.toml`** — `[tool.pytest.ini_options]` declaring the five test
   markers (`smoke`, `fixtures`, `request_shape`, `slow`, `network`) and pointing
   pytest at the `tests/` directory.
2. **`tests/conftest.py`** — injects a placeholder `ANTHROPIC_API_KEY`,
   skips `@pytest.mark.network` tests when no real key is set, and uses
   `pytest_ignore_collect` to skip GUI-dependent files when `tkinter` is
   unavailable. Without that hook, `test_core_regressions.py` and
   `test_gui_refactor_modules.py` failed collection on headless runners.
3. **`tests/fixtures/fake_anthropic.py`** — the five canonical response cases
   from the plan (structured review, structured verdict, tool_use stop,
   text-JSON fallback, `max_tokens` incomplete), plus batch-envelope
   wrappers, plus a `dict_shape` flag for the plain-dict code paths.
4. **`tests/fixtures/docx_fixtures.py`** — minimal DOCX builders for
   paragraph / table / real-world-section specs. Used by Chunk F (edit
   precondition safety) and later edit work.
5. **`tests/test_chunk_a_smoke.py`** — 34 smoke checks (every non-GUI module
   imports, tool names stay stable, output caps are sane, audit fields are
   present on the canonical objects).
6. **`tests/test_chunk_a_fixtures.py`** — 19 round-trip tests proving each
   fake-response case matches the schema and parses through the production
   helpers.
7. **`tests/test_request_payload_shape.py`** — 22 tests inspecting the
   request kwargs production code passes to the Anthropic SDK. `FakeClient`
   captures `messages.stream`, `messages.batches.create`, and
   `beta.messages.batches.create`. The `fake_client` fixture monkeypatches
   `_get_client` in `reviewer` / `batch` / `verifier` / `cross_checker`.
8. **`README.md` + `CLAUDE.md`** — new "Testing" / "Test Harness" sections.

## Tradeoffs and decisions

- **Stub `tokenizer.count_tokens` in request-shape tests, don't ship a real BPE
  cache.** The lazy tiktoken download fails in sandboxed CI; stubbing also
  keeps test runs deterministic. We patch every module that did
  `from .tokenizer import count_tokens` (`src.batch`, `src.cross_checker`)
  because each captures its own binding at import time.
- **Don't add `jsonschema` as a new dependency.** Chunk A is a baseline,
  not a schema-validation upgrade. The fixture tests do lightweight
  `required` / `type` checks via a local helper. If later chunks need real
  jsonschema validation, add it then.
- **Document Chunk C bugs as `xfail`, don't fix them here.**
  `TestVerifierRetryAndContinuationShape::test_retry_request_includes_verdict_tool_by_default`
  and `…test_continuation_request_includes_verdict_tool_by_default` currently
  fail: `_build_retry_request` / `_build_continuation_request` ship only
  the web-search tool, not the verdict tool. Marked `xfail(strict=False)`
  with a reference to Chunk C; they will flip to XPASS when Chunk C lands.
- **Don't refactor GUI tests.** Two existing test files import `src.gui` at
  module scope, which transitively requires `tkinter`. The plan explicitly
  excludes GUI refactors, so the conftest hook just skips those files on
  headless runners; nothing changed in the test files themselves.

## Deferred / out of scope

- Real golden DOCX fixtures for full report exports (Chunk N would benefit
  but the existing test suite handles in-memory builds well enough today).
- A `network` smoke test that actually hits Anthropic — keep the `network`
  marker reserved; add the first such test only when there's a concrete
  need.
- Replacing `xfail`s with passing assertions — that's the job of Chunk C.

## How to verify

```
pytest -q                              # full suite — 307 pass, 2 xfail (Chunk C)
pytest -m smoke                        # only the smoke checks
pytest -m fixtures                     # only the fixture round-trips
pytest -m request_shape                # only the request-payload tests
pytest tests/test_chunk_a_smoke.py     # one file in isolation
```
