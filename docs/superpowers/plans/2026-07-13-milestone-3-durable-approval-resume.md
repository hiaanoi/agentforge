# Milestone 3 Durable Approval and Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and
> superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, restart-safe approval decisions and checkpoint resume to the core Runtime.

**Architecture:** Persist executable state in versioned RuntimeSnapshot checkpoints and approval
facts in a separate table. Short SQLite transactions perform conditional claims and cancellation;
ToolExecutor runs outside transactions and accepts only digest-bound approval authorization.

**Tech Stack:** Python 3.14.3, Pydantic 2, SQLAlchemy 2, SQLite, pytest, pytest-asyncio, uv.

**Git constraint:** Do not commit, push, switch branches, reset, clean, stash, rebase, or merge.

---

### Task 1: Approval domain and deterministic digest

**Files:**
- Modify: `src/agentforge/domain/enums.py`
- Modify: `src/agentforge/domain/models.py`
- Modify: `src/agentforge/domain/errors.py`
- Create: `src/agentforge/domain/digests.py`
- Create: `tests/unit/test_approval_domain.py`

- [ ] Write failing tests that construct ApprovalRequest and RuntimeSnapshot, reject illegal state
  values, and prove equal validated payloads produce equal SHA-256 while checkpoint or step changes
  produce a different digest.

```python
digest = compute_tool_call_digest(
    tool_name="approval_probe",
    validated_arguments={"value": "raw-secret"},
    checkpoint_id=checkpoint_id,
    step_number=2,
)
assert len(digest) == 64
assert digest == compute_tool_call_digest(
    tool_name="approval_probe",
    validated_arguments={"value": "raw-secret"},
    checkpoint_id=checkpoint_id,
    step_number=2,
)
```

- [ ] Run `uv run --frozen pytest tests/unit/test_approval_domain.py -q`; expect import/attribute
  failures for the missing models.
- [ ] Add `ApprovalStatus`, `RejectionStrategy`, `ApprovalConsumptionState`, `ResumePhase`, stable
  approval/recovery error codes, ApprovalRequest, RuntimeSnapshot, ApprovalRequired, and
  ApprovalAuthorization. Implement canonical SHA-256 with `json.dumps(sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`.
- [ ] Run the same test; expect PASS. Run `test_domain.py` to confirm M0/M1 transitions remain green.

### Task 2: Approval table and repository

**Files:**
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/persistence/repositories.py`
- Create: `tests/unit/test_approval_persistence.py`

- [ ] Write failing tests for create/get/list-pending, deterministic ordering, database reopen,
  Run isolation, `(run_id, checkpoint_id)` uniqueness, checkpoint lookup by ID, and timestamp UTC.
- [ ] Run `uv run --frozen pytest tests/unit/test_approval_persistence.py -q`; expect missing table
  and repository failures.
- [ ] Add ApprovalRequestRow with foreign keys, unique constraint and pending index. Add
  ApprovalRepository methods `create`, `get`, `list_pending`, `list_for_run`, and mappings. Add
  `CheckpointRepository.get(checkpoint_id)` with Run ownership validation at the caller.
- [ ] Run approval persistence and existing persistence tests; expect all PASS.

### Task 3: Atomic approval workflow and conditional claim

**Files:**
- Create: `src/agentforge/persistence/approval_workflow.py`
- Create: `tests/unit/test_approval_workflow.py`

- [ ] Write failing tests proving one transaction can pause a Run with checkpoint/approval/events,
  resolve identical decisions idempotently, reject conflicting decisions, conditionally claim only
  once, consume only CLAIMED rows, atomically cancel Run plus unconsumed approval, and isolate Runs.

```python
assert workflow.claim_resume(run.run_id, approval.approval_id) is True
assert workflow.claim_resume(run.run_id, approval.approval_id) is False
```

- [ ] Run the workflow test; expect module-not-found failure.
- [ ] Implement short transaction methods `pause_for_approval`, `resolve`, `claim_resume`,
  `persist_consumed`, `mark_indeterminate`, and `cancel`. Use conditional SQL UPDATE predicates and
  verify row counts. Allocate per-Run event sequence numbers inside the same transaction.
- [ ] Run workflow and persistence tests; expect PASS and no open transaction during tool calls.

### Task 4: ToolExecutor approval outcome and authorization

**Files:**
- Modify: `src/agentforge/policy/engine.py`
- Modify: `src/agentforge/tools/executor.py`
- Modify: `tests/unit/test_policy.py`
- Modify: `tests/unit/test_tool_runtime.py`

- [ ] Write failing tests that REQUIRE_APPROVAL returns ApprovalRequired without TOOL_STARTED or
  TOOL_FAILED, budget increment, or tool invocation. Add tests that a valid digest-bound
  ApprovalAuthorization executes once and invalid/mismatched authorization is denied.
- [ ] Run the focused tests and verify expected failures against the current ToolResult behavior.
- [ ] Extend PolicyEngine with an internal `approval_granted` input. Extend ToolExecutor to return
  `ToolResult | ApprovalRequired`; authorized execution recomputes the digest from validated raw
  arguments, checkpoint ID, and step, skips duplicate TOOL_REQUESTED, then uses the normal bounded
  execution path.
- [ ] Run ToolExecutor, Policy, repository security, and M2 integration tests; expect PASS after
  updating only assertions whose approval semantics intentionally changed.

### Task 5: Runtime pause, decision, cancellation, and resume interface

**Files:**
- Modify: `src/agentforge/runtime/engine.py`
- Create: `tests/integration/test_approval_resume.py`

- [ ] Create a test-only approval tool with an execution counter and write failing tests for:
  WAITING_APPROVAL, zero execution before approval, pending listing after database reopen,
  identical decision idempotency, decision conflict, default reject/CONTINUE, reject/FAIL_RUN,
  cancellation, and terminal resume rejection.
- [ ] Run the integration test; expect missing Runtime methods and current Run failure behavior.
- [ ] Refactor Runtime loop into a private continuation that accepts RuntimeSnapshot history. Add
  `list_pending_approvals`, `approve`, `reject`, `resume`, and `cancel`. On ApprovalRequired,
  pre-generate Approval/checkpoint UUIDs, compute the raw digest, save AWAITING_APPROVAL snapshot,
  and atomically enter WAITING_APPROVAL with APPROVAL_REQUESTED and RUN_PAUSED.
- [ ] Implement approve and reject through the workflow. CONTINUE transitions to PAUSED; FAIL_RUN
  transitions directly to FAILED. Cancel atomically prevents later claim.
- [ ] Run the focused integration test until PASS, then run M0/M1/M2 runtime tests.

### Task 6: Consumption, exactly-once resume, and crash windows

**Files:**
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `tests/integration/test_approval_resume.py`

- [ ] Add failing tests for approved execution exactly once, duplicate/concurrent resume, consumed
  approval resume, missing/corrupt/wrong-Run snapshot, multiple Run isolation, and three crashes:
  decision-before-resume, CLAIMED-before-result, and result-checkpoint-before-model.
- [ ] Verify RED: current implementation either re-executes, restarts empty, or lacks recovery.
- [ ] Add per-Runtime active-Run guard. Claim with conditional UPDATE before execution and commit.
  Persist full ToolResult in a READY_FOR_MODEL checkpoint and mark CONSUMED before model call.
  Rebuild history from that checkpoint after restart without tool replay. Convert stale approved
  CLAIMED rows to INDETERMINATE and fail explicitly; safely reconstruct claimed rejection because
  it has no tool side effect.
- [ ] Run the crash-window tests repeatedly and then the entire approval integration file.

### Task 7: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/implementation_plan.md`
- Modify: `docs/security_model.md`
- Create: `docs/milestone_03_report.md`

- [ ] Update documents with only verified M3 behavior, event ordering, restart semantics,
  conditional claims, indeterminate outcomes, and deferred capabilities.
- [ ] Run `uv run --frozen pytest -ra`; expect zero failures with explicit Windows symlink skips.
- [ ] Run `uv run --frozen ruff check .`; expect `All checks passed!`.
- [ ] Run `uv run --frozen mypy src`; expect strict success for every source file.
- [ ] Run `uv run --frozen python -m compileall src`; expect exit code 0.
- [ ] Run `git diff --check`, `git status --short`, and `git diff --stat`; verify no staged files,
  no commit, no dependency changes, and no Milestone 4 work.
