# Milestone 7-B1 Repair Candidate Audit

## Scope and evidence standard

M7-B1 audits candidate facts and proposes a selection. It does not build fixtures, write
visible or hidden tests, port a reference fix, run a model, or begin M7-B2.

The canonical record is `evaluation/candidates/candidates.json`, versioned by
`candidate_schema.json`. Every candidate field has a `field_evidence` entry:

- `VERIFIED`: checked against a first-party repository, issue/PR, commit, test, dataset row,
  or license at a pinned revision.
- `INFERRED`: an AgentForge adaptation estimate, including runtime, fixture size, hidden-test
  strength, Windows feasibility, and difficulty.
- `UNVERIFIED`: unresolved and excluded from any claim of completed feasibility.

The audit used temporary clones outside AgentForge for QuixBugs and BugsInPy metadata and the
official SWE-bench Verified dataset API. No third-party source tree or fixture was copied into
this repository.

## Candidate pool

| Source | Count | Eligible | Conditional | Rejected |
| --- | ---: | ---: | ---: | ---: |
| QuixBugs | 4 | 2 | 1 | 1 |
| BugsInPy | 6 | 3 | 2 | 1 |
| SWE-bench Verified | 5 | 2 | 3 | 0 |
| Self-built concepts | 5 | 5 | 0 | 0 |
| **Total** | **20** | **12** | **6** | **2** |

Primary public evidence includes the [QuixBugs repository](https://github.com/jkoppel/QuixBugs),
the [BugsInPy repository](https://github.com/soarsmu/BugsInPy), and the
[SWE-bench Verified dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified).
Per-candidate pinned files, commits, issues, tests, and licenses are recorded in the canonical
JSON rather than duplicated here.

## Scored matrix

| Candidate | Source | Score | Status | Suggested difficulty | Proposed disposition |
| --- | --- | ---: | --- | --- | --- |
| `swebench-pytest-10051` | SWE-bench | 92 | ELIGIBLE | ENGINEERING | Primary |
| `swebench-flask-5014` | SWE-bench | 89 | ELIGIBLE | BASIC | Primary |
| `self-policy-priority-shadow` | Self-built | 89 | ELIGIBLE | ENGINEERING | Primary |
| `self-durable-double-consumption` | Self-built | 88 | ELIGIBLE | ENGINEERING | Primary/demo |
| `self-terminal-state-cas` | Self-built | 87 | ELIGIBLE | ENGINEERING | Alternate: semantic overlap |
| `self-adapter-default-propagation` | Self-built | 87 | ELIGIBLE | BASIC | Alternate: quota/diversity |
| `self-async-cancel-cleanup` | Self-built | 86 | ELIGIBLE | CHALLENGE | Primary |
| `quixbugs-shortest-path-length` | QuixBugs | 85 | ELIGIBLE | BASIC | Primary |
| `swebench-requests-5414` | SWE-bench | 85 | CONDITIONAL | BASIC | Alternate |
| `quixbugs-topological-ordering` | QuixBugs | 84 | ELIGIBLE | BASIC | Primary |
| `bugsinpy-black-21` | BugsInPy | 84 | ELIGIBLE | BASIC | Primary |
| `bugsinpy-httpie-4` | BugsInPy | 83 | ELIGIBLE | BASIC | Primary |
| `bugsinpy-pysnooper-3` | BugsInPy | 82 | ELIGIBLE | BASIC | Primary |
| `bugsinpy-cookiecutter-2` | BugsInPy | 80 | CONDITIONAL | ENGINEERING | Alternate |
| `swebench-pytest-5840` | SWE-bench | 80 | CONDITIONAL | ENGINEERING | Alternate |
| `quixbugs-flatten` | QuixBugs | 78 | CONDITIONAL | BASIC | Alternate |
| `swebench-pylint-6386` | SWE-bench | 78 | CONDITIONAL | ENGINEERING | Alternate |
| `bugsinpy-fastapi-3` | BugsInPy | 77 | CONDITIONAL | ENGINEERING | Alternate |
| `bugsinpy-tqdm-8` | BugsInPy | 64 | REJECTED | BASIC | Rejected: license unresolved |
| `quixbugs-bitcount` | QuixBugs | 52 | REJECTED | BASIC | Rejected: timeout/toy risk |

## Primary recommendation

The proposed ten-item set is exactly 2 QuixBugs, 3 BugsInPy, 2 SWE-bench, and 3 self-built
tasks. Its suggested mix is 6 BASIC, 3 ENGINEERING, and 1 CHALLENGE.

The primary choices are not simply the ten highest scores. `self-terminal-state-cas` and
`self-adapter-default-propagation` score well but are held as alternates to avoid self-built
over-representation and durable-state duplication. `swebench-requests-5414` is held because
its validation shape overlaps the Flask invariant task.

The suggested main demo is `self-durable-double-consumption`: its user-visible duplicate
effect, crash window, persistent facts, and exact-once invariant are understandable in a demo
without requiring external services. This remains a proposal for human approval.

## Hard-gate results

- All ten primary candidates are Python tasks or Python task concepts.
- All ten have verified permissive upstream licenses or are original concepts with no code yet.
- Public primary candidates have traceable buggy/fixed sources and regression evidence.
- Proposed primary execution is local and offline, but adapted fixtures are not yet built or
  run; that claim remains inferred.
- No primary candidate requires shell, Git write operations, network access, a database,
  native builds, or dependency installation in the proposed task run.
- Hidden-test and anti-hardcoding plans exist for every primary candidate, but no hidden tests
  have been authored.

## B2 risks requiring confirmation

1. QuixBugs graph tasks are source-similar; consider replacing one with `quixbugs-flatten` only
   if a faithful engineering adaptation can be designed.
2. Black and PySnooper have one-line fixes; B2 must preserve realistic caller context without
   padding the fixture with irrelevant code.
3. HTTPie must retain case-insensitive header semantics while removing all transport/network
   execution.
4. The pytest caplog crop must preserve reference aliasing and phase lifecycle; a simplified
   logger wrapper would change the bug.
5. Self-built tasks must use distinct domains and independently authored tests rather than
   copying AgentForge state names, repositories, or existing regressions.

The full rationale, risks, and B2 questions for each recommended item are machine-readable in
`recommended_primary.json` and `recommended_alternates.json`.
