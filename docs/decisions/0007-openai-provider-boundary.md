# ADR 0007: Isolate OpenAI Responses Behind ModelProvider

## Status

Accepted for Milestone 4.

## Decision

Use an asynchronous `OpenAIModelProvider` that converts provider-neutral ModelRequest values and
ToolSpecs into OpenAI Responses requests, then maps SDK responses into ModelResponse, ToolCall, or
FinalAnswer domain values. SDK types never cross the adapter boundary.

Requests set `store=False` and `parallel_tool_calls=False`. AgentForge does not use
`previous_response_id`; persisted local context is authoritative. Tool schemas are normalized to
strict function schemas before network access, and call IDs are preserved through tool results.

The provider boundary exposes one action per logical Runtime step. Unexpected multi-call responses
are serialized to the first call; the remaining calls are not executed and only bounded count
metadata crosses the boundary.

## Consequences

- Runtime, persistence, context, and tool policy remain provider-neutral.
- Fake injected clients can cover adapter behavior without network access.
- Additional providers require separate adapters rather than provider conditionals in Runtime.
- OpenAI-specific unsupported schema or protocol shapes fail at the boundary.
- Multi-call fallback is safe but may require an additional provider turn to complete the original
  intent.
