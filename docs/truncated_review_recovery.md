# Truncated per-spec review recovery

## What the transport preserves

Checked against the pinned Anthropic Python SDK **1.11.0** and the Claude API
documentation on 2026-10-05:

- [Stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
  document that `max_tokens` may leave an incomplete `tool_use` block. A tool
  call is not guaranteed: the model can exhaust its budget during thinking.
- [Batch results](https://platform.claude.com/docs/en/api/messages/batches/results)
  return a Message inside each succeeded result. SDK
  [`MessageBatchSucceededResult`](https://github.com/anthropics/anthropic-sdk-python/blob/v1.11.0/src/anthropic/types/messages/message_batch_succeeded_result.py)
  holds that Message, and
  [`ToolUseBlock.input`](https://github.com/anthropics/anthropic-sdk-python/blob/v1.11.0/src/anthropic/types/tool_use_block.py)
  is a dictionary. Present finding objects can be read even when the Message
  stopped at `max_tokens`; the SDK does not reject their tool schema. The
  batch endpoint provides no raw tool-input JSON deltas and does not promise
  that every truncated request returns recoverable input. We retain only
  objects with every declared finding field, then apply the usual semantic
  validation and edit demotion. Missing fields are never defaulted for salvage.
- [Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming)
  expose `input_json_delta.partial_json` and incremental parsed input. SDK
  [`accumulate_event`](https://github.com/anthropics/anthropic-sdk-python/blob/v1.11.0/src/anthropic/lib/streaming/_messages.py)
  assembles those deltas with `jiter.from_json(..., partial_mode=True)`;
  `get_final_message()` preserves the resulting snapshot. That parser may
  expose an unfinished last object, including one with every required field.
  The real-time runner therefore accumulates the public raw deltas as well,
  and salvage reads only finding objects that fully parse with their closing
  brace. It never adds delimiters or finishes a cut-off quote. EX-02 JSON
  responses use the same conservative prefix reader.

Recovered findings retain `parse_status="incomplete"` and an error. Collection
includes them in the findings while keeping the specification in
`failed_review_specs`; the report names the incomplete spec and says how many
findings survived. Refusals and unexpected stop reasons are not salvaged.
SDK-backed offline tests exercise both the batch envelope and the actual
streaming accumulator. No live, billable truncation experiment was run.

## The one repair

Both transports append the same bounded request: at most **20 findings**,
ordered CRITICAL, HIGH, MEDIUM, GRIPES, with each issue at most **40 words**.
All required fields and exact evidence/edit quotes remain required. The JSON
variant uses the same scope without naming a tool. The repair uses **low**
effort through `apply_effort_config`'s existing override and capability gate;
unsupported models omit effort. Primary requests retain their effort policy.
EX-03 varies primary effort only; its audit probes require repairs to keep the
same low effort in both arms and reject other request changes.

Twenty 40-word issue descriptions total at most 800 words before the schema
fields and evidence. This gives the recovery request a bounded focus on the
highest-severity issues and leaves substantial space under the 128k baseline
for exact quotes and reasoning. It is a conservative engineering ceiling,
not a measured optimum or a guaranteed token bound: long verbatim edit fields
can still be large. A repair returning 20 or more findings remains incomplete,
even with a successful stop reason, because the ceiling may hide other issues.
A primary that already yielded at least 20 findings before truncation also
remains incomplete after a bounded repair, even if the repair returns fewer.

The repair preserves the primary's system prompt, tools, thinking configuration,
output mode, and output cap. Only the uncached user suffix and effort change;
the suffix cannot promote a near-threshold primary onto the 300k cap. Findings
from the primary and repair are retained together and use ordinary downstream
deduplication. A failed repair cannot erase findings already paid for.

There is one instructed repair per spec. Real-time returns after it, including
when the repair raises. Batch collection persists and reattaches to that same
repair; even an expired, failed, or canceled saved repair does not authorize
another submission. Incomplete specs continue to appear by name in the report.
