# Trace Viewer — design notes

`trace_viewer.html` is a single-file, zero-build replay tool for the JSONL
artifacts the `TraceRecorder` writes to `the platformdirs state directory (`%LOCALAPPDATA%\SpecCritic\traces\<run_id>\` on Windows, `~/.local/state/SpecCritic/traces/<run_id>/` on Linux; override with `SPEC_CRITIC_TRACE_DIR`)`.

## Why single-file vanilla JS

- No build step, no `npm install`, no server. A reviewer double-clicks the
  file (or the GUI opens it) and picks a trace folder.
- **Fully offline.** Styling is a hand-written inline `<style>` block of
  utility classes (Tailwind-style names, no CDN, no `<link>`, no `@import`,
  no external `url()`), so opening the file performs zero network requests.
  The page renders local prompts and full spec text — a remote script has no
  business executing on it. `tests/test_trace_viewer_offline.py` pins the
  no-external-reference rule, that every class in use has a rule, and (when
  headless Chromium is available) that the page renders with all network
  requests aborted.
- All parsing is local (`FileReader` via `<input webkitdirectory>`). Nothing
  is uploaded — trace data can contain full spec text.

## Escaping contract

Every trace-derived string reaches the DOM through `esc()`, which escapes
`&`, `<`, `>`, `"` and `'` — it is used in text *and* attribute context
(`title="…"`, `data-*="…"`), so attribute quotes must be covered. No element
carries an inline `on*=` handler; interactive rows use a data attribute plus
a delegated listener (see `lifecycleRow` / `#middlePane`), so a hostile id
in `spans.jsonl` can never become script.

## Data contract (must stay in sync with the recorder)

The viewer reads these files from the selected directory:

| File | Shape it expects |
|---|---|
| `run.json` | object: `run_id, mode, model, cycle_label, files_reviewed[], capture_level, started_at, ended_at` |
| `spans.jsonl` | one span/line: `span_id, parent_span_id, kind, name, started_at, ended_at, status, error, inputs, outputs, metadata` |
| `events.jsonl` | one event/line: `ts, span_id, type, …type-specific fields` |
| `prompts.jsonl` | one/line: `hash, kind, text` (default level only) |
| `findings.jsonl` | one finding/line: serialized `Finding` incl. nested `verification{}` |

Prompt references in span `inputs` are `{ref: hash, kind}` (default) or
`{inline: text}` (deep). `resolvePrompt()` handles both.

## Cross-cutting views

- **By Finding** (primary): finding list → lifecycle (review span that
  produced it → verification spans → grounding) → verdict with every
  verification telemetry field. Finding↔span correlation is by `finding_id`
  (`metadata.finding_id` / `inputs.finding_id` on verification spans;
  `outputs.findings[].finding_id` on review spans). See `spansForFinding`.
- **By Span**: raw `parent_span_id` tree, chronologically sorted, with
  inputs/outputs/metadata + per-span event list.
- **Timeline**: flat event stream filterable by type. Deep traces surface
  `stream_chunk` events here.
- **Search / Grounding**: every `web_search_query`, `grounding_outcome`,
  and the de-duped set of retrieved URLs.

## Color / glyph parity

`STATUS_COLORS` / `STATUS_GLYPHS` / `SEVERITY_COLORS` mirror
`src/output/report_status.py` and `report_exporter.py` so a
`VERIFIED_CONTESTED` finding shows the same ⚡ purple in the viewer and the
Word report. `classifyStatus()` is a JS port of
`report_status.classify_status` — keep the two in sync when the
classification rules change.

## Known limitations

- Spans are written at close, so a span that never closed (crash mid-run)
  won't appear in `spans.jsonl`; its events still show in the Timeline.
- Batch-resume re-emits the pipeline span open; the viewer de-dupes by
  `span_id` (last wins).
- No virtualization — a 50-spec deep trace with tens of thousands of
  `stream_chunk` events will render slowly. Round 3 may add windowing.
