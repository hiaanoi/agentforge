# Milestone 5 TDD Plan

> Every production behavior starts with a focused failing test. M5 changes are not committed.

## File map

New production files:

- `src/agentforge/domain/mutations.py`: mutation plans, bindings, records, and result metadata.
- `src/agentforge/tools/mutation/base.py`: mutation tool protocol and limits.
- `src/agentforge/tools/mutation/security.py`: content and mutation-target validation.
- `src/agentforge/tools/mutation/atomic.py`: fsynced replace and no-clobber create.
- `src/agentforge/tools/mutation/write_file.py`: write_file schema, preflight, and execution.
- `src/agentforge/tools/mutation/edit_file.py`: exact replacement schema, preflight, execution.
- `src/agentforge/persistence/mutations.py`: binding/execution repositories and row conversion.
- `src/agentforge/persistence/mutation_workflow.py`: conditional claims, outcomes, and recovery.
- `src/agentforge/runtime/mutations.py`: Runtime-facing orchestration and safe result recovery.

Modified production files:

- `domain/enums.py`, `domain/models.py`, `domain/errors.py`, `persistence/tables.py`
- `tools/paths.py`, `tools/executor.py`, `policy/engine.py`
- `persistence/approval_workflow.py`, `runtime/engine.py`

## TDD sequence

### 1. Domain model

- Add failing `tests/unit/test_mutation_domain.py` cases for enum values, SHA-256 validation,
  CREATE_ONLY absence semantics, bounded summaries, immutable bindings, and forbidden source-text
  fields. Run that file and confirm missing imports fail before adding production types.

### 2. Persistence

- Add failing `tests/unit/test_mutation_persistence.py` cases for binding and execution round trips,
  uniqueness, ordering, restart durability, and Run isolation. Add tables and repositories only
  after the RED result.

### 3. Path and content security

- Add failing `tests/security/test_mutation_security.py` cases for traversal, absolute paths, UNC,
  missing parents, sensitive names, symlinks/reparse points, NUL, control characters, secret
  markers, and all byte limits. Extend `WorkspacePathResolver` and add mutation security helpers.

### 4. Atomic filesystem operations

- Add failing `tests/unit/test_atomic_mutation.py` cases for no-clobber create, expected-hash
  replacement, final-race rejection, mode preservation, fsync/replace failure, and temporary-file
  cleanup. Implement no-clobber hard-link publication and hash-checked `os.replace`.

### 5. write_file and edit_file

- Add failing `tests/unit/test_write_file.py` and `tests/unit/test_edit_file.py` cases for argument
  schemas, preflight plans, repeated validation, exact-one replacement, non-empty results, safe
  metadata, and absence of source text in ToolResult. Implement each tool independently.

### 6. Policy and approval binding

- First extend `tests/unit/test_policy.py`: local WRITE requires approval, approved WRITE is allowed,
  misconfigured WRITE and every DANGEROUS/non-local tool are denied.
- Extend approval workflow tests so checkpoint, approval, mutation binding and events are created
  atomically and reject paths never create executions.

### 7. Mutation workflow and crash recovery

- Add failing `tests/unit/test_mutation_workflow.py` cases for idempotent PREPARED creation, atomic
  PREPARED-to-WRITING claim, conditional COMMITTED/FAILED outcomes, WRITING-to-INDETERMINATE,
  COMMITTED recovery, changed-after-hash failure, and cross-Run conflicts.

### 8. Runtime integration

- Add failing `tests/integration/test_mutation_runtime.py` cases for unapproved no-write, approved
  single execution, reject/continue, repeated approve/resume, runtime recreation, the three crash
  windows, safe events, checkpoint recovery, query APIs, budgets, and multi-Run isolation.
- Use only MockModelProvider and temporary workspaces. Do not call a real model.

### 9. Regression and documentation

- Update project contract tests before README claims change.
- Update README, architecture, security model, implementation plan, and M5 report with actual
  results only.
- Run `uv run --frozen pytest -ra`, Ruff, strict mypy, compileall, and `git diff --check`.
