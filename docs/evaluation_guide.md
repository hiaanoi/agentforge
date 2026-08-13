# AgentForge Evaluation Guide

This guide describes how to reproduce the project's evaluation evidence without confusing
offline correctness, real-model behavior, and official benchmark results.

## Evaluation Layers

### 1. Unit and Integration Regression

Run the complete local suite first:

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

This layer validates the runtime, persistence, policy, process control, mutation, and evaluation
harness. It does not measure model quality.

### 2. Offline Pilot Matrix

The offline matrix uses the real AgentForge Runtime and deterministic MockModelProvider responses.
Each task receives a fresh workspace, evaluator-owned baseline execution, approval-bound mutation,
managed development tests, diff validation, and hidden final verification. It is the cheapest way
to validate the complete control flow and does not consume provider tokens.

The current matrix contains four fixtures and three repetitions per fixture. Its result is useful
for regression detection, but it is not evidence that a real model can solve the tasks.

### 3. Real-Model Canary and Study

Real-model runs require:

- a clean Git worktree;
- a frozen source/dependency/protocol manifest;
- an explicit operator confirmation digest;
- a fixed model and response model;
- a frozen pricing snapshot;
- isolated temporary workspaces;
- complete usage and outcome accounting.

The evaluator records model requests, logical calls, tool calls, mutations, test executions,
provider deviations, token usage, estimated cost, and failure class. A Study may be incomplete;
unexecuted slots must not be counted as failures or successes.

## Failure Taxonomy

- **Model-quality failure:** the runtime and tests executed normally, but the final verification
  failed, for example `FINAL_HIDDEN_TEST_FAILED`.
- **Infrastructure failure:** a process, workspace, provider, or persistence dependency failed
  independently of the model's attempted repair.
- **Configuration failure:** the run could not start because the frozen binding or authorization
  was invalid.
- **Indeterminate:** the evaluator cannot prove whether a side effect completed and therefore
  refuses unsafe replay.

Only scored model-quality outcomes belong in pass@1/pass@3. Infrastructure and indeterminate
outcomes require separate reporting.

## Current Real-Model Evidence

The bounded `gpt-5.4-mini` matrix succeeded on QuixBugs and BugsInPy with three independent
repetitions each. The cropped SWE-bench and self-built durability tasks passed their visible tests
but failed one hidden invariant each. A separate task-diagnostic prompt experiment ran one canary
for each failed task; both remained `FINAL_HIDDEN_TEST_FAILED`, so the diagnostic prompt is not
treated as a benchmark improvement.

The public report is intentionally redacted. It contains hashes, counts, classifications, and
bounded summaries, but not API keys, raw model responses, hidden tests, reference files, or
unredacted tool arguments.

The follow-up `task_contract_guidance` experiment also scored 0/2: both target tasks remained
`FINAL_HIDDEN_TEST_FAILED`. This negative result is evidence against continuing prompt-only tuning
without inspecting the model's final diff and action trajectory.

The Runtime now renders development test failures as bounded `repair_feedback` with a schema version,
failure kind, exit code, failed node IDs, assertion summaries, a test summary, and deterministic next
actions. This feedback is persisted in the checkpoint and returned to the model on the next step.
It is generated only from development-test output; final hidden verification continues to remove
stdout/stderr and does not create hidden-test feedback.

## Task-Level Diagnosis Protocol

When a task fails:

1. Preserve the baseline report and protocol unchanged.
2. Inspect the final diff and visible/hidden test outcome classification.
3. State the missing semantic invariant without copying hidden-test implementation details.
4. Create a separate prompt or fixture intervention with its own version and digest.
5. Run one bounded canary.
6. Compare success, failure class, trajectory length, tool count, test count, and cost.
7. Keep the intervention separate unless it improves multiple independent repetitions.

This follows the reproducibility pattern used by public coding-agent harnesses: freeze the run
identity, isolate each task, preserve trajectories, and report repeated success and cost rather
than a single anecdotal completion.

## Claims We Do Not Make

AgentForge does not currently claim an official SWE-bench score, general autonomous repair
quality, production deployment readiness, OS-level sandboxing, or parity with OpenHands, SWE-agent,
or Aider. Those projects are useful references for evaluation design; their public scores are not
AgentForge results.
