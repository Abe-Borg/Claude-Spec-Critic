# Review Follow-ups

## Phase D5 — Cross-check dependency filtering audit

- Audited `classify_cross_check_dependencies`; the current ID-based policy is not clearly wrong, so no runtime policy change was made.
- The existing behavior preserves cross-check findings when at least one cited upstream finding still stands, or when independent raw-evidence ids are present, and suppresses only when every cited upstream finding is disputed with no independent evidence.
- Follow-up recommendation: continue reducing reliance on the legacy file/section heuristic by requiring stable upstream finding ids and stable paragraph/table-cell evidence ids throughout cross-check prompts, parsers, resume payloads, and reports. Once compliance is proven, consider removing or further downgrading the heuristic fallback path.
