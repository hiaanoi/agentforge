# Repair Evaluation Protocol

## Scope

This protocol defines the M7-A infrastructure contract, the frozen M7-B2.3 offline Pilot, and the
M7-B2.4 real-model Study machinery that preserves it. The read-only real-model smoke passed, while
the formal 12-slot Study has not been executed, so this document reports no B2.4 aggregate
model-quality result.

## Evaluation Unit

One RepairEvaluationRun is one frozen EvaluationProtocol, one Campaign slot, one monotonic Attempt,
one model/config digest set, one fresh workspace lease, one RepairTaskPolicy digest, and one
AgentRuntime Run. Replacement Attempts always reference their invalid predecessor Attempt and
reference its evaluation-run ID when that predecessor produced one. History is never overwritten.

## Frozen Campaign

Before execution, register one immutable EvaluationProtocol and create the complete predeclared
slot set. The protocol binds task/registry assets, expected baseline fingerprint, provider/model
configuration, model budgets, prompts, Tool schema, ContextPolicy, task policy, TestProfile
template, executable/platform facts, repetition count, completion mode, and infrastructure-only
replacement policy.

Each slot runs sequentially in a fresh workspace. Ordinary execution must win that slot's
conditional claim; persisted active work can continue only through explicit recovery. Recovery
reuses persisted results and terminal side-effect facts. It fails closed on ambiguous baseline,
mutation, test-process, or approval state and never silently starts over.

## Isolation and Setup

1. Load a strict JSON EvaluationTaskDefinition.
2. Validate the source fixture and reject symlink/reparse or unsupported entries.
3. Copy the fixture to a new system temporary directory for each repetition.
4. Build and persist a full metadata WorkspaceBaseline before model execution.
5. Register immutable administrator-defined development and hidden TestProfiles.
6. Persist and run the evaluator-owned visible buggy baseline through the M6 managed execution
   core.
7. Require a non-zero result whose normalized pytest failed node-ID set exactly matches the
   manifest; any mismatch or uncertain outcome terminates before the first model request.
8. Inject only the bounded redacted visible failure summary into protected initial context.
9. Bind a fixed BASIC, ENGINEERING, or CHALLENGE RepairTaskPolicy.
10. Record system/task/tool-schema digests. Do not store prompt full text in Events or reports.

Temporary fixture separation is reproducibility hygiene, not an OS sandbox.

The baseline is evaluator-owned: the model cannot trigger, skip, configure, or repeat it. It does
not consume development-test, Tool, model-call, or token budgets. A verified record is reused after
restart; an inherited STARTED record becomes INDETERMINATE and is not automatically rerun. Hidden
verification is not executed during baseline setup.

## Allowed Capabilities

- Local bounded READ repository tools.
- `edit_file` and `write_file` only where the bound policy permits, after durable approval.
- `run_tests(profile_id)` only for the allowed development profile or trusted hidden final profile.

The protocol forbids arbitrary shell, model-supplied process configuration, dependency installation,
Git mutation, network tools, and task-policy modification.

## Completion

The model may propose FinalAnswer. The Runtime accepts verified completion only after:

- a successful allowed development test covers the latest mutation;
- the full baseline-to-final diff is compliant;
- protected files are unchanged;
- the trusted hidden final profile succeeds.

One default completion correction may state an unsatisfied invariant without prescribing a file,
tool, root cause, repair, or hidden-test detail. Strict mode uses the same initial prompt and Tool
schemas but permits zero corrections.

## Approval

Evaluation AutoApproval must remain constrained. It may act only for a temporary workspace bound to
the same Run and RepairTaskPolicy digest. Every action must still use ApprovalRequest, approve,
resume, checkpoint binding, mutation/test binding, and the existing coordinators. AutoApproval must
never become a general Runtime `approve_all` option.

## Recorded Facts

Persist:

- IDs, digests, versions, status, counts, timestamps, bounded durations, and failure categories;
- terminal Repair state status and stable model failure reason before an evaluation result is
  admitted;
- MutationExecutionRecord and ProcessExecutionRecord execution facts;
- independent EvaluationBaselineExecutionRecord facts and safe-summary digests;
- baseline and diff metadata summaries;
- RepairEvaluationRun and TaskEvaluationSummary values.

Do not place source full text, complete diffs, prompt full text, hidden-test content, API keys,
environment mappings, or complete stdout/stderr in Events, checkpoints, or evaluation reports.
The bounded ProcessExecutionRecord output retained by M6 remains sensitive SQLite data.

## Repetitions and Metrics

Formal campaigns use predeclared repetition counts. Aggregate at least:

- run-level verified-success rate;
- majority, stable, and any-success indicators;
- mean model/edit/test counts;
- median wall time;
- completion-correction, policy-block, budget-exhaustion, and infrastructure-failure rates;
- failure-category distribution.

Infrastructure failures must not be silently counted as model repair failures. Replacement
Attempts remain traceable even when a pre-model failure produced no RepairEvaluationRun. Only
selected effective runs contribute to model-quality metrics. A model/provider failure must
terminalize the Repair state before result persistence; no `RUNNING` RepairEvaluationRun is
admissible. For a replacement result, its `replacement_for_evaluation_run_id` must exactly match
the persisted predecessor evaluation-run ID of its Attempt.

## Real-Model Entry Criteria

Before formal evaluation:

- use a reviewed frozen protocol and never mutate it after registration;
- review every fixture license and provenance;
- validate development and hidden profiles on the target OS;
- require every Pilot to pass the evaluator-owned visible baseline gate before model startup;
- run pilot tasks and classify infrastructure failures;
- decide repetition count and replacement policy;
- document model ID and trusted model parameters;
- keep formal results separate from M7-A infrastructure acceptance.

B2.4 adds four immutable REAL_MODEL Protocols, one digest-bound Study, explicit operator and
clean-source authorization, fixed sequential execution, recovery-only adoption of active state,
outcome-aware replacement, immutable telemetry, pricing snapshots, and deny-by-default public
reporting. `parallel_tool_calls=False` remains a request constraint rather than a trust boundary;
the Runtime still enforces its existing multi-tool normalization policy.

The model binding records `model_id` as the requested Provider alias and `response_model_id` as the
exact identity expected in every Provider response. The response identity may equal the alias or a
strict dated Snapshot of that alias when prepared, but execution uses exact equality against the
frozen response identity.

Preparation is offline and creates all four Campaigns and 12 slots before any formal Provider
request. `run` requires `RUN_REAL_MODEL_PILOT=1`, a runtime-only API key, and the exact Study
definition digest. The complete execution gate is checked before authorization is persisted.
`recover` is the only entry point allowed to adopt persisted active work. `report` performs no
Provider call and requires no API key. Its public JSON and Markdown represent every planned slot,
including unexecuted and invalid slots, while durable private identifiers and execution facts remain
in SQLite.

Model-quality failures remain score-eligible. Only allowlisted infrastructure failures may use
the single frozen replacement per slot. Authentication, unsupported/bad requests, and response
identity drift are configuration failures; deterministic Provider/Protocol/source/gate binding
errors also abort without replacement. Uncertain local side effects stop the Study as
INDETERMINATE. Provider-side exactly-once remains impossible across a crash.
