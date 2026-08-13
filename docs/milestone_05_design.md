# Milestone 5 Design: Safe Workspace Mutation and Durable Side Effects

## Scope

Milestone 5 adds two local WRITE tools, `write_file` and `edit_file`, to the existing durable
Runtime. Every mutation requires approval and is bound to the Run, checkpoint, tool-call digest,
canonical relative path, and the file state observed before approval. The milestone does not add
test execution, shell commands, dependency installation, Git mutation, automatic repair,
FastAPI, CLI, MCP, LangGraph, multi-agent execution, or a sandbox.

## Current architecture audit

- `PolicyEngine` currently rejects every non-READ tool before approval can authorize it. M5 must
  permit only registered local WRITE tools that declare `requires_approval=True`; DANGEROUS and
  non-local tools remain denied.
- `WorkspacePathResolver.resolve` requires an existing path and follows workspace-internal
  symlinks. Mutation resolution needs a separate path that permits a missing final component,
  requires an existing parent, and rejects every symlink or reparse-point component.
- Approval consumption already provides conditional claims and short SQLite transactions. A
  mutation-specific workflow must atomically claim Approval, Run, and MutationExecutionRecord.
- SQLite and the filesystem cannot share a transaction. M5 records explicit PREPARED, WRITING,
  COMMITTED, FAILED, and INDETERMINATE states and never claims general filesystem exactly-once.
- Runtime checkpoints persist pending raw tool arguments. Mutation source text is therefore
  protected database content. Audit events, Approval rows, bindings, and execution records never
  contain source text. High-confidence secret detection runs before a checkpoint is created, but
  it is not a complete DLP system and SQLite is not encrypted by AgentForge.

## Domain model

`WriteMode` has `CREATE_ONLY` and `EXPECTED_HASH_REPLACE` values.

`MutationApprovalBinding` is an immutable fact created in the same transaction as the approval
checkpoint. It stores approval, Run, checkpoint, request digest, tool name, canonical relative
path, whether the target existed, the before SHA-256 (or `None` for expected absence), expected
after SHA-256, expected byte count, and creation time.

`MutationExecutionRecord` stores execution facts only: execution, Run and Approval IDs, digest,
tool name, target path, before/expected/actual hashes, bytes written, status, bounded safe result
summary, and timestamps. `approval_id` and `tool_call_digest` are unique.

## Write contracts

`write_file` accepts `path`, `content`, `mode`, and an optional `expected_sha256`:

- CREATE_ONLY requires `expected_sha256=None` and an absent target.
- EXPECTED_HASH_REPLACE requires an existing regular file and a matching SHA-256.
- Existing files are never overwritten without a matching expected hash.

`edit_file` accepts `path`, `old_text`, `new_text`, and `expected_sha256`. The current file hash
must match, `old_text` must occur exactly once, and the resulting file must be non-empty. Old,
new, and final content have independent byte limits.

Both tools reject absolute and UNC paths, traversal, sensitive paths, NUL and disallowed control
characters, high-confidence private-key/token material, oversized text, symlinks, reparse points,
non-regular targets, and missing parent directories.

## Filesystem publication

All final bytes and the expected after hash are computed before a side effect. A temporary regular
file is created in the target directory, written as UTF-8, flushed, fsynced, and checked again.
Replacement preserves the existing mode and uses `os.replace` only after a final before-hash
check. CREATE_ONLY cannot safely use `os.replace`; it publishes the fsynced temporary inode with
an atomic no-clobber hard link and fails closed when the filesystem cannot provide that primitive.
Temporary artifacts are removed on determinate failures. Directory fsync is best effort where the
platform supports it. Adversarial external path swaps remain outside M5's guarantees.

## Approval and execution flow

1. Mutation preflight validates path and content, computes before and expected-after hashes, and
   returns a safe MutationPlan without changing the filesystem.
2. Runtime creates checkpoint, ApprovalRequest, MutationApprovalBinding, MUTATION_REQUESTED,
   APPROVAL_REQUESTED, and RUN_PAUSED in one transaction.
3. Approve is idempotent and ensures one PREPARED execution record. Reject creates no execution.
4. Resume conditionally changes Approval NOT_STARTED to CLAIMED, Run PAUSED to RUNNING, and
   Mutation PREPARED to WRITING in one short transaction, then commits before file I/O.
5. The tool repeats path, sensitive-content, expected-state, and expected-after validation.
6. A determinate result changes WRITING to COMMITTED or FAILED and emits a safe mutation event.
7. Runtime stores the result checkpoint, consumes the Approval, and continues the model loop.

## Recovery

- APPROVED/NOT_STARTED plus PREPARED is claimed and executed once.
- CLAIMED plus WRITING is marked INDETERMINATE and never retried, even when the target appears to
  contain the expected bytes.
- CLAIMED plus COMMITTED verifies the current after hash, reconstructs a safe ToolResult from the
  execution record, persists consumption, and never writes again.
- CLAIMED plus FAILED restores the determinate failure without executing again.
- A COMMITTED record whose target hash changed becomes INDETERMINATE.
- INDETERMINATE fails the Run. M5 exposes it for inspection but provides no automatic or manual
  replay API.

## Runtime API and events

The existing approve, reject, resume, and cancel APIs remain authoritative. M5 adds
`get_mutation_execution(execution_id)` and `list_mutation_executions(run_id)`.

New events are MUTATION_REQUESTED, MUTATION_STARTED, MUTATION_COMMITTED, MUTATION_FAILED, and
MUTATION_INDETERMINATE. Payloads contain IDs, tool name, canonical relative path, hashes, byte
count, duration, and status only. They never contain source text, replacement text, file output,
credentials, API keys, or complete tool arguments.

## Known limits

- AgentForge does not sandbox the process or encrypt SQLite.
- High-confidence secret detection can have false negatives and is not a DLP guarantee.
- Mutation tools run synchronously in the Runtime execution context so a timeout cannot leave a
  detached writer thread. Their configured timeout is non-preemptive; process isolation is needed
  for forced termination.
- Windows junction and broader reparse-point behavior require explicit tests; current real
  symlink tests may still skip under WinError 1314.
- Database and filesystem exactly-once is impossible here. The contract is atomic claiming,
  deterministic result reuse, and fail-closed treatment of uncertain side effects.
