# Milestone 7-B1 Candidate Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a traceable, license-aware, machine-validated candidate audit for the later M7-B repair fixture selection gate.

**Architecture:** Treat `evaluation/candidates/candidates.json` as the canonical audit record. Generate the CSV projection and selection artifacts from the same definitions, attach field-level verification status and evidence references, and verify every artifact with standard-library-only pytest tests. External repositories remain temporary research inputs outside AgentForge and are never copied into the repository.

**Tech Stack:** Python 3.14 standard library, JSON Schema 2020-12 as a data contract, CSV, pytest, Ruff, mypy, Git.

**Constraint:** The implementation stage forbade commits, tags, pushes, fixture construction, runtime changes, and real-model evaluation. The user later authorized a dedicated M7-B1 baseline commit and tag after accepting the selection.

---

### Task 1: Freeze the asset contract

**Files:**
- Create: `tests/evaluation/test_candidate_assets.py`
- Create: `evaluation/candidates/candidate_schema.json`

- [x] Write failing tests for required fields, enums, scores, evidence, selections, CSV consistency, secret/path scanning, and artifact hashes.
- [x] Run `uv run --frozen pytest tests/evaluation/test_candidate_assets.py -q` and verify it fails because the asset directory is absent.
- [x] Generate JSON Schema version `1.0.0` with closed candidate objects and explicit source/audit/evidence enums.

### Task 2: Audit first-party public sources

**Files:**
- Create: `evaluation/candidates/candidates.json`

- [x] Verify QuixBugs programs, paired corrected sources, tests, pinned repository revision, and MIT license.
- [x] Verify BugsInPy metadata and patches, then verify each upstream project commit and license independently.
- [x] Verify selected SWE-bench Verified rows against the official dataset, original issues or pull requests, merge commits, tests, and repository licenses.
- [x] Encode every field as `VERIFIED`, `INFERRED`, or `UNVERIFIED`; do not promote proposed adaptation claims to verified facts.

### Task 3: Score and select candidates

**Files:**
- Create: `evaluation/candidates/recommended_primary.json`
- Create: `evaluation/candidates/recommended_alternates.json`
- Create: `evaluation/candidates/rejected_candidates.json`
- Create: `evaluation/candidates/self_built_candidate_briefs.json`

- [x] Apply the same eight-category 100-point rubric to every candidate.
- [x] Produce a ten-item primary recommendation with the requested source distribution and a defensible 6 BASIC / 3 ENGINEERING / 1 CHALLENGE mix.
- [x] Partition all remaining candidates into alternates or explicit rejections without overlap.
- [x] Record the manual questions that must be answered before M7-B2.

### Task 4: Generate and validate derived assets

**Files:**
- Create: `evaluation/candidates/build_assets.py`
- Create: `evaluation/candidates/candidate_matrix.csv`
- Create: `evaluation/candidates/artifact_registry.json`

- [x] Generate all machine-readable projections deterministically from one candidate definition set.
- [x] Hash and size-register every candidate asset except the registry itself.
- [x] Run `uv run --frozen pytest tests/evaluation/test_candidate_assets.py -q` and verify all asset tests pass.

### Task 5: Write the human audit record

**Files:**
- Create: `docs/milestone_07b1_candidate_audit.md`
- Create: `docs/milestone_07b1_selection_protocol.md`
- Create: `docs/milestone_07b1_license_and_provenance.md`
- Create: `docs/milestone_07b1_risk_register.md`
- Create: `docs/milestone_07b1_report.md`

- [x] Document method, source distribution, hard-gate results, scores, primary/alternate/rejected lists, and evidence status.
- [x] Document license obligations and unresolved provenance risks without giving legal advice.
- [x] Document B2 adaptation risks and the six explicit user decisions required at the gate.

### Task 6: Acceptance and cleanup

- [x] Run `uv run --frozen pytest -ra`.
- [x] Run `uv run --frozen ruff check .`.
- [x] Run `uv run --frozen mypy src`.
- [x] Run `uv run --frozen python -m compileall src`.
- [x] Run `git diff --check`.
- [x] Verify `pyproject.toml` and `uv.lock` are unchanged and no third-party source tree is present in AgentForge.
- [x] Remove the external temporary audit clones after checking the resolved path.
- [x] Stop at the human decision gate without creating a fixture or M7-B2 implementation; record the later human approval for B2.0 preflight.
