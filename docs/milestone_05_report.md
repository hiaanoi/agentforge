# Milestone 5 Report: Safe Workspace Mutation and Durable Side Effects

## Result

Milestone 5 is **PASS**. It adds bounded, approval-gated workspace mutation without adding shell,
test execution, Git mutation, or an automatic repair loop.

Final local verification on Python 3.14.3:

- pytest: 217 collected, 213 passed, 4 skipped, 0 failed.
- Ruff: all checks passed.
- strict mypy: 54 source files, no issues.
- compileall: passed.
- `git diff --check`: passed.

The skips are one explicitly opt-in OpenAI live test and three real symlink tests blocked by
Windows `WinError 1314`. The simulated reparse-point case passed. Windows junctions and other
reparse-point forms are not comprehensively verified.

## Delivered capability

- `write_file`: no-clobber creation or existing-file replacement bound to `expected_sha256`.
- `edit_file`: exactly one old/new text replacement bound to the current file SHA-256.
- Mandatory durable approval for both local WRITE tools; DANGEROUS and non-local tools remain
  denied.
- Workspace containment, sensitive path/content detection, strict UTF-8 and byte limits, NUL and
  binary-like content rejection, and repeated pre-execution validation.
- Fsynced temporary files with atomic no-clobber create or hash-checked atomic replacement.
- Immutable mutation approval bindings and durable execution records queryable through Runtime.
- Safe audit events containing IDs, paths, hashes, counts, status, and bounded summaries, but no
  source text, raw replacement text, tool output, or credentials.

## Durability and crash windows

1. Approval persisted before resume: a recreated Runtime can query, decide, and resume it.
2. Resume claimed before result persistence: WRITING is treated as INDETERMINATE and is never
   automatically replayed.
3. Filesystem commit recorded before result checkpoint: Runtime verifies the actual target hash,
   reconstructs the safe result, and consumes the approval without writing again.

Repeated approve and resume operations are idempotent under the supported single-process SQLite
model. Cancel atomically cancels the Run and pending approval and prevents PREPARED execution.
Different Runs retain independent approvals, checkpoints, bindings, events, and executions.

## Security boundary and debt

- SQLite contains raw validated mutation arguments in recovery checkpoints and must be protected
  as sensitive application data. AgentForge does not encrypt it.
- Secret scanning is intentionally high confidence and is not a DLP guarantee.
- SQLite and filesystem publication cannot form one atomic transaction; uncertain side effects
  fail closed instead of being retried.
- Mutation tools execute synchronously in the Runtime context to prevent detached background
  writes. Their timeout is non-preemptive; process-isolated forced termination is future work.
- No OS sandbox protects the workspace. External adversarial path swaps and broad Windows
  junction/reparse behavior need further hardening.
- CREATE_ONLY depends on same-directory atomic hard-link publication and fails closed on
  filesystems that do not support it.

## Deferred scope

`run_tests`, arbitrary shell or process execution, dependency installation, Git mutation,
automatic bug repair, file deletion/move/rename, general patch parsing, FastAPI, CLI, MCP,
LangGraph, multi-agent execution, containers, and evaluation remain unimplemented.
