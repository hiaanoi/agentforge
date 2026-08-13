# Milestone 7-B2.0 Selected Candidate Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce execution-backed GO/CONDITIONAL/NO_GO/UNVERIFIED decisions for the ten human-approved M7-B1 candidates without building formal fixtures or running a model.

**Architecture:** Keep external repositories, source virtual environments, and disposable crop spikes under a verified temporary directory outside AgentForge. Store only bounded, redacted, machine-readable evidence summaries in `evaluation/preflight/`; generate CSV, gate, difficulty, demo, replacement, and registry projections deterministically from one definition module. Asset tests enforce hard-gate semantics and repository boundaries.

**Tech Stack:** Python 3.14 standard library, JSON Schema 2020-12, CSV, pytest, Git, external disposable Python environments where source verification requires them.

**Constraints:** No Runtime changes, dependency or lockfile changes, formal fixtures, task tests, reference-fix ports, model runs, commit, tag, push, or M7-B2.1 implementation.

---

### Task 1: Freeze the preflight contract

**Files:**
- Create: `tests/evaluation/test_preflight_assets.py`
- Create: `evaluation/preflight/preflight_schema.json`

- [x] Write tests for the complete result schema, evidence classifications, final-gate rules, ten primary IDs, JSON/CSV consistency, replacements, decision metadata, secrets, absolute paths, artifact hashes, and third-party file limits.
- [x] Run `uv run --frozen pytest tests/evaluation/test_preflight_assets.py -q` and verify RED because the preflight assets do not exist.
- [x] Define closed schema version `1.0.0` with `GO`, `CONDITIONAL`, `NO_GO`, and `UNVERIFIED` gates and explicit `VERIFIED`, `INFERRED`, and `UNVERIFIED` evidence.

### Task 2: Verify public source facts and executable baselines

**Files:**
- Create outside repository: `.tmp-agentforge-m7b2-preflight/`
- Create: `evaluation/preflight/build_assets.py`

- [x] Clone only official QuixBugs, Black, PySnooper, HTTPie, Flask, and pytest repositories outside AgentForge and verify pinned revisions and licenses.
- [x] For each public candidate, run the narrowest trustworthy buggy and fixed check three times where compatible; record command shape, exit class, digest, durations, dependency/Python environment, and any inability to execute.
- [x] Build disposable crop spikes outside AgentForge only when needed to verify Python 3.14, Windows, offline, fixed-profile, and semantic-preservation feasibility.
- [x] Record patch files, effective line counts, test/dependency changes, source/support/test file requirements, and bounded evidence summaries without raw logs or local paths.

### Task 3: Complete self-built design preflight

**Files:**
- Generate: `evaluation/preflight/primary_preflight_results.json`
- Generate: `evaluation/preflight/demo_preflight.json`

- [x] Define symptoms, at least three business modules, invariants, buggy mechanisms, repair principles, visible/hidden plans, two incorrect patches, protected paths, edit rounds, and leakage risks for all three primary self-built candidates.
- [x] Verify the durable demo avoids AgentForge names and direct solution language, has a visible symptom plus restart/reuse/concurrency hidden matrix, and supports a two-round interview narrative.
- [x] Perform the same design-level check for `self-terminal-state-cas` as the registered demo backup without building code.

### Task 4: Apply gates, difficulty review, and replacement rules

**Files:**
- Generate: `evaluation/preflight/primary_preflight_matrix.csv`
- Generate: `evaluation/preflight/alternate_preflight_results.json`
- Generate: `evaluation/preflight/gate_summary.json`
- Generate: `evaluation/preflight/replacement_recommendations.json`
- Generate: `evaluation/preflight/difficulty_review.json`
- Generate: `evaluation/preflight/artifact_registry.json`

- [x] Apply GO only when every hard gate has verified or design-closed evidence; use CONDITIONAL for one or two explicit B2.1-resolvable risks and UNVERIFIED for missing execution evidence.
- [x] Preflight an alternate only when a same-source primary is NO_GO or high-risk CONDITIONAL; never mutate the accepted primary list automatically.
- [x] Review difficulty using files, patch size, cross-module distance, feedback rounds, and hidden-test complexity only.
- [x] Recommend four representative B2.1 fixtures spanning source, difficulty, and engineering behavior.
- [x] Run the focused asset tests and verify GREEN.

### Task 5: Write the human preflight record

**Files:**
- Create: `docs/milestone_07b2_preflight_protocol.md`
- Create: `docs/milestone_07b2_preflight_report.md`
- Create: `docs/milestone_07b2_environment_findings.md`
- Create: `docs/milestone_07b2_crop_risk_register.md`
- Create: `docs/milestone_07b2_license_review.md`

- [x] Document source and execution methodology, environment separation, every primary gate, buggy/fixed evidence, uncertainty, crop risks, hidden-test risks, demo decision, replacements, difficulty review, and first-four proposal.
- [x] State explicitly why B2.0 may pass while full M7-B2 cannot pass before formal fixtures, tests, reference fixes, and model evaluation exist.

### Task 6: Acceptance and cleanup

- [x] Run `uv run --frozen pytest -ra`.
- [x] Run `uv run --frozen ruff check .`.
- [x] Run `uv run --frozen mypy src`.
- [x] Run `uv run --frozen python -m compileall src`.
- [x] Run `git diff --check` after all files are visible to Git.
- [x] Verify `src/`, `pyproject.toml`, and `uv.lock` are unchanged and no external source tree, environment, cache, build product, secret, or absolute temporary path entered AgentForge.
- [x] Remove the verified external temporary directory and stop at the M7-B2.1 human decision gate without commit, tag, push, fixture construction, or model execution.
