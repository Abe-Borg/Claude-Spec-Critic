# HTML Report + AI Chat — Baseline Evidence (Phases 0–1)

Prepared: 2026-08-04, from a fresh clone of `Abe-Borg/Claude-Spec-Critic` in a remote (headless Linux) session.
Companion note to the chat-delivered handoff "Surgical HTML Report + AI Chat Integration Handoff" (that document is not checked into the repository).

**Status: BASELINE APPROVED** (2026-08-04). The user confirmed: *"the latest master branch is the latest
app"* — the explicit confirmation the handoff's baseline gate requires. See the Approval record below.

---

## Phase 0 — Prototype preserved

| Item | State |
|---|---|
| Prototype branch | `origin/codex/html-report-option` — untouched by this session |
| Prototype commit | `04e1c387b390e324bf2bfb3b68bc02f1e5345c47` ("new shit", 2026-08-03, author Abey) |
| Prototype parent | `e1ad95f519bfcb9833583f333ddbb74fd4ac25c9` (2026-03-05, merge of PR #18) |
| Files in prototype commit | `src/html_report_exporter.py` (+1765), `tests/test_html_report_exporter.py` (+452), `src/gui.py` (±127), `README.md`, `CLAUDE.md` — exactly the five files the handoff records |
| Open PR for prototype | None |
| This session's branch | `claude/html-report-baseline-review-1unr56` (created from `origin/master` tip) |

The prototype branch remains recoverable and is treated as a behavioral reference only.

## Phase 1 — Baseline located and proven

### Candidate baseline

**`origin/master` @ `37c3cbe88f74190e59467fd7440b69805a688572`** (2026-07-27, "Merge pull request #324 …add-trust-information-modal-link").

- Version literals in lockstep: `pyproject.toml` = **3.3.0**, `src/__init__.py` = **3.3.0**.
- Tag `v3.3.0` sits at `c6a8599` (2026-07-21). Master = v3.3.0 release + two small GUI commits:
  PR #323 (fix: activity log staying blank after Clear during a run) and PR #324 (trust/security modal).
- Repository path in this session: `/home/user/Claude-Spec-Critic` (fresh clone, working tree clean before this note).

### The handoff's diagnosis is confirmed

- The March commit `e1ad95f` **is an ancestor of master**, and master is **715 commits ahead** of it.
- The modern application was on GitHub the whole time. The previous session's local clone simply had
  ~5-month-stale remote references, and the prototype was branched from that stale `master`.
- Nothing of the modern app was lost or overwritten by the prototype; it lives on its own branch.

### Full live-branch sweep (all remote refs fetched and inspected)

Only two branches postdate master's tip; neither carries newer application code:

| Branch | Date | Content |
|---|---|---|
| `codex/html-report-option` | 2026-08-03 | The quarantined prototype (excluded by handoff instruction) |
| `claude/beautiful-lovelace-s3l1ky` | 2026-08-03 | **Docs-only** sync of README/CLAUDE.md version labels (open PR #326) |

Every other branch (40+) predates 2026-07-27. Related open PRs, for the user's awareness:

| PR | Branch | Nature |
|---|---|---|
| #326 | `claude/beautiful-lovelace-s3l1ky` | Docs-only version-label sync (supersedes #325) |
| #325 | `claude/beautiful-lovelace-8c9rnk` | Docs-only version-label sync (older duplicate of #326) |
| #320 | `claude/spec-critic-diagnostics-fixes-jgie70` | 2026-07-21 run-audit fixes — **unmerged feature work older than master's tip**; not part of the baseline unless the user says otherwise |
| #319 | `claude/hyperscale-datacenter-architecture-4xz1xt` | 2026-07-20 datacenter-architecture module — superseded (master already contains `src/modules/datacenter_architecture.py`) |

- No HTML report exporter exists anywhere on master (`src/output/report_exporter.py` is the DOCX exporter;
  the only `.html` file is the tracing viewer).
- The checked-in `CLAUDE.md` header still says "v3.0.0" — stale docs that PR #326 fixes; the code is v3.3.0.

### Behavioral proof (this environment)

- **Full hermetic test suite: 1602 passed, 0 failed, 7 skipped** (all 7 are `network`-marked tests that
  require a real `ANTHROPIC_API_KEY`), 12.4 s. Python 3.12 virtualenv, dependencies from
  `requirements.txt`, run under a virtual display (Xvfb) so GUI tests execute rather than skip.
- **Launch proof:** the app was launched headlessly (`main.py` under Xvfb) and stayed alive; a main-window
  screenshot was captured and shared in the session. The rendered window shows the **v3.3.0 footer**, the
  Review-program selector, Attach Files… / Attach Drawings…, the real-time review toggle with the 2/4/6/8
  worker selector, and — decisively — the **"Why Trust It?" header button (PR #324)** and the **Activity-log
  "Clear" control (PR #323)**: the two newest commits on master are visibly present in the running app.
  Startup log was clean.

### Corrections to the handoff's architectural assumptions (v3.3.0 reality)

The handoff was written against the March app and explicitly warns not to assume that structure. Verified
differences that matter for the integration seam:

1. **There is no in-app report window and no output selector** in v3.3.0. "View in App" does not exist.
   The report path is DOCX-only: at run completion, `review_run_controller.on_review_complete`
   (`src/gui/review_run_controller.py:448`) automatically opens the save dialog via
   `report_controller.export_report_to_file(app, result)`, which writes the DOCX report plus the
   edit-instructions and requirements-profile sidecars, returning `"canceled"/"success"/"error"`.
2. **A dormant post-completion seam already exists:** the completed `PipelineResult` is retained on
   `app._last_result` (`src/gui/gui.py:213`, set at `src/gui/review_run_controller.py:398`) and is
   currently consumed by nothing. An additive "Save HTML Report…" control keyed off `_last_result`
   touches zero lifecycle paths (startup, batch, resume, reset, cancellation, automatic DOCX flow all
   byte-identical).

### Environment caveats (honest limits of this proof)

- This is a headless cloud session: no Windows desktop, no visual side-by-side, no real API run.
  Batch submit/resume and real exports were exercised only through the hermetic suite's simulations.
- The user's local machine state is not visible from here. If any local checkout contains uncommitted
  work beyond `origin/master`, the user must say so **before** approving this baseline.

---

## Approval record

- Approved baseline: `origin/master` @ `37c3cbe88f74190e59467fd7440b69805a688572` (v3.3.0 + PRs #323/#324).
- User approval: **GIVEN 2026-08-04** — *"the latest master branch is the latest app"* (after reviewing the
  launch screenshot, test results, and branch sweep in this document).
- PR #320 (run-audit fixes): closed by the user on 2026-08-04 — no longer part of any baseline question.
- Feature branch: `claude/html-report-baseline-review-1unr56`, cut from the approved commit; its only
  pre-feature delta is this evidence document.

---

## Provisional Phase 2 scope (contingent on baseline approval — no edits made)

Proposed file allowlist:

| File | Change |
|---|---|
| `src/output/html_report_exporter.py` | **New.** Standalone exporter; read-only consumer of a completed `PipelineResult`; no imports from pipeline/GUI |
| `tests/test_html_report_exporter.py` | **New.** Content parity, security (escaping/CSP/exact bytes), empty/partial/Unicode/hostile/large fixtures, no-mutation assertion |
| `src/gui/report_controller.py` | Additive sibling `export_html_report_to_file(app, result)` mirroring the existing canceled/success/error contract |
| `src/gui/gui.py` | One additive post-run "Save HTML Report…" control enabled off `_last_result`; no existing control removed, renamed, moved, or restyled |
| `README.md` / `CLAUDE.md` | Docs, only after functionality is accepted |

Denylist unchanged from the handoff (pipeline, prompts, models, reviewer, verifier, extractor,
preprocessor, tokenizer, cross-checker, batch/resume/persistence, main input form, DOCX exporter,
dependency files) — any touch outside the allowlist pauses for approval first.
