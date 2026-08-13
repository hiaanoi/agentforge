# AgentForge Demo Walkthrough

This walkthrough is the smallest reproducible demonstration of the AgentForge runtime. It uses
the deterministic MockModelProvider and does not require an API key or network access.

## What It Demonstrates

The integration scenario exercises one complete repair lifecycle:

1. Create a fresh temporary workspace from a buggy Fixture.
2. Run the evaluator-owned baseline and persist its failure fingerprint.
3. Let the model read the workspace.
4. Request an approval-bound file mutation.
5. Resume from the durable checkpoint without duplicating the mutation.
6. Run the trusted visible TestProfile through the managed process executor.
7. Validate the full diff and run evaluator-owned hidden verification.
8. Persist the typed evaluation result and audit facts in SQLite.

## Reproduce Offline

From `D:\aaa\agentforge`:

```powershell
uv run --frozen pytest tests/integration/test_repair_evaluation_e2e.py -q
uv run --frozen pytest tests/evaluation/test_formal_fixture_pilot.py -q
```

The first command verifies the end-to-end repair path. The second verifies that every formal
Fixture is copied into a fresh workspace and that visible, hidden, and profile bindings remain
isolated.

For a real-model Canary, select the self-built durability task explicitly so the first paid probe
matches the project's main story:

```powershell
uv run --env-file .env --frozen python evaluation/run_real_model_pilot.py canary `
  --state-dir .agentforge/pilots/m7-b2.4-gpt-5.4-mini-self-canary `
  --task-id self-durable-double-consumption `
  --confirm "<study-definition-digest>"
```

## Evidence To Show In An Interview

- SQLite contains ordered model, tool, approval, mutation, test, checkpoint, and evaluation facts.
- A repeated resume reuses the committed mutation result instead of writing twice.
- `read_file` returns bounded content plus the complete file SHA-256 needed by a safe edit.
- A wrong expected file hash is rejected before publication.
- A non-zero development test result is recorded as a TestResult failure, not silently treated as
  a Runtime crash.
- Failed development tests carry a structured `failure_kind` and repair guidance into the next
  model context, while infrastructure failures remain fail-closed.
- Hidden verification runs only after the final diff policy passes.

## Real-Model Boundary

The offline Demo is deterministic and is the primary regression artifact. A separate real-model
Canary is evaluator-only and costs per provider usage. The first pre-fix `gpt-5.4-mini` Canary
made three real requests and was correctly scored as `MODEL_TOOL_FAILED` because the model supplied
an incorrect `expected_sha256`. After exposing the file digest through `read_file`, a fresh
self-built-task Canary committed one safe mutation and passed the visible test. Evaluator-owned
hidden verification then reported `5 passed, 1 failed` for an unrepaired ownership invariant,
producing `FINAL_HIDDEN_TEST_FAILED`. This is real-model end-to-end execution and evaluation
evidence, not a complete successful repair score.

A separate fresh QuixBugs Canary Study with `gpt-5.4-mini` completed three independent repetitions
successfully: 3/3 `VERIFIED_SUCCESS`, three approval-bound mutations, six managed test executions,
12 physical model requests, 25,034 total tokens, and an estimated cost of `$0.027013`. This is a
three-repetition Fixture result, not a benchmark-wide score.
