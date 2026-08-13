# Resume STAR Material

This document is a source of truthful resume bullets. Replace no numbers with claims that have
not been reproduced locally.

## Durable Agent Runtime

**Situation:** Tool-using LLM agents can lose approval state, repeat side effects after restart,
or leave incomplete process executions when the host process crashes.

**Task:** Build a durable runtime that makes model decisions, tool execution, approval, and
recovery auditable and fail-closed.

**Action:** Implemented typed Run and RuntimeSnapshot state, SQLite checkpoints and audit events,
digest-bound ApprovalRequest records, conditional CAS resume claims, idempotent mutation records,
expected-state SHA-256 checks, atomic file publication, and Windows Job Object/POSIX process-tree
supervision. Added explicit `INDETERMINATE` handling for uncertain side effects.

**Result:** The offline suite currently collects 640 tests with 631 passed and 9 skipped. The
synthetic repair E2E completes baseline verification, approval-bound edit, managed testing, full
diff validation, hidden verification, and durable result persistence using fresh workspaces.

## Agent Evaluation Infrastructure

**Situation:** A model-repair result is not trustworthy unless the buggy baseline, model attempts,
tool calls, test executions, and final verification are all bound to the same immutable task.

**Task:** Build an evaluator that can distinguish model quality failures from infrastructure
failures and survive evaluator restarts.

**Action:** Implemented immutable Fixture and EvaluationProtocol bindings, evaluator-owned baseline
execution, fixed repetition slots, replacement and recovery state machines, usage-complete cost
accounting, typed `SCORED`/`INFRASTRUCTURE_INVALID`/`INDETERMINATE` outcomes, and redacted JSON/
Markdown reports. Added OpenAI Responses integration with model alias and exact response Snapshot
binding, plus explicit multi-tool normalization policies.

**Result:** The deterministic three-repetition Pilot passed visible and hidden verification for the
self-built durability Fixture. After the read-tool Hash contract was completed, a real
`gpt-5.4-mini` Canary committed one approval-bound mutation, passed the visible test, and reached
evaluator-owned hidden verification. The hidden suite reported `5 passed, 1 failed`, classified as
`FINAL_HIDDEN_TEST_FAILED`, with no infrastructure failure or unsafe write.

A separate QuixBugs real-model Canary Study completed three independent repetitions with
`VERIFIED_SUCCESS` on all three runs: three mutations, six managed test executions, 12 provider
requests, 25,034 tokens, and an estimated cost of `$0.027013`. This is a repeated Fixture result,
not a benchmark-wide score.

## Do Not Claim Yet

- Do not claim a successful real-model bug repair score.
- Do not claim a production CLI, FastAPI service, MCP integration, LangGraph integration, or
  multi-agent system.
- Do not claim a benchmark score from the formal 12-slot Study; it has not been completed.
- Describe AgentForge as a durable Agent Runtime and evaluation infrastructure project.
