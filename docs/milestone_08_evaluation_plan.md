# Milestone 8: Reproducible Agent Benchmark Evaluation

## Purpose

Milestone 8 turns the existing real-model Pilot into a small, reproducible Agent benchmark. It
adopts the useful evaluation principles used by SWE-bench, Aider, OpenHands Benchmarks, and
SWE-agent without claiming the scale or status of those public benchmarks.

The result must answer four separate questions:

1. Can the Agent complete a task correctly?
2. Does it remain correct across independent repetitions?
3. Did the Runtime, Provider, or test infrastructure fail independently of model quality?
4. What did the task cost in model requests, tokens, latency, and money?

## Reference Pattern

Public coding-agent evaluations commonly combine:

- immutable task and source revisions;
- fresh isolated workspaces per attempt;
- tests hidden from the Agent and executed by the evaluator;
- both task-specific correctness tests and regression tests;
- many independent instances or repetitions;
- per-instance results plus aggregate rates, cost, and failure analysis;
- reproducible environment and model/configuration bindings.

AgentForge already implements most of the runtime and evidence chain. Milestone 8 adds the
benchmark-facing vocabulary, task matrix, and report contract.

## Scope

### In scope

- Four current formal Fixtures with explicit source categories and difficulty tiers.
- Fixed protocol, model, response Snapshot, dependency, platform, and source bindings.
- Fresh workspace and evaluator-owned baseline for every Attempt.
- Visible development tests plus evaluator-owned hidden final verification.
- Three repetitions per task for the first benchmark profile.
- `pass_at_1`, `pass_at_3`, majority success, and stable success metrics.
- Model-quality, infrastructure-invalid, configuration-abort, and indeterminate outcomes.
- Per-task and aggregate token, cost, latency, tool, mutation, test, and Provider-deviation metrics.
- Public redacted JSON/Markdown reports and a machine-readable run manifest.
- Offline scripted validation before any real-model spend.

### Out of scope

- Claiming an official SWE-bench or Aider score.
- Running the full SWE-bench dataset.
- Comparing models without freezing the same protocol and environment.
- Publishing prompts, hidden tests, reference patches, file contents, API metadata, or secrets.
- Treating a single successful task as general Agent competence.

## Evaluation Profiles

### Profile A: Offline Contract

The deterministic MockModelProvider runs the complete four-task matrix. This validates harness
correctness, report denominators, recovery, replacement, redaction, and task isolation.

Required result: 4 tasks x 3 repetitions, with every slot classified and all expected success or
failure fixtures asserted.

### Profile B: Real-Model Canary

One selected task and one repetition. Used after every Runtime, prompt, tool schema, or model
binding change. It is the cheapest real-model regression gate.

Required result: durable report, complete usage metadata, workspace isolation, and no unclassified
failure. A failed Canary is evidence, not a reason to hide the run.

### Profile C: Real-Model Small Matrix

Four tasks x one repetition. Used to measure cross-task coverage with bounded cost.

Required result: per-task result and aggregate coverage; do not report stable success from one
repetition.

### Profile D: Real-Model Repeated Matrix

Four tasks x three repetitions, for 12 planned slots. Used only after Profiles A-C pass and the
operator has accepted the cost budget.

Required result: complete slot accounting, task-level `pass_at_1`/`pass_at_3`, majority/stable
success, infrastructure gap analysis, and cost-complete report.

## Metrics Contract

### Correctness

- `verified_success`: visible development tests, diff policy, and hidden verification all pass.
- `pass_at_1`: success rate of the first repetition for each task.
- `pass_at_3`: at least one successful repetition among three attempts for each task.
- `majority_success`: at least two successful repetitions out of three.
- `stable_success`: all three repetitions succeed.
- `visible_test_failure_rate` and `hidden_test_failure_rate`.

`pass_at_3` is a task-level any-success metric, not a claim that one attempt has a 3x chance of
success. All denominators must be explicit.

### Reliability and Safety

- infrastructure-invalid and indeterminate rates;
- replacement count and replacement eligibility;
- duplicate mutation count;
- repeated resume side effects;
- process-tree termination result;
- Provider deviation and discarded-call count;
- policy blocks and unsafe mutation attempts.

### Efficiency

- logical model calls and physical Provider requests;
- READ, mutation, and test counts;
- input/output/total/cached/reasoning tokens;
- wall time and model latency;
- estimated cost and usage completeness.

## Task Matrix

| Task | Source | Role | Initial profile |
| --- | --- | --- | --- |
| `quixbugs-shortest-path-length` | QUIXBUGS | simple code repair | Canary + 3 repetitions |
| `bugsinpy-black-21` | BUGSINPY | repository-style repair | Small Matrix |
| `swebench-pytest-10051` | SWE_BENCH_CROPPED | issue-style repair | Small Matrix |
| `self-durable-double-consumption` | SELF_BUILT | Agent Runtime durability | Canary + 3 repetitions |

Task difficulty, fixture digest, expected baseline fingerprint, visible test IDs, hidden test
root, and reference overlay digest remain immutable and are recorded in the protocol.

## Implementation Steps

### M8.0 Evaluation Contract

- Status: implemented as the first M8 slice.
- freeze this document and the task matrix;
- add machine-readable benchmark profile metadata;
- define exact report field names and denominator rules;
- add a clean-source and protocol-drift check to the run manifest.

Each prepared study now writes an immutable `run_manifest.json` beside the private study
manifest. It binds the study and definition digests, all four protocol digests, model and
response snapshot IDs, pricing and fixture digests, platform binding, Git commit, runtime
source digest, `pyproject.toml` hash, and `uv.lock` hash. Re-running preparation is idempotent;
an identity conflict fails closed. The manifest contains no API key, prompt, hidden test, file
content, or model response.

### M8.1 Metrics and Reports

- Status: implemented for task reports, Study aggregates, JSON, and Markdown;
- expose `pass_at_1` and `pass_at_3` explicitly;
- preserve existing any/majority/stable metrics for compatibility;
- add per-profile aggregate summaries;
- add a cost and usage completeness section;
- test deterministic JSON/Markdown agreement.

### M8.2 Offline Matrix

- Status: implemented and verified offline.
- execute the complete four-task x three-repetition Mock matrix;
- verify fresh workspace, baseline, hidden tests, and report isolation;
- inject scripted model-quality and infrastructure failures;
- verify replacements and indeterminate outcomes do not inflate success.

The implemented matrix executes all four registered Fixtures three times each. All 12 slots
were scored successfully, with task-level `pass@1=4/4` and `pass@3=4/4`. The matrix uses the
explicit `OFFLINE_TEST` Builder mode and a deterministic MockModelProvider; it makes no network
request and consumes no model tokens.

### M8.3 Real-Model Small Matrix

- Status: first bounded run completed; repeated pass@3 evidence is still pending.
- run one Canary per task with the same frozen model and protocol;
- record per-task success, failure class, cost, and request usage;
- stop on configuration or indeterminate failures;
- do not silently retry model-quality failures.

The first `gpt-5.4-mini` Small Matrix used one Canary slot per task. Two tasks passed
(`quixbugs-shortest-path-length` and `bugsinpy-black-21`); two reached final hidden
verification and failed (`swebench-pytest-10051` and `self-durable-double-consumption`). The
run scored 4/4 slots, with `pass@1=2/4`, 24 physical requests, 50,158 total tokens, estimated
cost `$0.042312`, and four Provider-deviation events. Because no task completed all three
repetitions, `pass@3` is not eligible for this run.

A follow-up bounded Study repeated the two passing tasks under the current source binding.
`quixbugs-shortest-path-length` and `bugsinpy-black-21` each completed 3/3 successfully,
with both task-level `pass@1=true` and `pass@3=true`. This follow-up scored 6/6 slots, used
46,694 total tokens, 28 physical requests, and had estimated cost `$0.046108`. The two
previously failing tasks were intentionally not retried in this bounded run.

### M8.4 Real-Model Repeated Matrix

- repeat the selected task set three times;
- report `pass_at_1`, `pass_at_3`, majority, and stable success;
- compare task difficulty and failure distributions;
- publish only redacted artifacts with source and dependency provenance.

Before expanding the repeated matrix, an `INVARIANT_GUIDANCE` Prompt experiment was run on
the two failed tasks. It added generic guidance about state invariants, object identity,
concurrency, recovery, and idempotency without exposing hidden tests. It did not improve the
outcome: SWE-bench remained `FINAL_HIDDEN_TEST_FAILED`, while the self-built task reached
`MODEL_CALL_LIMIT`. The experiment scored 0/2, used 45,852 tokens, 17 physical requests, and
cost `$0.034470`. The baseline Prompt remains the primary comparison; this negative result
indicates that future work needs task-specific diagnosis or stronger test-feedback design.

### M8.4.1 Task-Level Diagnostic Experiment

The next bounded experiment uses a separate `task_diagnostic` prompt variant. It borrows three
practices from public coding-agent evaluation harnesses: freeze the experiment protocol, keep
each task in an isolated workspace, and preserve a trajectory-level record of actions and
observations. The variant is explicitly diagnostic and must not replace or be merged into the
baseline score.

The guidance is derived only from each public task description and general engineering semantics:

- `swebench-pytest-10051`: preserve live references to exposed mutable collections, keep active
  phase records current after clearing, and isolate phase state;
- `self-durable-double-consumption`: make retry/recovery converge on a durable result, bind a
  dispatch side effect to a unique durable identity, and keep ownership and completion coherent.

It does not include hidden-test paths, reference-file contents, hidden test names, or private
assertion details. The implementation records this as system prompt version 3. The first run is
one Canary per failed task, with the same model, pricing, fixture, and tool policy as the baseline.
Success, failure class, model-call count, tool counts, hidden-test result, and cost will be
reported separately from the official baseline.

The bounded run `m8.4.1-task-diagnostic-v4` completed 2/12 planned slots: both target tasks
reached `FINAL_HIDDEN_TEST_FAILED`, with zero infrastructure failures and zero successful slots.
The SWE-bench task used 5 physical requests, 9,467 tokens, and estimated cost `$0.007527`; the
self-built durability task used 10 physical requests, 33,619 tokens, and estimated cost
`$0.024213`. The run total was 15 requests, 43,086 tokens, and `$0.031740`, with four normalized
multi-tool/provider-deviation events. Compared with the prior baseline canaries, the diagnostic
prompt did not improve either target task and increased the self-built task's trajectory length.
This is a negative diagnostic result, not a new benchmark score.

### M8.5 Reproducibility Package

- add a run manifest containing commit, dependency digest, protocol digests, model IDs, pricing,
  platform, and report digest;
- add a one-command offline reproduction guide;
- add a resume-ready evaluation summary with explicit limitations;
- tag the evaluation baseline without claiming an external benchmark score.

## Exit Criteria

Milestone 8 is complete when:

- the offline four-task matrix is reproducible and fully classified;
- the report exposes correct `pass_at_1` and `pass_at_3` denominators;
- at least one real-model Small Matrix has been run or explicitly blocked with evidence;
- all real-model results have complete usage/cost status or an explicit incomplete marker;
- public artifacts pass the existing secret, prompt, hidden-data, and path scanners;
- the final report distinguishes internal Fixture results from official public benchmarks.

## Current Baseline

As of the plan creation:

- Offline suite: 650 collected, 641 passed, 9 skipped, 1 warning.
- Offline four-task matrix: 12/12 scored slots, task `pass@1=4/4`, task `pass@3=4/4`.
- `gpt-5.4-mini` QuixBugs: 3/3 `VERIFIED_SUCCESS`.
- `gpt-5.4-mini` self-built durability task: visible pass, hidden failure (`5 passed, 1 failed`).
- Formal 12-slot real-model Study: not completed.
- First bounded real-model Small Matrix: `pass@1=2/4`; `pass@3` not eligible.
- Repeated passing-task Study: QuixBugs 3/3 and BugsInPy 3/3; both `pass@3=true`.
- Invariant-guidance experiment: 0/2 on the two previously failing tasks; no measured uplift.
- Task-diagnostic experiment: 0/2 on the previously failing tasks; both were
  `FINAL_HIDDEN_TEST_FAILED`, with no infrastructure failures and no change to the baseline score.
