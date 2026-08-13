# Security Model

## Milestone 7-A trust boundary

The model is untrusted. Tool names, arguments, paths, and requested operations must pass the
Tool Registry, Pydantic input validation, and Policy Engine before execution. Local `READ` tools
are executable. The only executable WRITE tools are local `write_file` and `edit_file`, and both
require durable approval. The sole executable DANGEROUS capability is approval-bound
TEST_PROFILE_EXECUTION. Other DANGEROUS, non-local, and misconfigured requests are rejected before
`TOOL_STARTED`.

`REQUIRE_APPROVAL` for a local READ tool creates a durable ApprovalRequest and pauses the Run.
Approval is bound to the exact validated call by a deterministic SHA-256 digest. For a mutation,
an immutable binding additionally records the canonical path and before/expected-after hashes.
Approval grants only that call. A test approval binds one immutable registered Profile; it does not
grant non-local, arbitrary shell, model-defined process, or general WRITE capability.

The OpenAI adapter makes outbound model requests only when explicitly configured by application
code. Offline tests block network access, and live tests require an explicit marker plus
`RUN_LIVE_TESTS=1`. This test guard is not an operating-system network sandbox for production.
Model output remains untrusted and must pass provider mapping, domain validation, Tool Registry,
Pydantic argument validation, and Policy Engine checks before any tool starts.

## Model provider boundary

`OpenAIModelProvider` uses the asynchronous Responses API with SDK retries disabled. AgentForge
persists and audits each physical attempt before network access, then applies its own bounded
retry/backoff policy. Requests set `store=False` and `parallel_tool_calls=False`; local checkpoints,
not `previous_response_id`, are the recovery source.

The API key is represented as Pydantic `SecretStr`, excluded from dumps and reprs, and is never
written to checkpoints, audit events, provider metadata, or errors. Events contain bounded
provider/model/error-category metadata but no complete prompts, model outputs, tool contents, raw
SDK responses, unsanitized arguments, or credentials. Error mapping returns stable internal codes
and safe messages.

Tool definitions are normalized into strict function schemas before network access. Unsupported
schema shapes fail locally. Provider function calls map to internal calls with their `call_id`, and
tool results return under the same ID. The adapter permits model selection and endpoint-related
configuration, so deployment code must treat those settings as trusted operator configuration.

## Context persistence

RuntimeSnapshot v4 stores typed context, compatibility history, model counters, bounded provider
metadata, loop state, safe test recovery state, and bounded repair summaries. Checkpoint data
includes raw validated tool arguments and complete results needed for durable recovery, so the
SQLite database is sensitive application data even though audit events and Approval rows are
minimized.

Repository tool results are deterministically rendered and bounded before model reuse. Context
compaction removes only complete call/result pairs and records metadata-only `CONTEXT_COMPACTED`
events. Exact repeated action/result/error digests produce warnings and terminal loop failures;
this does not detect semantically equivalent or adversarially varied loops.

## Workspace paths

`WorkspacePathResolver` is the only component that converts model-supplied paths into filesystem
paths. It rejects empty, absolute, drive-qualified, UNC, missing, wrong-type, and final resolved
paths outside the configured workspace. Containment uses normalized resolved paths, including
case normalization on Windows. The implementation is designed to protect against `..`, symlink,
and junction escape.

Directory traversal does not follow symlink directories and skips all symlink entries. An
explicit internal symlink is allowed and canonicalized to its target; an external target is
denied. In the final acceptance run, both real symlink tests skipped with Windows `WinError 1314`
because the current user cannot create symlinks. The protection logic is implemented but was not
runtime-verified in this environment. Windows junctions and other reparse-point variants have not
been comprehensively validated.

## Sensitive files

Central rules block `.env`, `.env.*`, private-key suffixes, `id_rsa`, `id_ed25519`, Git config
and credentials, and names containing credential, secret, token, or private_key. Matching is
case-insensitive and applies to canonical relative paths, so a symlink cannot hide a sensitive
target. Listing and search also skip sensitive files.

Mutation tools also reject NUL and disallowed control characters, invalid or oversized UTF-8,
private-key blocks, and high-confidence named secret/token assignments before an approval
checkpoint is created. This reduces accidental credential persistence but is not general DLP and
can have false negatives. Raw validated mutation content remains in the protected SQLite
checkpoint because durable resume requires it; events, approvals, bindings, and execution records
contain only sanitized arguments, hashes, counts, IDs, and bounded summaries.

## Workspace mutation

`write_file` supports only no-clobber creation and expected-hash replacement. `edit_file` requires
the current expected hash and exactly one `old_text` occurrence. Both reject deletion of the full
file, sensitive paths, missing parents, symlink/reparse components, and non-regular targets.

Mutation bytes are written to a temporary file in the target directory, flushed, and fsynced.
CREATE_ONLY publishes using an atomic no-clobber hard link and fails closed if unavailable.
Existing files are re-hashed immediately before `os.replace`; their mode is preserved. Directory
fsync is best effort on supported platforms. External adversarial path swaps and filesystem/SQLite
distributed transactions are not claimed to be solved.

Approval, Run, and MutationExecution state are claimed together before I/O without holding a
database transaction during the write. PREPARED is replayable; WRITING after a crash is marked
INDETERMINATE and never automatically retried. A COMMITTED record is reusable only after its
actual file hash is verified. This is fail-closed recovery, not filesystem exactly-once.

## Verified test execution

The model may provide only `profile_id`. Trusted startup code registers immutable TestProfiles;
there is no Runtime or Tool registration API. Registration resolves and fixes the absolute
executable, argv, workspace-contained cwd, complete child environment, version, and deterministic
digests. Resume rejects any Profile identity or configuration mismatch before process creation.

The child environment is exactly the administrator-provided mapping and does not inherit host
variables. Registration rejects environment names associated with API keys, tokens, secrets,
passwords, credentials, SSH, cloud providers, or model providers. Executable PATH search happens
once at trusted registration and never during execution.

All process launches use `shell=False`. POSIX uses a new session/process group; Windows uses a
KILL_ON_JOB_CLOSE Job Object. A controlled Windows launcher is assigned to the Job before it may
start the Profile process, preventing a pre-assignment child escape. Timeout and cancellation
target the whole tree. Windows termination retains wait handles for every Job PID and returns only
after Job accounting is empty and those process objects are signaled.

stdout and stderr are drained in chunks. Complete streams contribute only SHA-256 and byte counts;
memory and persistence retain bounded prefixes. Prefixes are decoded defensively, control-cleaned,
and high-confidence secret/private-key patterns are redacted before ProcessExecutionRecord,
checkpoint, or model context. Events contain no output summaries, executable, argv, cwd, or env.

TestProfile is a command allowlist, not an OS sandbox. A registered test has the filesystem,
network, and process permissions of the AgentForge account. External OS isolation and egress
controls remain operator responsibilities.

## Repair evaluation

RepairTaskPolicy is immutable for one Run and binds normalized path rules, allowed development
profiles, one hidden final profile, fixed budgets, change-size limits, and a deterministic digest.
Repair preflight runs after argument validation and before a mutation or test ApprovalRequest can
be created. Forbidden and protected path matches override allowed patterns.

WorkspaceBaseline records metadata for the complete fixture, not source content. Completion uses a
fresh Git-independent full scan and compares hashes, file types, sizes, creation/deletion, and
protected files. SuspiciousChangeAnalyzer detects common test-bypass patterns, but it is heuristic
and may produce false positives or false negatives.

Final verification is selected by trusted Runtime logic and still uses the M6 ApprovalWorkflow,
TestExecutionCoordinator, process-tree supervisor, and ProcessExecutionRecord. The model cannot
select the hidden profile. Hidden stdout/stderr summaries are retained only in the bounded
execution fact; they are removed before model history and RuntimeSnapshot persistence. Events and
evaluation reports retain IDs, digests, counts, status, and limited metadata only.

AutoApproval is restricted to a temporary Evaluation workspace bound to one Run and policy digest.
It approves only policy-eligible `edit_file`, `write_file`, or registered test-profile requests,
checks remaining side-effect budget and nonterminal RepairState, and calls the normal
`approve`/`resume` APIs. It does not bypass ApprovalRequest, bindings, checkpoints, or coordinator
execution.

Evaluation fixtures, TestProfiles, hidden tests, and diff policy are not an OS sandbox. They do not
isolate filesystem access, process creation, or network egress from a trusted registered test.

## Execution and audit

Each attempted tool call emits `TOOL_REQUESTED` with recursively sanitized arguments. Allowed
calls emit `TOOL_STARTED` and one terminal event. Denied calls emit `TOOL_FAILED` without a start
event. Terminal event payloads contain status, policy decision, error code, duration, and
truncation state, never tool output or file contents.

Sanitization redacts content-like keys recursively, bounds retained strings, and redacts path
values that are absolute or match the centralized sensitive-file policy. It is a persistence
minimization boundary, not a general secret detector: a credential placed under an unrelated
argument name cannot be identified reliably.

Expected failures use stable `ToolErrorCode` values. Unexpected exception messages and
tracebacks are not returned to the model or written to the standard tool log. Tool outputs must
pass Pydantic JSON-value validation and are bounded again by the Executor.

## Repository tools

- `list_files` uses stable traversal, ignored directories, sensitive-file filtering, and result
  limits.
- `read_file` reads bounded bytes, accepts strict UTF-8 text, rejects NUL-containing binary data,
  and reports truncation.
- `search_text` uses Python only, bounds query/file/result/snippet sizes, and skips invalid UTF-8,
  binary, sensitive, and oversized files.
- `get_git_diff` is the only subprocess tool. Its command and arguments are fixed, it uses
  `shell=False`, fixed workspace `cwd`, a timeout, disabled external diff/text conversion,
  scrubbed inherited `GIT_*` variables, and bounded returned output. No model argument reaches
  the Git command line.

## Known limitations

- A timed-out synchronous READ tool can continue in its worker thread; thread timeout only stops
  waiting. Mutation tools execute inline so they cannot continue in a detached timeout thread,
  but their configured timeout is not preemptive and cannot forcibly interrupt filesystem code.
  Process isolation remains deferred.
- Git stdout is captured before the returned character limit is applied. Very large diffs can
  therefore use memory above the model-facing output bound; streaming process capture is debt.
- Budget update, start event, and tool completion event are separate SQLite transactions.
- SQLite schema migration tooling is not yet present.
- Git diff currently reports unstaged working-tree changes, not staged changes.
- Text tools support strict UTF-8 only.
- Filesystem checks still have platform race limits against adversarial external workspace
  mutation; OS-level sandboxing is not implemented.
- General-purpose tool network access is prevented by capability absence, not an operating-system
  network sandbox.
- M4 intentionally adds outbound access through the configured OpenAI provider. It does not add a
  general network tool, URL fetcher, or shell capability. Production egress restrictions remain an
  operator responsibility.
- External task cancellation records `TOOL_FAILED` with `TOOL_CANCELLED` and is then re-raised;
  it does not stop an already running synchronous worker thread.
- Bounded `write_file`, exact-replacement `edit_file`, registered `run_tests`, and offline
  constrained repair evaluation infrastructure are implemented. Arbitrary shell, model-provided
  process configuration, dependency installation, Git mutation, FastAPI, CLI, MCP, LangGraph,
  additional providers, broad upstream benchmark execution, and interactive budget extension are
  absent. B2.4 adds a private evaluator-only Study script, not a product CLI; its real OpenAI smoke
  and formal 12-slot Study have not been run.
- Approval records contain sanitized arguments and bounded summaries. Raw validated arguments and
  complete ToolResult values exist in recovery checkpoints because durable execution requires them;
  checkpoint storage must therefore be treated as sensitive application data.
- Resume claims commit before tool execution. If the process exits after claim and before result
  persistence, approved execution becomes INDETERMINATE and is never automatically retried.
- Exactly-once external side effects require idempotency support from the tool. AgentForge chooses
  at-most-once retry behavior for uncertain approved calls.
- Conditional claims are designed for the current single-process SQLite runtime. Distributed
  workers, leases, and atomic external side effects are not implemented.
- Physical request budgets count every persisted attempt, including retries. Token totals rely on
  provider-reported usage and cannot enforce a precise pre-request token cap without a tokenizer.
- A crash after a provider accepts a request but before AgentForge persists the response can cause
  a later duplicate request and duplicate billing. Provider requests have no exactly-once claim.
- B2.4 real-model execution requires an exact opt-in, runtime-only API key, operator-confirmed
  Study digest, immutable authorization, clean matching source/dependency facts, and an ordered
  four-Protocol binding. A candidate authorization must pass the complete gate before its CAS is
  persisted; a rejected gate leaves the Study in DRAFT. The API key is excluded from persistence,
  reports, TestProfile environments, and command arguments.
- Formal TestProfiles use an evaluator-owned complete environment rather than inheriting the host.
  Python bytecode writes, user site packages, pytest config injection, and automatic external pytest
  plugin loading are disabled; only required fixed values and allowlisted Windows runtime roots are
  present.
- Deterministic source, Protocol, Provider, model, or execution-gate binding errors are
  configuration failures. They stop later Study work and cannot consume an infrastructure
  replacement.
- Real-model identity separates the requested alias from the exact Provider response ID. Formal
  preparation permits only the same ID or a strict `alias-YYYY-MM-DD` Snapshot and freezes both;
  execution rejects every other response ID, including a later Snapshot from the same family.
- B2.4 public artifacts use typed allowlisted fields and reject supplied secrets, Prompt
  substrings, Provider request IDs, Tool arguments/output, hidden/reference markers, unsafe pricing
  URLs, and POSIX/Windows/UNC absolute paths. All 12 planned slots remain visible without private
  UUIDs, raw output, or parameters. Private SQLite state and retained investigative workspaces
  remain ignored local data.
- Context limits use item count, characters, and UTF-8 bytes rather than model-specific tokens.
- Multi-function-call responses use explicit STRICT or SEQUENTIAL_READ_ONLY policy. Sequential
  normalization is allowed only when every call is a registered local READ tool without approval.
  Any WRITE, DANGEROUS, approval-required, non-local, unknown, or over-limit call makes the complete
  response STRICT.
- Discarded arguments and call IDs do not cross the Provider boundary. Discarded tool names may
  appear only in bounded audit events; checkpoints and normalization context retain counts but no
  discarded names or parameters.
- Three live fixture runs produced two completed read-only analyses and one Provider timeout before
  any response/tool execution. The timeout exhausted one retry and failed closed with no tool side
  effects.
- Real mutation-symlink tests remain skipped on this Windows account because link creation fails
  with `WinError 1314`. A simulated reparse-point test passes, but junctions and other Windows
  reparse variants are not comprehensively verified.
- Windows Job Object timeout/cancel/tree behavior was exercised with real child processes. The
  POSIX process-group test is platform-gated and was skipped in the final Windows run.
- A crash with ProcessExecutionRecord STARTED is always INDETERMINATE. Runtime does not reattach by
  PID, PGID, or Job ID and never automatically retries an uncertain test execution.
- Repair model-call accounting is bound to Runtime logical model steps in the current
  single-process Evaluation Mode. Provider physical attempts remain separately persisted by
  ModelWorkflow; exact cross-process reconciliation between a transmitted provider attempt and a
  repair decision remains technical debt.
- Repair budget facts are deduplicated by `(run_id, budget_kind, fact_id)` and terminal transitions
  use conditional version checks where control-flow races matter. The Evaluation Harness is
  serialized; distributed concurrent RepairState aggregation is not supported.

## A1 verification capsule boundary

- A final `VERIFIED` result attests the exact source and hidden-verifier bytes copied into an
  AgentForge-owned, per-execution verification capsule. The hidden test process receives only
  structured `SOURCE`, `VERIFIER`, and `SCRATCH` mount references; it does not execute against the
  mutable live workspace or a live hidden-test path.
- Capture copies and hashes each regular file in one pass, publishes a random-ID capsule through a
  same-filesystem rename, and recomputes both tree digests before binding it as `SEALED`. Runtime
  verifies the canonical source and verifier trees before launch and after process termination.
  An incomplete `STAGING` capsule and a capsule whose integrity check fails are never evidence for
  `VERIFIED`; their execution outcome is `INDETERMINATE`/`UNKNOWN`.
- `SEALED` means integrity checked and made read-only on a best-effort filesystem basis. It is not
  an operating-system immutability or sandbox claim. Scratch data is deliberately separate from
  the canonical captured source and verifier trees. A1 performs no automatic capsule garbage
  collection.
- The executable remains an operator-trusted `SYSTEM_RUNTIME`. AgentForge binds and repeatedly
  checks its file bytes and records `NON_HERMETIC`; it does not claim a bundled runtime, dependency
  closure, or hermetic execution. Python verification rewrites `PYTHONPATH` to captured source and
  disables user-site packages, bytecode writes, and automatic pytest plugin loading.
- This boundary is designed for accidental mutation and AgentForge-managed writes. The threat model
  excludes a malicious same-user or privileged external process able to rewrite AgentForge's
  artifact store or runtime during execution, and excludes a malicious operator-trusted verifier.
  OS sandboxing and a content-addressed runtime bundle remain future work.
