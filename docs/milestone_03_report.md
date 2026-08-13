# Milestone 3 Report: Durable Approval and Resume

## Status

**PASS.** Implementation and final quality gates are complete.

## Implemented

- ApprovalRequest domain lifecycle with PENDING, APPROVED, REJECTED, and CANCELLED decisions.
- Separate NOT_STARTED, CLAIMED, CONSUMED, and INDETERMINATE consumption lifecycle.
- SQLite approval table, deterministic pending queries, Run isolation, and database reopen support.
- Versioned RuntimeSnapshot with Run, step, history, pending call, approval ID, digest, and phase.
- SHA-256 binding over tool name, validated raw arguments, checkpoint ID, and step number.
- Short transactional pause, decision, conditional claim, consumption, indeterminate, and cancel
  operations. Tool and model calls execute outside transactions.
- Runtime methods: `list_pending_approvals`, `approve`, `reject`, `resume`, and `cancel`.
- Default reject/CONTINUE injects a structured rejection result and lets the model change course.
  reject/FAIL_RUN terminates the Run without executing the tool.
- Restart recovery before resume and after result persistence, with no approved-tool replay.
- Explicit failure for missing, corrupt, wrong-phase, or indeterminate recovery state.

## Idempotency

- Repeating the same approve or reject returns the persisted first decision.
- Conflicting decisions raise ApprovalDecisionConflictError.
- Only a successful conditional NOT_STARTED-to-CLAIMED update may process a decision.
- Consumed approvals never execute their tool again. A resume call on a terminal Run raises
  ResumeNotAllowedError without side effects.
- Cancel atomically closes the Run and every unconsumed approval.

## Crash windows

1. A decision persisted before resume survives Runtime and database recreation.
2. An approved CLAIMED call without a persisted result becomes INDETERMINATE and is not retried.
3. A persisted READY_FOR_MODEL result resumes model execution without tool replay.

## Security and scope

Approval applies only to local READ tools. It does not enable WRITE, DANGEROUS, shell, code change,
test execution, network, or non-local tools. Complete results and raw validated arguments are
recovery data in checkpoints, not Approval rows or audit events.

No FastAPI, CLI, real model provider, MCP, LangGraph, evaluation infrastructure, frontend,
distributed worker, or schema migration framework was added. Python, dependencies, and `uv.lock`
were not changed.

## Known limits

- Unknown approved side effects are not retried; the Run fails INDETERMINATE.
- Exactly-once external effects require tool-level idempotency.
- SQLite conditional claims target the current single-process architecture, not distributed work.
- Legacy unversioned checkpoints are not valid M3 approval recovery snapshots.
- Windows symlink and reparse-point limits remain as documented in the M2 security model.

## Verification

- Python: 3.14.3.
- `uv run --frozen pytest -ra`: 106 collected, 104 passed, 2 skipped, 0 failed.
- Both skips are existing Windows symlink tests blocked by `WinError 1314`.
- `uv run --frozen ruff check .`: all checks passed.
- `uv run --frozen mypy src`: 32 source files, no issues.
- `uv run --frozen python -m compileall src`: passed.
