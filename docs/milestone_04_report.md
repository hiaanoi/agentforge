# Milestone 4 Report: Real Model Adapter and Context Engineering

## Status

**PASS.** Implementation and Offline M4 quality gates are complete.

## M4A implemented

- Provider-neutral ModelResponse, usage, configuration, budgets, and stable error hierarchy.
- Asynchronous OpenAI Responses adapter with injected-client offline tests.
- Strict function schema normalization and provider-neutral call-ID correlation.
- Explicit `store=False`, `parallel_tool_calls=False`, no `previous_response_id`, and SDK retries
  disabled (`max_retries=0`).
- Persisted physical model attempts, normalized retryability, injected backoff/jitter, and bounded
  request/token accounting.
- RuntimeSnapshot v2 with strict unversioned and v1 migration plus future-version rejection.

## M4B implemented

- Typed provider-neutral context items and deterministic context construction.
- Deterministic rendering for repository tool results before model reuse.
- Pair-boundary context compaction with `CONTEXT_COMPACTED` audit events.
- Exact repeated action/result/error loop warnings and terminal protection.
- Context, loop, model usage, request count, provider metadata, pending call, and approval state in
  RuntimeSnapshot v2.
- v2 approval pause and consumed-result checkpoints; legacy M3 v1 approval snapshots remain
  readable through migration.
- Resume rehydrates persisted context and loop state without replaying consumed tools.

## Offline verification evidence

- Python: 3.14.3.
- `uv run --frozen pytest -ra`: 157 collected, 154 passed, 3 skipped, 0 failed.
- Skipped: one opt-in live OpenAI test and two symlink tests blocked by Windows `WinError 1314`.
- All repository/Git tests passed, including the three real Git subprocess tests.
- Approval/resume focused suite: 11 passed.
- `uv run --frozen ruff check .`: all checks passed.
- `uv run --frozen mypy src`: 44 source files, no issues.
- `uv run --frozen python -m compileall src`: passed.
- `git diff --check`: passed. Git emitted only expected LF-to-CRLF working-tree warnings.

## Live validation

The live fixture was executed three times after the formal policy implementation:

1. PASS in 66.46 seconds: Provider returned multiple calls; maximum returned count 2, selected 2,
   discarded 2, model requests 5, tool calls 3, FinalAnswer completed, both fixture paths cited,
   and deviation/normalization events recorded.
2. FAILED in 47.47 seconds: both 20-second attempts timed out before any Provider response. Model
   requests 2, tool calls 0, no deviation event, one retry event, and stable MODEL_TIMEOUT failure.
3. PASS in 68.02 seconds after raising the live-only request timeout to 45 seconds: Provider
   returned multiple calls; maximum returned count 2, selected 1, discarded 1, model requests 5,
   tool calls 3, FinalAnswer completed, both fixture paths cited, and deviation/normalization events
   recorded.

The policy behavior passed. The 2/3 completion observation also records remaining external latency
variance rather than treating the timeout as a successful run.

## Security and scope

Only local READ tools execute. A real model does not bypass Tool Registry, Pydantic validation,
Policy Engine, path containment, sensitive-file rules, durable approval, or tool budgets. API keys,
complete prompts, model outputs, tool contents, raw SDK objects, and unsanitized arguments are not
written to audit events.

M4 does not implement write/edit tools, arbitrary shell, test execution, FastAPI, CLI, MCP,
LangGraph, RAG, evaluation infrastructure, frontend, multi-agent execution, distributed workers,
or additional model providers.

## Known limits

- Physical request retries can duplicate provider billing after an ambiguous process crash.
- Token budgets rely on provider-reported usage; context preflight uses item/character/UTF-8 byte
  limits rather than a model tokenizer.
- Exact digest loop detection does not identify semantic repetition with changed arguments or
  outputs.
- Synchronous tool timeout stops waiting but cannot kill the underlying worker thread.
- Symlink containment logic is implemented but not exercised under the current Windows account;
  junction and broader reparse-point coverage remains incomplete.
- Git output is captured in memory before the model-facing bound is applied.
- SQLite migration tooling and distributed concurrency control remain deferred.
