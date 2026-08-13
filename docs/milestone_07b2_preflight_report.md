# Milestone 7-B2.0 Selected Candidate Preflight Report

## Decision

**PASS for B2.0 only.** All ten accepted candidates have a versioned, evidence-classified
preflight result. Six are `GO`, four are `CONDITIONAL`, and none are `NO_GO` or `UNVERIFIED` at
the candidate gate level. Full M7-B2 is not PASS because no formal fixture, visible/hidden test
package, reference fix, or model evaluation exists.

## Primary gates

| Candidate | Gate | Most important evidence or condition |
| --- | --- | --- |
| `quixbugs-shortest-path-length` | GO | Original buggy/correct tests separated 3x on Windows Python 3.14 |
| `quixbugs-topological-ordering` | CONDITIONAL | Execution is stable; B2.1 must replace exact-order assertions with a semantic oracle |
| `bugsinpy-black-21` | GO | Exact helper separated 3x on Python 3.8 and 3.14 under GBK |
| `bugsinpy-pysnooper-3` | CONDITIONAL | Exact failure is stable; formal crop must retain tracer/decorator symptom distance |
| `bugsinpy-httpie-4` | CONDITIONAL | Offline source and 3.14 probes pass; support mapping equivalence must be demonstrated |
| `swebench-flask-5014` | GO | Official regression separated 3x on 3.10 and direct source behavior separated 3x on 3.14 |
| `swebench-pytest-10051` | GO | Official regression and 3.14 alias-lifecycle probe both separated 3x |
| `self-durable-double-consumption` | GO | Four-module design, crash/CAS hidden matrix, leakage controls, and two-round path close |
| `self-policy-priority-shadow` | GO | Four-module ranking contract and independent permutation oracle close |
| `self-async-cancel-cleanup` | CONDITIONAL | B2.1 must prove barrier-driven cancellation tests without sleeps |

## Demo and replacement decision

`self-durable-double-consumption` remains the recommended main demo. It uses parcel-domain
terminology, at least four business modules, an external duplicate symptom, durable restart and
concurrency hidden checks, and no AgentForge class, state, table, or event names.
`self-terminal-state-cas` remains the registered backup and was not automatically selected.

No primary candidate is automatically replaced. If the HTTPie mapping-equivalence condition
fails, the registered same-source fallback is `bugsinpy-cookiecutter-2`; that replacement would
require a new human decision.

## Difficulty review

No label changes are recommended. The accepted 6 BASIC / 3 ENGINEERING / 1 CHALLENGE mix remains
supported by actual patch size, module relationships, expected feedback rounds, symptom distance,
and hidden-test complexity. No model result influenced difficulty.

## Recommended first four fixtures

1. `quixbugs-shortest-path-length`: simplest end-to-end public-source construction proof.
2. `bugsinpy-black-21`: Windows encoding and historical-source adaptation proof.
3. `swebench-pytest-10051`: multi-module aliasing and lifecycle engineering proof.
4. `self-durable-double-consumption`: original durable-systems main demo.

This set covers all four source families and BASIC plus ENGINEERING behavior. Conditional items
remain outside the first batch until their named conditions pass.

## Human decision record

On 2026-07-17, the user:

- approved the proposed four-item first fixture batch;
- authorized CONDITIONAL candidates to be constructed with explicit admission gates in B2.1;
- accepted the current no-replacement decision;
- authorized entry into M7-B2.1.

Authorization is not execution. At B2.0 closeout, formal fixture construction and M7-B2.1 have
not started, and no model run is authorized until formal fixture verification is complete.

## Boundaries

- No formal fixture or task source was added.
- No formal visible or hidden test or reference fix was authored.
- No model was invoked.
- AgentForge Runtime, `pyproject.toml`, and `uv.lock` were not changed.
- No commit, tag, or push is part of B2.0 implementation.
- M7-B2.1 has not started.

## Acceptance evidence

- `uv run --frozen pytest -ra`: 355 collected, 346 passed, 9 skipped, 0 failed
- Preflight asset tests: 10 passed
- The nine skips are the existing live, POSIX-only, executable-bit, and Windows symlink privilege cases
- `uv run --frozen ruff check .`: passed
- `uv run --frozen mypy src`: passed for 85 source files
- `uv run --frozen python -m compileall src`: passed
- `git diff --check`: passed
