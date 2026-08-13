# Milestone 7-B2.1 Formal Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify four offline, schema-validated repair fixtures with isolated visible and hidden tests and auditable reference overlays.

**Architecture:** Store each fixture as immutable evaluator assets plus an editable workspace. A standalone verifier validates manifests and hashes, creates fresh temporary copies, applies declared fixed-file overlays, and runs fixed pytest commands in a minimal environment.

**Tech Stack:** Python 3.14 standard library, pytest, JSON Schema assets validated by repository tests, SQLite for the original durable fixture.

---

### Task 1: Freeze the fixture asset contract

**Files:**
- Create: `evaluation/fixtures/fixture_schema.json`
- Create: `evaluation/fixtures/registry.json`
- Create: `tests/evaluation/test_fixture_assets.py`

- [ ] Write tests requiring schema version `1.0.0`, exactly four authorized registry entries,
  normalized contained paths, disjoint workspace/protected paths, fixed argv commands, and complete
  SHA-256 maps.
- [ ] Run `uv run --frozen pytest tests/evaluation/test_fixture_assets.py -q` and confirm failure
  because the fixture contract does not exist.
- [ ] Add the minimal schema and registry structure that satisfies the contract tests.
- [ ] Re-run the focused tests and keep all existing candidate/preflight asset tests green.

### Task 2: Add the isolated verifier

**Files:**
- Create: `evaluation/fixtures/verify_fixtures.py`
- Test: `tests/evaluation/test_fixture_verifier.py`

- [ ] Write tests proving fresh-copy execution, fixed cwd/argv, minimal environment, bounded output,
  timeout failure, reference overlay containment, hidden-test separation, and versioned reports.
- [ ] Run the verifier tests and confirm expected missing-module failures.
- [ ] Implement validation, copy, overlay, subprocess, timeout, and report composition using only the
  standard library.
- [ ] Re-run verifier and asset tests until green.

### Task 3: Build QuixBugs and BugsInPy fixtures

**Files:**
- Create task trees under `evaluation/fixtures/tasks/quixbugs-shortest-path-length/`
- Create task trees under `evaluation/fixtures/tasks/bugsinpy-black-21/`

- [ ] Add visible and hidden tests first and execute each against an empty/buggy crop to observe the
  intended failures.
- [ ] Add the minimal buggy source, task manifests, attribution, and fixed-file overlays.
- [ ] Verify buggy visible/hidden failure and reference visible/hidden success once per task.
- [ ] Add anti-hardcoding cases for generated graph variants and multiple non-ASCII encodings.

### Task 4: Build SWE-bench pytest phase fixture

**Files:**
- Create task tree under `evaluation/fixtures/tasks/swebench-pytest-10051/`

- [ ] Add phase lifecycle tests first and observe stale-list failures against the buggy clear
  behavior.
- [ ] Add the phase capture crop, manifest, attribution, and one-file reference overlay.
- [ ] Verify all phases, repeated clear, post-clear records, isolation, and identity semantics.

### Task 5: Build the original durable dispatch fixture

**Files:**
- Create task tree under `evaluation/fixtures/tasks/self-durable-double-consumption/`

- [ ] Add visible restart and hidden durable-invariant tests first, using events/barriers instead of
  timing sleeps.
- [ ] Implement the four-module buggy SQLite package with the single separate-commit defect.
- [ ] Observe duplicate dispatch, repeated recovery, and concurrent-owner failures.
- [ ] Add a private fixed-file overlay implementing conditional ownership, unique dispatch storage,
  and completed-result reuse; verify all tests pass without importing AgentForge code.

### Task 6: Generate immutable metadata and run three-repeat admission

**Files:**
- Create: `evaluation/fixtures/build_assets.py`
- Create: `evaluation/fixtures/verification_report.json`
- Modify: all four `task_manifest.json` files
- Test: `tests/evaluation/test_fixture_assets.py`

- [ ] Write tests requiring deterministic digest generation and a complete three-repeat report.
- [ ] Implement deterministic manifest/registry generation and immutable-file hashing.
- [ ] Run `uv run --frozen python evaluation/fixtures/verify_fixtures.py --repeat 3` and require each
  buggy suite to fail and each reference suite to pass on all repetitions.
- [ ] Re-run focused asset and verifier tests.

### Task 7: Close B2.1 documentation and regression

**Files:**
- Create: `docs/milestone_07b2_fixture_report.md`
- Modify: `evaluation/preflight/gate_summary.json`

- [ ] Record actual commands, repetitions, failure reasons, runtimes, source attribution, isolation
  checks, and remaining limitations without claiming model evaluation.
- [ ] Mark formal fixture construction and B2.1 complete while keeping model evaluation false.
- [ ] Run `uv run --frozen pytest -ra`, `uv run --frozen ruff check .`,
  `uv run --frozen mypy src`, `uv run --frozen python -m compileall src`, and `git diff --check`.
- [ ] Stop before M7-B2.2, model invocation, commit, tag, or push unless separately authorized.
