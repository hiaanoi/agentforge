# Milestone 4 Design: Provider Contract Deviation and Multi-Tool Policy

## Status

Approved for implementation on `feat/milestone-4`.

## Problem

An opt-in live OpenAI Responses request returned multiple `function_call` items even though
AgentForge sent `parallel_tool_calls=False`. The Runtime is intentionally single-action per logical
step, so it must not implicitly execute or silently accept an unbounded batch.

Provider output remains untrusted. `parallel_tool_calls=False` is a request constraint, not a
security boundary.

## Decision

Use a hybrid Provider/Runtime boundary:

- the OpenAI adapter counts calls, reads bounded tool names, and classifies them against
  `ModelRequest.tools`;
- STRICT deviations fail inside the Provider boundary so `ModelExecutor` can apply persisted,
  bounded physical-request retries;
- SEQUENTIAL_READ_ONLY returns only the first parsed ToolCall plus sanitized normalization facts;
- discarded arguments never cross the Provider boundary;
- Runtime owns normalization events, context feedback, tool execution, and checkpoints.

## Domain model

Add `MultiToolResponsePolicy`:

- `STRICT`
- `SEQUENTIAL_READ_ONLY`

Add `MultiToolResponseInfo` containing only:

- provider and model;
- provider request ID;
- returned, selected, and discarded counts;
- selected tool name;
- bounded discarded tool names;
- effective policy;
- stable reason;
- `provider_contract_deviation=True`.

`ModelResponse` gains `multi_tool_response: MultiToolResponseInfo | None`.

`ModelProviderConfig` gains:

- `multi_tool_response_policy`, default `SEQUENTIAL_READ_ONLY`;
- `max_function_calls_per_response`, default 8, range 1 through 32.

## Classification

SEQUENTIAL_READ_ONLY is allowed only when every returned function call:

1. names a ToolSpec present in `ModelRequest.tools`;
2. has `source == LOCAL`;
3. has `risk_level == READ`;
4. has `requires_approval == False`;
5. remains within `max_function_calls_per_response`.

Any unknown, non-local, WRITE, DANGEROUS, or approval-required call makes the complete provider
response STRICT. No tool call is selected in that case.

Call plus non-empty final text remains an immediate fail-closed protocol error. It is never
normalized.

## Provider algorithm

1. Validate that output is a list.
2. Count `function_call` items and normalize at most ten tool names for audit.
3. Reject call-plus-final-text ambiguity.
4. For zero calls, return FinalAnswer or the existing empty-response error.
5. For one call, parse it normally.
6. For multiple calls above the configured limit, raise ProviderContractDeviationError.
7. Under STRICT, raise ProviderContractDeviationError.
8. Under SEQUENTIAL_READ_ONLY, classify every name against ToolSpecs.
9. If classification fails, raise ProviderContractDeviationError.
10. Parse only the first call arguments and return it with MultiToolResponseInfo.

Discarded arguments and call IDs are never decoded into domain objects.

## Retry semantics

`ProviderContractDeviationError` is a specialized ModelProtocolError carrying
MultiToolResponseInfo. ModelExecutor:

1. records `MODEL_PROVIDER_DEVIATION`;
2. records the physical attempt as failed with `MODEL_PROTOCOL_ERROR`;
3. marks the error retryable;
4. applies the existing `ModelBudget.max_retries` and persisted retry schedule;
5. raises the final stable ModelRequestError after the last attempt.

Malformed JSON, ambiguous call plus final text, and other ModelProtocolError /
ModelOutputInvalidError cases retain their current non-retryable behavior.

## Runtime normalization

For a successful SEQUENTIAL_READ_ONLY ModelResponse, Runtime:

1. emits `MODEL_PROVIDER_DEVIATION`;
2. emits `MULTI_TOOL_RESPONSE_NORMALIZED`;
3. emits `MODEL_RESPONDED` with sanitized provider metadata;
4. executes only `ModelResponse.action`;
5. after the selected tool result, appends a protected
   `ContextItemKind.MULTI_TOOL_NORMALIZATION`;
6. checkpoints only the selected call/result, normalization context, and sanitized metadata.

The context payload contains counts, the selected tool name, and a fixed instruction that discarded
calls were not executed and must not be assumed complete. Discarded tool names remain audit-only.
The context contains no arguments or tool output.

## Events

Add:

- `MODEL_PROVIDER_DEVIATION`
- `MULTI_TOOL_RESPONSE_NORMALIZED`

Payload fields are limited to provider, model, provider request ID, returned/selected/discarded
counts, selected tool name, bounded discarded tool names, policy, and stable reason.

No event contains complete provider output, API keys, function arguments, or tool output.

## Provider metadata

Normalized responses retain:

- `returned_function_call_count`
- `discarded_function_call_count`
- `multi_tool_policy`
- `provider_contract_deviation`

## Recovery and budgets

Discarded calls:

- do not increment `Run.tool_call_count`;
- emit no TOOL_REQUESTED or TOOL_STARTED;
- create no ApprovalRequest;
- are never pending_tool_call;
- are not stored as ToolCall history;
- cannot be replayed by resume.

RuntimeSnapshot remains schema version 2. The new protected ContextItem and sanitized metadata fit
the existing v2 extension points.

## Live validation

Run the same temporary fixture test three times. Each run records sanitized metrics from persisted
events/state:

- returned, selected, and discarded calls;
- model request count;
- tool call count;
- completion and path citations;
- budget result;
- presence of deviation and normalization events.

The live fixture exposes only local READ tools and never accesses the AgentForge workspace.

## Deferred

- Executing multiple calls from one provider response.
- Parallel tools.
- WRITE, DANGEROUS, or approval-required normalization.
- Provider-native batch recovery.
- Semantic comparison of discarded and later re-requested calls.
