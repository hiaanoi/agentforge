# Task Failure Diagnosis

This document records the first bounded task-level diagnosis for the two real-model tasks that
passed their visible tests but failed final verification. It is a diagnostic artifact, not a new
benchmark score.

## Evidence Boundary

Both runs completed normally. There were no provider, workspace, process, persistence, or
authorization failures. The visible development test passed for both tasks, while final hidden
verification failed. The reports classify both outcomes as `FINAL_HIDDEN_TEST_FAILED`.

The intentionally buggy Fixture workspaces remain unchanged. Reference files and hidden tests are
not copied into the model context and are not used as public prompt content.

## Phase Capture

### Observed Failure

The model's patch passed the visible test that checks clearing the active phase. Final verification
then showed that a previously returned phase list had also been cleared. The failing behavior was
that a previous phase's records became empty after clearing the current phase.

### Root Cause

The implementation conflated the handler's current mutable buffer with the durable record list for
every phase. Multiple phase entries could therefore reference one list. Clearing the active phase
mutated the object observed by earlier phases as well.

### Required Invariant

- Every phase owns a distinct records list.
- `clear()` mutates only the current phase's list in place.
- Existing references to the current phase remain live after clearing.
- Existing references to previous phases remain unchanged.
- Subsequent messages are visible through the current phase's existing reference.

## Durable Dispatch

### Observed Failure

The model's patch passed the visible restart-after-dispatch scenario. Final verification showed
that a competing worker could replace an owner while the receipt was claimed but no durable
dispatch record existed.

### Root Cause

The claim transition treated every non-completed receipt as replaceable. It did not distinguish the
safe recovery window after a durable dispatch record from the unsafe window after claim but before
dispatch persistence. Replacing the owner in the latter window can cause the side effect to run
twice.

### Required Invariant

- An unowned receipt may be claimed once.
- A claimed receipt without a durable dispatch record cannot be replaced automatically.
- A claimed receipt with a durable dispatch record may be recovered by reusing that record.
- Completion is conditional on the current owner.
- Repeated processing returns the durable result and never creates another dispatch.

## Diagnostic Intervention

The `task_diagnostic` Prompt experiment used general invariant guidance and did not improve either
task. A separate versioned `task_contract_guidance` variant now states the confirmed contracts above.
It is kept separate from baseline and from the previous diagnostic result. A future real-model
Canary may test this intervention once the prompt binding is prepared; its result must not be merged
into the baseline without independent repeated evidence.

The first `task_contract_guidance` Canary was then executed once for each target task. Both remained
`FINAL_HIDDEN_TEST_FAILED`, with no infrastructure failures. The run used 13 physical requests,
36,014 tokens, and estimated cost `$0.034248`. The SWE-bench task used 4 requests and 6,373 tokens;
the durable dispatch task used 9 requests and 29,641 tokens. The explicit contract therefore did
not produce a measured uplift in this bounded trial.

## Engineering Interpretation

These failures are not evidence that the Runtime lost data or executed an unsafe side effect.
They show that the model can satisfy a direct visible symptom while missing a deeper contract about
object identity or crash-window ownership. The next experiment should measure whether the explicit
contract changes the final invariant outcome, trajectory length, and cost.
