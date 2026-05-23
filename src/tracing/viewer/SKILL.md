# Trace Viewer — design notes

`trace_viewer.html` is a single-file, zero-build replay tool for the JSONL
artifacts the `TraceRecorder` writes to `~/.spec_critic/traces/<run_id>/`.

## Why single-file vanilla JS

- No build step, no `npm install`, no server. A reviewer double-clicks the
  file (or the GUI opens it) and picks a trace folder.
- Tailwind via CDN for styling only — if the CDN is unreachable the layout
  degrades but the data still renders (the tree/table structure is plain
  HTML).
- All parsing is local (`FileReader` via `<input webkitdirectory>`). Nothing
  is uploaded — trace data can contain full spec text.

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
  Chunk 11-13 field. Finding↔span correlation is by `finding_id`
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
