# AgentForge SWE Budget Ablation Design

## Goal

Determine whether AgentForge's weak `1/10` result on the frozen public
SWE-bench Verified sample is primarily caused by its current execution limits
or by its repair-loop design. The experiment must repair the two product bugs
observed in the real run, raise the model-call ceiling from 50 to 100, and
score exactly three fresh attempts with the official SWE-bench harness.

## Evidence that drives this scope

The completed `verified10-pass1-81413e5-rerun-20260820-1` campaign used the
frozen protocol, DeepSeek V4 Flash, and the official harness. AgentForge
resolved `scikit-learn__scikit-learn-13142`; mini-SWE-agent resolved that task
plus `django__django-13343` and `matplotlib__matplotlib-24026`.

The AgentForge databases and trajectories established two product failures:

1. On `django__django-12419` and `django__django-13343`, the `runs` row was
   terminal `FAILED` while the paired `repair_states` row remained `RUNNING`.
   Product projection consequently rejected `agentforge inspect`.
2. On `django__django-13343`, three successful identical `read_file` calls
   were treated as a fatal loop. The repeated read was not a failed or
   mutating action. The run ended after 21 model calls, below its 50-call
   budget.

Six other attempts reached the current 50-model-call limit. Increasing a
budget alone therefore cannot test the hypothesis fairly: it cannot repair
the two state/projection failures or the false positive read-loop termination.

## Chosen approach

Implement the smallest repair-loop correction and run a targeted three-task
ablation:

| Task | What it tests |
| --- | --- |
| `django__django-13343` | terminal repair-state consistency and benign repeated-read recovery; mini resolved it |
| `matplotlib__matplotlib-24026` | whether a task stopped at the 50-call ceiling can finish with 100 calls; mini resolved it |
| `django__django-12419` | repeated-read recovery on a task that previously failed at 21 calls |

The ablation uses fresh workspaces and a fresh output root. It uses the same
pinned SWE-bench source, exact base commits, Docker image digests, model
identity, temperature `0`, and official harness. It intentionally is not a
second full ten-task claim.

## Product changes

### Terminal-state synchronization

Whenever a repair run is terminally failed by the runtime, persist a terminal
`RepairCompletionStatus` compatible with the terminal `RunStatus` in the same
workflow path. `RunDetails` and `ExportRunDetails` must subsequently project
the persisted run without error. This is a product invariant, not an
evaluation-only workaround.

### Loop policy

Keep fatal loop protection for repeated mutating actions and repeated failed
tool actions. A successful, identical `read_file` action is instead handled
as a bounded benign read repetition: it remains auditable and consumes its
normal read budget, but does not immediately produce `LOOP_DETECTED` or fail
the run. A separate small repetition ceiling prevents unbounded read-only
spinning before the normal model/read budgets stop the run.

### Budget profile and targeted executor

Add a fixed `SWE_BENCH_ABLATION_100` repair budget profile with a 100 model
call cap and proportional read/edit/test ceilings. Add a narrow targeted
evaluation entry point that accepts only an explicit protocol-owned task list,
creates fresh workspaces, captures the same attempt ledger and predictions,
and rejects more than the three declared tasks. It exports normal SWE-bench
predictions for the official harness.

## Acceptance criteria

1. A terminal failed run always has a projector-compatible terminal repair
   state; `agentforge inspect` succeeds after a repeated-tool-loop failure.
2. Three successful identical `read_file` calls no longer terminate a run;
   repeated successful mutations and repeated failed tool calls remain fatal.
3. The ablation profile admits exactly 100 model calls and is persisted in the
   generated task policy.
4. The targeted runner rejects undeclared task IDs and existing output roots,
   writes three predictions, and the official harness evaluates the submitted
   non-empty patches.
5. Run the three public tasks with DeepSeek V4 Flash and the official harness.

## Decision rule

- Continue AgentForge's native repair loop only if at least two of the three
  targeted tasks resolve under the official harness.
- Otherwise stop increasing AgentForge's autonomous-repair budget and migrate
  mini-SWE-agent's repair loop behind AgentForge's approval, persistence, and
  audit boundaries.

## Explicit non-goals

- No third full ten-task campaign before the decision rule is met.
- No relaxing of write approvals, workspace containment, or final verification.
- No claim that the three-task ablation is a leaderboard result.
