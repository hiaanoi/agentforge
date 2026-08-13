# ADR 0010: Treat Multi-Tool Responses as Provider Contract Deviations

## Status

Accepted for Milestone 4.

## Context

OpenAI Responses returned multiple `function_call` items during live validation even though the
request set `parallel_tool_calls=False`. AgentForge deliberately supports one tool action per
logical Runtime step, and future tools may have approvals or side effects.

Provider output is untrusted. Request options cannot serve as the Runtime security boundary.

## Decision

Use two explicit policies:

- `STRICT`: execute no call, persist a sanitized MODEL_PROVIDER_DEVIATION, fail the physical model
  attempt with MODEL_PROTOCOL_ERROR, and apply the existing bounded model retry policy.
- `SEQUENTIAL_READ_ONLY`: allowed only when every returned call names a registered local READ tool
  that does not require approval and the response stays within the configured call-count limit.

SEQUENTIAL_READ_ONLY parses and returns only the first call. Arguments and call IDs from discarded
calls do not cross the Provider boundary. Runtime executes the selected call, emits
MULTI_TOOL_RESPONSE_NORMALIZED, and adds protected context stating that other calls were not
executed and must not be assumed complete.

The default configuration is SEQUENTIAL_READ_ONLY with conditional STRICT fallback.
`max_function_calls_per_response` defaults to 8.

## Audit and persistence

Deviation events may contain provider/model/request identifiers, counts, policy, reason, selected
tool name, and up to ten bounded discarded tool names. They never contain arguments, provider
output, file content, or API keys.

Checkpoints contain only the selected ToolCall/ToolResult, count-only normalization context, and
sanitized Provider metadata. Discarded tool names remain audit-only.

## Consequences

- The Runtime remains deterministic and single-action.
- Discarded calls consume no tool budget and create no tool or approval lifecycle.
- STRICT responses can recover from a transient Provider deviation through persisted retries.
- Completing a multi-tool intent may require extra model turns.
- WRITE, DANGEROUS, approval-required, unknown, and non-local calls remain STRICT by default when
  those capabilities are introduced.

## Deferred

- Executing Provider batches.
- Parallel tools.
- Persisting discarded calls.
- Multi-tool side-effect transactions.

