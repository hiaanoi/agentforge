# Milestone 7-B1 Final Report

## Decision

**PASS.** M7-B1 produced a traceable, scored repair-task candidate audit. The user accepted the
primary list for B2 preflight with no replacements, accepted the 6/3/1 difficulty mix, selected
`self-durable-double-consumption` as the main demo, and selected `self-terminal-state-cas` as its
backup. M7-B1 did not build fixtures, author task tests or reference fixes, run a model, modify
AgentForge Runtime behavior, or begin M7-B2.1.

## Preconditions

- Starting branch: `feat/milestone-7a`
- M7-A commit: `3f58e3641614d2ca781eec3aa15ea5e52cbb8c6d`
- Required tag: `milestone-7a-complete`
- Starting worktree: clean
- Working branch created: `feat/milestone-7b1-candidate-audit`
- Baseline before M7-B1: 337 collected, 328 passed, 9 skipped

## Audit Method

The audit froze a versioned candidate schema before selection, then checked public candidates
against first-party repositories, issues or pull requests, pinned commits, original tests, and
project licenses. Temporary source checkouts remained outside AgentForge and were not copied
into the repository.

Each candidate field is classified as `VERIFIED`, `INFERRED`, or `UNVERIFIED`. Runtime,
Windows, Python 3.14, crop fidelity, hidden-test strength, and fixture-size estimates remain
inferences until M7-B2 performs controlled fixture trials.

The canonical machine-readable records are under `evaluation/candidates/`. JSON/CSV
consistency, schema validity, score arithmetic, list membership, source quotas, difficulty
quotas, license gates, provenance fields, secret patterns, absolute paths, and artifact sizes
are checked by `tests/evaluation/test_candidate_assets.py`.

## Candidate Pool and Hard Gates

| Source | Audited | Eligible | Conditional | Rejected |
| --- | ---: | ---: | ---: | ---: |
| QuixBugs | 4 | 2 | 1 | 1 |
| BugsInPy | 6 | 3 | 2 | 1 |
| SWE-bench Verified | 5 | 2 | 3 | 0 |
| Self-built concepts | 5 | 5 | 0 | 0 |
| **Total** | **20** | **12** | **6** | **2** |

The ten primary recommendations pass the M7-B1 evidence and license gates. Local/offline
execution, deterministic crop behavior, Windows compatibility, and current-Python compatibility
are design assessments, not completed fixture validation.

Two candidates failed hard gates:

- `bugsinpy-tqdm-8` (64): historical source-license status remains unresolved, so it cannot be
  redistributed as a formal task.
- `quixbugs-bitcount` (52): the failure is nontermination, which raises timeout-containment risk,
  and the adaptation has weak engineering realism.

## Scoring and Recommendations

The common 100-point rubric scores reproducibility, Runtime compatibility, engineering realism,
hidden-test strength, adaptation cost, diversity, demo value, and provenance. Score alone does
not determine selection; hard gates and portfolio diversity apply first.

### Primary Set

| Candidate | Score | Difficulty | Demo |
| --- | ---: | --- | --- |
| `quixbugs-shortest-path-length` | 85 | BASIC | No |
| `quixbugs-topological-ordering` | 84 | BASIC | No |
| `bugsinpy-black-21` | 84 | BASIC | No |
| `bugsinpy-pysnooper-3` | 82 | BASIC | No |
| `bugsinpy-httpie-4` | 83 | BASIC | No |
| `swebench-flask-5014` | 89 | BASIC | No |
| `swebench-pytest-10051` | 92 | ENGINEERING | No |
| `self-durable-double-consumption` | 88 | ENGINEERING | **Recommended main demo** |
| `self-policy-priority-shadow` | 89 | ENGINEERING | No |
| `self-async-cancel-cleanup` | 86 | CHALLENGE | No |

The source mix is exactly 2 QuixBugs, 3 BugsInPy, 2 SWE-bench, and 3 self-built concepts. The
suggested difficulty mix is 6 BASIC, 3 ENGINEERING, and 1 CHALLENGE.

### Alternates

- `quixbugs-flatten` (78, CONDITIONAL)
- `bugsinpy-cookiecutter-2` (80, CONDITIONAL)
- `bugsinpy-fastapi-3` (77, CONDITIONAL)
- `swebench-requests-5414` (85, CONDITIONAL)
- `swebench-pylint-6386` (78, CONDITIONAL)
- `swebench-pytest-5840` (80, CONDITIONAL)
- `self-terminal-state-cas` (87, ELIGIBLE)
- `self-adapter-default-propagation` (87, ELIGIBLE)

The two eligible self-built alternates are held back to avoid source imbalance and semantic
overlap, not because they failed hard gates.

## License, Provenance, and Adaptation Risks

- QuixBugs is MIT. Selected BugsInPy source projects were checked individually because the
  BugsInPy collection has no single top-level redistribution license covering all projects.
- Selected SWE-bench candidates retain their source-project license obligations; the benchmark
  repository license does not replace those obligations.
- Self-built entries are concepts only. M7-B2 must author original code, names, domains, and
  tests rather than copy AgentForge internals.
- Every public fixture will need file-level attribution and a minimal-source review before
  inclusion.
- The largest B2 risks are changing bug semantics during cropping, historical dependency
  incompatibility with Python 3.14, weak hidden tests for one-line fixes, Windows-specific
  behavior, and accidental leakage from reference patches.

`docs/milestone_07b1_license_and_provenance.md` and
`docs/milestone_07b1_risk_register.md` contain the detailed evidence and B2 gates.

## Acceptance Evidence

- `uv run --frozen pytest -ra`: 345 collected, 336 passed, 9 skipped, 0 failed
- Candidate asset tests: 8 passed
- `uv run --frozen ruff check .`: passed
- `uv run --frozen mypy src`: passed for 85 source files
- `uv run --frozen python -m compileall src`: passed
- `git diff --check`: passed

The nine skips are existing platform/live-test constraints; M7-B1 introduced no new skips.
There are no dependency changes, no `uv.lock` changes, no copied external repository, and no
Agent Runtime source changes.

## Human Decision Record

- Primary list: accepted for B2 preflight
- Replacements: none
- Main demo: `self-durable-double-consumption`
- Main demo backup: `self-terminal-state-cas`
- Difficulty mix: 6 BASIC / 3 ENGINEERING / 1 CHALLENGE accepted
- CONDITIONAL candidates: validate in source-tiered order
- Authorized next phase: M7-B2.0 preflight only

This decision does not authorize formal fixtures, visible or hidden task tests, reference fixes,
model runs, prompt or budget changes, or M7-B2.1 implementation.
