# Drawings feature — handoff for Session 2 (and beyond)

Audience: the coding agent picking up the drawing-reading feature. You have the
full codebase; this doc gives you the *why*, the *what's done*, the *next goal*,
and the invariants so you don't have to reverse-engineer the design decisions.

---

## 0. TL;DR

- **Session 1 (merged, PR #266) built the engine**: `src/drawings/` turns
  construction drawing PDFs into a **structured text digest**. It runs standalone
  today via its own GUI (`python -m src.drawings`).
- **Session 2 (your job) is integration**: get that digest into the spec
  reviewer's **Project Context** so review + cross-check + verification all "see"
  the drawings — as text. This is deliberately small because the digest is text.
- **After Session 2 the MVP is complete.** Session 3+ are *optional* enhancements
  (parallelism, caching, synthesis, cost-confirm, license swap). None block use.

---

## 1. Why this architecture (the load-bearing idea)

Claude reads drawings via vision, but the Sonnet cross-check/verification phases
cap images at 1568px (Opus is 2576px), and shipping page-images to every phase
multiplies cost and fights the prompt-cache/resume invariants. So we chose
**"Option A": read each drawing once with Opus 4.8 at high resolution, emit a
TEXT digest, and feed that text to the rest of the pipeline.**

The payoff you inherit: `project_context` is already sent on **every review,
cross-check, AND verification call** (see `src/review/prompts.py`,
`src/cross_check/cross_checker.py`, `src/verification/verifier.py`; and the
`PROJECT_CONTEXT_MAX_TOKENS` note in `src/core/tokenizer.py`). So putting the
digest into `project_context` makes **all** phases drawing-aware at text cost,
sidestepping the Sonnet image cap entirely. That is exactly the "each subagent
understands the drawings" goal the owner asked for.

Because the digest is plain text, three things you might fear are non-issues:
- **Token budget**: it counts against the existing `PROJECT_CONTEXT_MAX_TOKENS`
  (100k) cap like any other context text — reuse the existing cap check.
- **Batch resume**: `src/orchestration/batch_resume.py` already persists
  `project_context` text verbatim. The digest rides along automatically — **do
  not special-case drawings in resume** (owner: "leave batch resume as is").
- **Prompt-cache prefix stability**: the digest is computed once and frozen into
  `project_context` before submission, so the cached prefix stays byte-stable.

---

## 2. What Session 1 shipped (the engine you call)

Package `src/drawings/` (PyMuPDF is AGPL-3.0 and is isolated in `render.py` only):

| Module | Role |
|---|---|
| `models.py` | dependency-free `SheetRef` / `ImageTile` / `RenderedSheet` |
| `tiling.py` | dependency-free geometry: 6×6 clip rects + overlap, vision-cap-aware long-edge target, render zoom |
| `render.py` | PyMuPDF rasterization — **the only file importing the PDF backend** |
| `digest.py` | one sheet → one Opus 4.8 vision request → structured text |
| `pipeline.py` | orchestration: PDFs → sheets → digests → combined text |
| `gui.py`, `__main__.py` | standalone CustomTkinter window |

**The one call you need:**

```python
from src.drawings import extract_drawing_context, DrawingContext

ctx: DrawingContext = extract_drawing_context(
    pdf_paths,            # list[Path]; each PDF page = one sheet
    client=None,          # injectable Anthropic client (None → shared factory)
    progress=callback,    # optional: progress(done:int, total:int, label:str)
)
ctx.combined_text         # ← the digest (markdown text) to inject into Project Context
ctx.sheet_count, ctx.ok_sheet_count, ctx.errors
ctx.total_input_tokens, ctx.total_output_tokens, ctx.total_image_token_estimate
```

Behavior to rely on:
- **Page = sheet.** Multi-sheet PDFs are split page-by-page; several PDFs flatten
  into one ordered sheet list. Verified: E-size sheet → 36 tiles at ~273 DPI, 37
  images, ~153k image tokens/sheet.
- **One request per sheet**, processed **sequentially** (each sheet independent).
- **Discipline is auto-detected** by the model from the title block — there is no
  discipline selector, and you should not add one.
- **Errors are captured per sheet** (`SheetDigest.error`, surfaced in
  `ctx.errors` and inline in `combined_text`); a bad sheet never aborts the run.
- Also available: `estimate_image_tokens_for_set(...)` for a pre-run budget
  preview, and `src.core.tokenizer.estimate_image_tokens(w, h, *, model)`.

Caveat to remember: the digest pass is **slow** (a vision call per sheet, with
adaptive thinking) — minutes for a large set. The standalone GUI runs it on a
worker thread with progress. **You must do the same in the main app** (do NOT run
it inline like `attach_context_files` does for fast .docx/.pdf text extraction).

---

## 3. Session 2 — implementation plan

### 3.1 One decision to confirm with the owner first

How should the digest reach Project Context?

- **(a) Integrated button** — an "Attach Drawings…" action in the main app's
  Project Context area that runs `extract_drawing_context` inline (threaded) and
  injects the digest. Best one-app UX.
- **(b) Minimal** — accept `.md`/`.txt` as context attachments so the digest the
  *standalone tool* already saves can be attached. Trivial; honors the owner's
  "separate tool" preference.
- **(c) Both** *(recommended)* — (b) is ~5 lines and (a) is the real UX. They
  share the same sink (`project_context` text).

The owner previously valued keeping the main app lean (the reason the engine is a
separate subsystem). Confirm (a) vs (b) vs (c) before building (a).

### 3.2 Tasks

1. **Accept `.md`/`.txt` context attachments** *(small)*
   - `src/input/extractor.py`: add `.md` / `.txt` to `CONTEXT_ATTACHMENT_EXTENSIONS`
     and a branch in `extract_context_text` that reads the file as UTF-8 text.
   - `src/gui/context_controller.py`: add the extensions to `_CONTEXT_FILETYPES`.
   - Test in `tests/` (mirror existing context-extraction tests).

2. **Integrated "Attach Drawings…"** *(medium — only if decision is (a)/(c))*
   - In `src/gui/context_controller.py` (the Project Context owner), add a
     function mirroring `attach_context_files`, but:
     - file picker restricted to `*.pdf`;
     - call `extract_drawing_context` on a **worker thread**, marshaling
       `progress` back to the UI via `app.after(...)` (see the standalone
       `src/drawings/gui.py` for the exact threading/progress pattern — reuse it,
       don't reinvent);
     - merge `ctx.combined_text` into Project Context via `set_context_text`,
       wrapped in clear delimiters like the existing
       `--- BEGIN ATTACHMENT … --- / --- END ATTACHMENT … ---`;
     - **enforce `PROJECT_CONTEXT_MAX_TOKENS`** using the same
       `count_tokens` + `messagebox.showerror` pattern `attach_context_files`
       already uses; refuse (don't truncate) if the merged context exceeds the cap.
   - Wire the button into the Project Context UI in `src/gui/gui.py` (next to the
     existing "Attach Files…" / modal controls).
   - Surface errors: if `ctx.errors` is non-empty, show a warning listing the
     failed sheets (the digest of the good sheets still merges).

3. **Token-budget interaction** — confirm the merged (typed + file + drawing)
   context is checked against `PROJECT_CONTEXT_MAX_TOKENS`. A large set's digest
   (~1–1.5k tokens/sheet) is usually well under 100k, but a very large set could
   approach it — fail clearly, and consider showing the sheet/token count.

4. **Docs** — update `CLAUDE.md` (the input/context section) to note that drawing
   digests feed `project_context`, and that the standalone analyzer
   (`python -m src.drawings`) exists. Keep the PyMuPDF-AGPL note discoverable.

### 3.3 Invariants you must not break (from `CLAUDE.md`)

- `PROJECT_CONTEXT_MAX_TOKENS` (100k) is a hard cap — enforce on the *merged*
  context, refuse over-cap (existing pattern).
- **Do not special-case drawings in `batch_resume`** — the digest is
  `project_context` text and is already persisted/re-used correctly.
- **Prompt-cache prefix stability** — the digest must be frozen into
  `project_context` before submission; never introduce per-call variability.
- **Do not change the cross-check model/binding** — it receives the digest as
  text via `project_context`.
- **Reuse `src.drawings`** — never duplicate render/tile/digest logic in the GUI.
- **Hermetic tests** — integration tests must not hit the network or require
  PyMuPDF. Monkeypatch `extract_drawing_context` (or inject a fake
  `DrawingContext`) rather than rendering real PDFs in the GUI/integration tests.
  (The engine's own tests already cover real rendering and skip when PyMuPDF is
  absent — see `tests/test_drawing_*.py`.)

### 3.4 Acceptance criteria

- From the main app, the owner can add drawings and see the digest appended to
  Project Context, under the token cap, without freezing the UI.
- A `.md`/`.txt` digest saved by the standalone tool can be attached as context.
- Review / cross-check / verification requests include the digest text (verify it
  lands in `project_context`).
- Full hermetic suite passes (`python -m pytest -q`); no regressions.

---

## 4. Are there sessions after Session 2?

**Session 2 completes the MVP.** Everything after is optional polish, each
independently shippable — schedule per the owner's appetite:

- **Parallel / Batch digest** — the engine processes sheets sequentially. Each
  sheet is independent, so wrap the per-sheet step in a thread pool, or route it
  through the **Message Batches API** (already used elsewhere in this repo) for
  ~2× throughput and 50% cost. Highest-value follow-up for large sets.
- **Digest caching** — cache per-(PDF page) by content fingerprint, mirroring
  `src/input/extraction_cache.py`, so re-running a set doesn't re-pay for vision.
- **Cross-sheet synthesis pass** — one final cheap text call reconciling the
  per-sheet digests into a coherent *set* summary (equipment on M-101 referenced
  by the schedule on M-501, etc.).
- **Cost-estimate confirm dialog** — show the estimated image-token cost
  (`estimate_image_tokens_for_set`) and confirm before a large/expensive run.
- **Streaming the digest call** — for robustness if per-sheet output grows.
- **License swap** — if the desktop app is distributed commercially, replace
  PyMuPDF (AGPL-3.0) with pypdfium2 + Pillow. Contained to `render.py` by design.
- **Quality knobs** — finer/adaptive tiling or ROI zoom for ultra-dense sheets;
  tune `effort` / `use_thinking` / `max_tokens` in `digest.py`.

---

## 5. Orientation / quick start for the next agent

- Run the standalone tool to see the engine output first: `python -m src.drawings`
  (needs `tkinter`; on headless, call `extract_drawing_context` directly).
- Engine entry points: `from src.drawings import extract_drawing_context,
  DrawingContext, SheetDigest, estimate_image_tokens_for_set`.
- Integrate at: `src/gui/context_controller.py` (Project Context owner),
  `src/input/extractor.py` (context attachment extraction),
  `src/core/tokenizer.py` (`PROJECT_CONTEXT_MAX_TOKENS`, `estimate_image_tokens`).
- Tests: `python -m pytest -q` (hermetic; ~750+ tests). Engine tests:
  `tests/test_drawing_tiling.py`, `tests/test_drawing_tokens.py`,
  `tests/test_drawing_digest.py`.
- Env: `pip install -r requirements.txt` (now includes `pymupdf`). `tkinter` is a
  system package; GUI test files skip when it's absent.
- Read in `CLAUDE.md`: the input/context section, "Prompt-cache breakpoint
  stability", batch resume (section 1), and "Token Budgets".
