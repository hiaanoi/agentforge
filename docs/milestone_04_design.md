# Milestone 4 Design: Real Model Adapter and Context Engineering

## Status

Implemented and accepted on `feat/milestone-4`.

## Scope

M4 introduces a real OpenAI Responses adapter and deterministic context reliability while
preserving the single-process, read-only AgentForge security model. It does not add write tools,
shell execution, FastAPI, CLI product surfaces, MCP, LangGraph, multi-agent orchestration, RAG, or
a frontend.

M4 is delivered through two gates:

1. **M4A Real Model Foundation:** model domain, Responses adapter, request attempts, budget,
   provider events, and complete RuntimeSnapshot v2 schema.
2. **M4B Context Reliability:** typed context, tool-result rendering, deterministic compaction,
   exact-repeat loop detection, and restart consistency.

## Current architecture

The M3 provider returns an untyped object which Runtime parses. `MODEL_REQUESTED` and
`MODEL_RESPONDED` are logical-turn events. `Run.current_step/max_steps` already represent logical
model turns. Model request attempts and token totals are not independently persisted. Normal tool
checkpoints are unversioned history dictionaries; approval checkpoints are RuntimeSnapshot v1.

## Model seam

`ModelProvider.generate(ModelRequest) -> ModelResponse` performs exactly one physical provider
request. A provider-neutral `ModelExecutor` owns request-budget reservation, retry, backoff,
attempt events, error normalization, and usage persistence. Runtime continues to own logical turns.

OpenAI SDK types remain inside `OpenAIModelProvider`. The client is asynchronous, injected for
tests, configured with SDK retries disabled, and sends `store=False` and
`parallel_tool_calls=False`.

## Function calling

Tool schemas pass through a strict schema normalizer before becoming Responses function tools.
Unsupported schemas fail before network access. Provider function calls map to an internal
ToolCall carrying a provider-neutral `call_id`; function results return with the same ID.

AgentForge never relies on `previous_response_id`. Local checkpoints are the recovery source.
If a provider returns multiple calls despite `parallel_tool_calls=False`, AgentForge applies the
formal STRICT / SEQUENTIAL_READ_ONLY policy in
`docs/milestone_04_multi_tool_policy_design.md`. Sequential normalization is conditional, not an
implicit first-call default.

## Model state and budget

`Run.current_step/max_steps` remain logical model-turn counters. A new one-to-one
`model_runtime_states` table stores request count, token totals, configured request/token limits,
and policy versions. A `model_attempts` table records physical request lifecycle.

Request count is reserved before network access. Usage totals update only from provider-reported
values. Character and byte estimates are context limits, not token usage. No price or cost is
invented when a versioned price table is absent.

## Events

Existing `MODEL_REQUESTED` remains one logical-turn event. New physical-request events are:

- `MODEL_ATTEMPT_STARTED`
- `MODEL_ATTEMPT_FAILED`
- `MODEL_RETRY_SCHEDULED`
- `MODEL_FAILED`
- `CONTEXT_COMPACTED`
- `LOOP_WARNING`
- `LOOP_DETECTED`
- `BUDGET_EXCEEDED`

Events contain bounded metadata and never complete prompts, outputs, tool content, raw provider
objects, API keys, or unsanitized arguments.

## Context model

RuntimeSnapshot v2 stores typed provider-neutral ContextItems:

- system instruction
- user task
- tool call
- tool result
- approval result
- runtime error
- loop warning
- legacy payload

ContextBuilder injects versioned system instructions, preserves current recovery and error state,
and compacts oldest complete call/result pairs deterministically.

## ToolResultRenderer

Renderers understand current `list_files`, `search_text`, `read_file`, and `get_git_diff` outputs.
They report original/rendered sizes, truncation, SHA-256 digest, and returned count where
applicable. A total count is reported only when the underlying tool supplies one.

## Loop detection

M4 detects only deterministic repetition:

- same action digest;
- same action and result digest;
- same stable error code.

Default warning threshold is 2 and terminal threshold is 3. Different arguments do not match.
Same action with a new result does not immediately terminate. LoopState is persisted in v2.

## RuntimeSnapshot v2

M4A defines the complete v2 shape, including model usage, request count, context state, loop state,
provider metadata, last model error, policy versions, pending tool call with call ID, approval ID,
digest, and resume phase. M4B populates existing fields without changing schema version.

Both ordinary tool checkpoints and approval pause/consumption checkpoints write v2. Approval
resume restores persisted context and loop state; legacy M3 v1 approval snapshots migrate on read.

Migration inputs:

- unversioned M1/M2 history checkpoint -> deterministic legacy/context items;
- M3 v1 approval snapshot -> v2 preserving pending approval state;
- v2 -> strict validation;
- unknown version -> explicit failure.

## Live validation

Offline tests use an injected fake client and keep network blocked. Live tests require explicit
environment opt-in, use only a temporary fixture repository, and are separate from Offline M4
acceptance. An optional manual demo against the current repository requires explicit user consent.

## Known limits

No tokenizer is introduced, so request-time context limits use characters, UTF-8 bytes, and item
counts. A provider request interrupted after network transmission may be billed twice after
recovery; the original attempt remains counted and audited. SQLite request claims target the
current single-process architecture.
