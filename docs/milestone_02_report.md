# Milestone 2 Final Report

**Status: PASS**

## 1. Initial audit

The initial working tree had 39 changed paths: 16 tracked modifications and 23 untracked paths.
They were classified and reviewed individually in `milestone_02_existing_changes_audit.md`.
All belonged to M2. No duplicate ToolExecutor, empty generated artifact, dependency churn, secret,
database, cache, virtual environment, or unrelated feature was found, so all were retained.

The audit identified five corrective areas: cancellation lacked a terminal event; schema mismatch
used a generic exception; invalid NUL paths were not classified as `INVALID_PATH`; sensitive or
absolute path arguments could be retained in requested-event payloads; and inherited `GIT_*`
variables could redirect the fixed Git command. Tests were written first and observed failing for
each behavior before the implementation was corrected.

## 2. Milestone 0/1 regression

The requested suites passed before corrective M2 work: domain 4/4, persistence 4/4,
model-and-tools 3/3, and runtime integration 7/7. Existing tests were not removed or weakened.
No M0/1 interface break was found.

## 3. Milestone 2 implementation

- **ToolRegistry:** typed sync/async bindings, deterministic lookup/list/schema export, Pydantic
  argument validation, source tracking, duplicate-domain errors, and invalid-spec domain errors.
- **ToolExecutor:** one lifecycle for lookup, validation, policy, budget, requested/started/terminal
  events, sync/async invocation, timeout, cancellation audit, stable failures, duration, and bounds.
- **Policy Engine:** structured ALLOW/DENY/REQUIRE_APPROVAL decisions for registration, arguments,
  cancellation, budgets, source, risk, approval, resolved paths, sensitive files, and allowed paths.
- **WorkspacePathResolver:** rejects empty, invalid, absolute, drive-qualified, UNC, missing,
  wrong-type, traversing, and resolved external paths with Windows case normalization.
- **Sensitive policy:** centralized case-insensitive environment, key, Git credential, and filename
  keyword rules applied to canonical relative paths.
- **Repository tools:** stable bounded `list_files`, strict UTF-8 `read_file`, Python-only
  `search_text`, and fixed read-only `get_git_diff`; no write or arbitrary command tools exist.
- **Event auditing:** requested, started, and terminal sequences with minimized payloads; denied
  calls never start; external cancellation records `TOOL_CANCELLED` then propagates.
- **Budget:** persisted per-Run `max_tool_calls`, checked before execution and incremented once for
  allowed attempts, with isolated tests across Runs.

## 4. Security boundaries

Protected behavior includes traversal and link escape, absolute/drive/UNC input, sensitive files,
binary and invalid UTF-8 reads, oversized search inputs/files/results, non-read risks, approval
requirements, non-local sources, budget exhaustion, unexpected exceptions, traceback leakage,
model-facing output growth, Git argument injection, and inherited Git control variables.

Milestone 2 does not provide an OS sandbox. Filesystem validation has a check/open race. A timed-out
synchronous worker thread can continue running, so only local `READ` tools are executable.
`WRITE`, `DANGEROUS`, and `REQUIRE_APPROVAL` decisions block execution in this milestone.

Directory walks skip symlinks, and the resolver implements canonical containment checks intended
to allow internal targets while denying external targets. The two real symlink tests were skipped
because the current Windows user cannot create symlinks (`WinError 1314`), so this logic is not
runtime-verified in the current environment. Windows junctions and other reparse-point variants
have not been comprehensively tested. Git stdout is captured in memory before the returned-character
bound is applied.

## 5. Final verification

- Python: 3.14.3.
- Full pytest suite: 80 collected, 78 passed, 2 skipped, 0 failed.
- Both skips: Windows `WinError 1314`; the current user cannot create symlinks.
- Git/Diff focused suite: 5 passed, including all three real Git subprocess tests.
- Ruff: `All checks passed!`.
- strict mypy: 30 source files, no issues.
- compileall: passed.

The complete test and static-analysis evidence satisfies the Milestone 2 acceptance criteria.

## 6. Dependencies and environment

Python remains 3.14.3. `pyproject.toml`, `uv.lock`, dependency versions, and dependency source were
not changed. No dependency was added. The lock remains on official PyPI and contains no Aliyun
mirror URL.

## 7. Not implemented

Human approval execution, write/apply-patch/test tools, arbitrary shell commands, network tools,
real model APIs, FastAPI, MCP execution, LangGraph, recovery APIs, distributed workers,
containers, evaluation infrastructure, and multi-agent orchestration remain outside M2.

## 8. Known debt

Atomic run/event/budget transactions, schema migrations, concurrent same-Run budget arbitration,
process-isolated synchronous tools, streaming Git capture, OS filesystem/network sandboxing,
schema-declared secret fields, and comprehensive Windows junction/reparse-point verification are
not implemented. Symlink protection is implemented but remains unverified on this Windows account
because both real symlink tests were skipped.
