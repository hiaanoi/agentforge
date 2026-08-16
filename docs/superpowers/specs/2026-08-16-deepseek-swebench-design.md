# DeepSeek Provider and Official SWE-bench Canary Design

**Date:** 2026-08-16

**Status:** Proposed for operator review

**Scope:** Native DeepSeek support plus one bounded official SWE-bench instance

## 1. Goal

Add a first-class DeepSeek model provider to AgentForge and use it to generate one
candidate patch for the pinned official SWE-bench Lite instance
`sympy__sympy-20590`. Evaluate that patch with the unmodified, pinned SWE-bench
harness that has already passed a gold-patch infrastructure smoke test.

The result is a product and infrastructure canary. It is not an official leaderboard
submission and must not be presented as a general SWE-bench score.

## 2. Non-goals

- Do not add arbitrary OpenAI-compatible endpoints or operator-controlled base URLs.
- Do not replace the existing OpenAI Responses provider.
- Do not run the full 300-instance SWE-bench Lite suite.
- Do not tune prompts after observing the official test result in the same canary.
- Do not persist API keys, raw secrets, hidden tests, or unredacted provider payloads.
- Do not claim that a single resolved task establishes benchmark quality.

## 3. Alternatives considered

### A. Native DeepSeek provider (selected)

Implement `DeepSeekModelProvider` against DeepSeek Chat Completions while preserving
AgentForge's existing `ModelProvider` contract, budgets, journaling, approval flow,
and tool policy. This is the smallest implementation that is still a credible product
feature.

### B. Generic OpenAI-compatible provider

Expose a configurable base URL and reuse one provider for many vendors. This is more
flexible, but it expands the trust boundary, makes provider identity less precise, and
requires more configuration validation than the current canary needs.

### C. Standalone translation script

Call DeepSeek outside AgentForge and feed a generated patch directly to SWE-bench.
This is faster but does not exercise AgentForge's runtime, approvals, persistence, or
audit evidence, so it does not support the portfolio claim we want to test.

## 4. Architecture

### 4.1 Provider boundary

Add `src/agentforge/models/deepseek_provider.py` and a small DeepSeek tool-schema
converter. The provider will use the existing OpenAI Python client only as an HTTP SDK,
configured with the fixed base URL `https://api.deepseek.com`. Product code will call
Chat Completions, not the OpenAI Responses API.

The provider name is exactly `deepseek`; its journal identity is
`deepseek/<requested-model-id>`. The base URL is a code constant and is not accepted
from project configuration or environment variables.

For this first version, requests explicitly disable provider-side thinking. This keeps
multi-turn tool history deterministic and avoids introducing an unreviewed
`reasoning_content` persistence contract. A future change may add frozen thinking
settings as a separate protocol version.

### 4.2 Request translation

Translate `ModelRequest` into Chat Completions messages in this order:

1. `instructions`, when present, becomes a `system` message.
2. `task` becomes the first `user` message.
3. A `TOOL_CALL` history item becomes an `assistant` message with one `tool_call`.
4. A matching `TOOL_RESULT` or `APPROVAL_RESULT` becomes a `tool` message with the
   original call ID and canonical JSON content.
5. Other bounded history payloads become canonical JSON `user` messages.

AgentForge tool schemas are mapped to Chat Completions function tools. The provider
does not rely on DeepSeek beta strict mode; returned arguments are parsed as JSON and
continue through AgentForge's existing schema and policy enforcement.

`max_output_tokens` maps to `max_tokens`. The request uses a single choice and does
not request streaming. Unknown provider-specific parameters are not forwarded.

### 4.3 Response normalization

The first response choice must contain exactly one of:

- one or more tool calls; or
- non-empty final text.

A response containing both is a protocol error. Function arguments must decode to a
JSON object, and every selected call must have a non-empty name and call ID.

Multiple tool calls use the existing AgentForge policy:

- strict policy rejects them;
- sequential read-only policy may select only the first call when all returned calls
  are known local, read-only, approval-free tools and the configured count limit is
  respected;
- all other multi-call responses are contract deviations.

The normalized `ModelResponse` records provider/model identity, request ID, duration,
token usage, returned/discarded tool-call counts, and only sanitized metadata.

### 4.4 Errors and retries

Map SDK exceptions to the existing stable error taxonomy:

- authentication or permission -> `MODEL_AUTH_ERROR`, not retryable;
- rate limit -> `MODEL_RATE_LIMITED`, retryable;
- timeout -> `MODEL_TIMEOUT`, retryable;
- connection -> `MODEL_TRANSPORT_ERROR`, retryable;
- bad request -> `MODEL_BAD_REQUEST`, not retryable;
- other HTTP 5xx -> `MODEL_PROVIDER_ERROR`, retryable;
- other provider status errors -> `MODEL_PROVIDER_ERROR`, not retryable.

The SDK performs zero implicit retries. AgentForge remains the only retry authority so
attempts stay journaled and budgeted.

### 4.5 Frozen evaluation binding

Extend the evaluation provider binding to permit `deepseek` for `REAL_MODEL` protocols.
DeepSeek response identity must equal the requested model ID exactly; OpenAI's dated
snapshot exception remains OpenAI-only.

Add a DeepSeek evaluation factory that:

- requires an authorized real-model protocol;
- requires a validated real-model execution gate;
- reads only `DEEPSEEK_API_KEY` at runtime;
- creates a `ProtocolBoundModelProvider`;
- never includes the secret in manifests, digests, logs, or repr output.

The selected model and reported response model are frozen in the study definition.
The operator will query `/v1/models` with the secret immediately before the cloud run
and choose an exact model ID returned by the account instead of relying on a stale
hard-coded default.

## 5. One-instance SWE-bench canary

### 5.1 Frozen inputs

- SWE-bench source commit:
  `4e6126978a16bdfebc6538db8f28cacc2c8b77dc`
- Dataset: `SWE-bench/SWE-bench_Lite`, test split
- Instance: `sympy__sympy-20590`
- Repository: `sympy/sympy`
- Base commit: `cffd4e0f86fefd4802349a9f9b19ed70934ea354`
- Dataset mirror is transport only; the downloaded row is exported locally and hashed
  before execution so the live run does not depend on mutable Hub state.

### 5.2 Separation of generation and scoring

Generation and scoring are separate processes:

1. Prepare a disposable Git worktree at the exact base commit.
2. Run AgentForge with the frozen issue text, DeepSeek binding, bounded step/token
   budget, and the normal mutation/approval/audit pipeline.
3. Capture the final `git diff` without allowing AgentForge to read official hidden
   tests or the gold patch.
4. Write a standard SWE-bench predictions JSONL record containing the instance ID,
   exact model identity, and generated patch.
5. Invoke the pinned official SWE-bench harness in Docker against that JSONL.
6. Preserve the AgentForge run export, prediction digest, harness report, container
   cleanup result, elapsed time, and cost summary.

The already completed gold smoke proves the official harness and instance image can
resolve the task. It is infrastructure evidence only and is never exposed to the model.

### 5.3 Bounded outcome

The canary has one of four reported outcomes:

- `RESOLVED`: official harness resolves the generated patch;
- `UNRESOLVED`: harness runs successfully but the patch fails;
- `GENERATION_FAILURE`: AgentForge does not produce a valid patch;
- `INFRASTRUCTURE_FAILURE`: provider, network, Docker, disk, or harness failure prevents
  a valid score.

An unresolved result is useful diagnostic evidence and does not trigger prompt tuning
inside the same frozen run. Any follow-up receives a new protocol digest.

## 6. Security and cost controls

- Enter `DEEPSEEK_API_KEY` with hidden terminal input and export it only for the current
  shell. Never pass it on a command line or save it in shell history.
- Scrub the variable after the run and keep it out of generated artifacts.
- Keep the Tencent security group limited to SSH from the operator's trusted IP.
- Default to one model request canary before the repair run.
- Freeze maximum model requests, per-request output, total tokens, and wall-clock
  timeout before authorization.
- Stop the CVM after artifacts are copied and container/image cleanup is confirmed.

## 7. Verification strategy

Implementation follows test-driven development.

### Unit tests

- system/task/history message translation;
- tool-schema conversion;
- final answer and tool-call parsing;
- invalid/mixed/empty response handling;
- multi-tool policy behavior;
- usage normalization, including cached and reasoning tokens when reported;
- every SDK error mapping;
- exact provider/model identity and secret redaction;
- DeepSeek protocol validation and configuration digest stability.

### Integration tests

- fake Chat Completions client through a complete AgentForge tool round trip;
- DeepSeek evaluation factory plus execution gate;
- existing OpenAI and mock provider tests remain unchanged and passing;
- CLI/study preparation accepts a frozen DeepSeek binding without requiring a live key.

### Live and cloud checks

- opt-in authenticated `/v1/models` check with no secret output;
- one low-cost DeepSeek tool-call canary on an existing AgentForge demo fixture;
- one official SWE-bench prediction and harness evaluation;
- verify no unstopped containers and archive redacted evidence.

## 8. Acceptance criteria

The change is ready for cloud deployment when:

1. all new unit and integration tests pass;
2. the complete existing test, lint, type-check, and compile suite passes;
3. no secret appears in Git, reports, logs, exceptions, or object representations;
4. OpenAI and mock behavior are regression-tested;
5. the cloud runbook can produce a standard prediction record from one disposable
   workspace;
6. the official harness classifies the single attempt without infrastructure failure;
7. public documentation labels the result as a one-instance canary, not an official
   benchmark score.

## 9. Deliverables

- native DeepSeek provider and schema converter;
- DeepSeek evaluation factory and protocol binding support;
- TDD unit/integration/live-test coverage;
- bounded one-instance SWE-bench generation bridge and runbook;
- redacted canary evidence template;
- documentation describing limitations and reproducibility.
