# Milestone 6 Report: Verified Test Execution Runtime

## Result

Milestone 6 is **PASS**. It adds approval-bound execution of administrator-registered test
profiles, not arbitrary shell or an automatic repair system.

Final local verification on Python 3.14.3:

- pytest: 268 collected, 262 passed, 6 skipped, 0 failed.
- Ruff: all checks passed.
- strict mypy: 69 source files, no issues.
- compileall: passed.
- `git diff --check`: passed.

The skips are one explicit OpenAI Live opt-in, one POSIX-only process-group test on Windows, and
four real symlink tests blocked by Windows `WinError 1314`.

## Delivered capability

- Trusted-only immutable TestProfileRegistry with fixed executable, argv, cwd, complete environment,
  profile version, and deterministic digests.
- `run_tests(profile_id)` with no command, argv, cwd, env, or extra model arguments.
- TEST_PROFILE_EXECUTION as the only executable DANGEROUS capability, always requiring approval.
- Durable TestApprovalBinding and ProcessExecutionRecord with attempt, record version, result schema
  version, process-tree identity, stream digests/sizes, bounded redacted summaries, and termination
  facts.
- Runtime APIs to list safe Profile summaries and query ProcessExecutionRecords.
- RuntimeSnapshot v3 with v2 migration, pending test identity, bounded last TestResult, and execution
  state.
- MockModel end-to-end edit_file approval/commit, run_tests approval/execution, result feedback, and
  FinalAnswer flow. Test failure never triggers automatic rollback or modification.

## Process and output safety

All Profile launches use `shell=False`, fixed absolute executable/argv/cwd, and a complete fixed
environment that does not inherit host secrets. The model selects only profile_id.

POSIX uses a new session and process group with TERM/KILL escalation. Windows uses a real Job Object
with KILL_ON_JOB_CLOSE. Its internal launcher waits until Job assignment before starting the Profile.
Timeout and cancel enumerate Job PIDs, terminate the Job, wait for empty accounting, and wait for
every retained process handle to signal. Real Windows parent/child tests found no residual process.

stdout/stderr are read incrementally. Data above each stream limit is immediately discarded while
the full digest and size continue. Only bounded, control-cleaned, high-confidence-redacted summaries
enter execution records, checkpoints, or model context. Events contain no summaries or raw process
configuration. A real process printing a fake API key was verified absent from record, checkpoint,
and Event payloads.

## Crash recovery

1. APPROVED plus CREATED after restart: revalidate every Profile binding field, claim STARTED, and
   execute once.
2. CLAIMED plus STARTED after restart: mark Process, Approval, and Run INDETERMINATE/FAILED; never
   attach by PID or automatically retry.
3. Terminal ProcessExecutionRecord before consumed checkpoint: reconstruct TestResult from the
   bounded record and never launch another process.
4. Consumed checkpoint before the next model response: RuntimeSnapshot v3 restores last_test_result
   and continues the model.

ProcessExecutionRecord is the execution fact source. Run status is control flow. Concurrent natural
completion, timeout, cancel, and recovery use status plus record_version conditions so a later
control-flow cancellation cannot rewrite a completed execution fact.

## Boundaries and debt

- TestProfile is a command whitelist, not an OS sandbox. Tests retain the AgentForge account's
  filesystem, network, and process permissions.
- The POSIX Supervisor is implemented and platform-tested conditionally, but its real acceptance
  test was skipped on this Windows host.
- SQLite and process execution are not one transaction. STARTED crash outcomes fail closed.
- Profile administration is trusted startup configuration; there is no persistent admin API.
- Arbitrary shell, model-provided process configuration, dependency installation, Git mutation,
  rollback, automatic bug repair, FastAPI, CLI, MCP, LangGraph, containers, and multi-agent execution
  remain unimplemented.
