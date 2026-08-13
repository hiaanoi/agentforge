# Milestone 7-B2.1 Formal Fixture Report

## Decision

**PASS for M7-B2.1.** The four human-approved first-batch repair fixtures now have versioned
manifests, editable buggy workspaces, isolated visible and hidden tests, immutable reference
overlays, attribution, and a three-repetition verification report. M7-B2.2 model evaluation has
not started and remains unauthorized.

## Admitted fixtures

| Task | Source | Difficulty | Repair boundary |
| --- | --- | --- | --- |
| `quixbugs-shortest-path-length` | QuixBugs | BASIC | Correct the relaxation distance |
| `bugsinpy-black-21` | BugsInPy / Black | BASIC | Force UTF-8 for diagnostic text |
| `swebench-pytest-10051` | SWE-bench / pytest | ENGINEERING | Clear the bound phase list in place |
| `self-durable-double-consumption` | AgentForge original | ENGINEERING | Preserve one durable dispatch across restart and concurrent ownership |

Public-source crops retain pinned revisions and MIT attribution. Their evaluator tests are new,
fixture-specific tests rather than copies of upstream test suites. The parcel fixture is original
project work and imports no AgentForge Runtime implementation.

## Verification evidence

The formal command was:

```text
uv run --frozen python evaluation/fixtures/verify_fixtures.py --repeat 3 --output evaluation/fixtures/verification_report.json
```

The report records:

- 4 tasks;
- 3 fresh-copy repetitions;
- visible and hidden suites in buggy and reference states;
- 48 isolated pytest process executions;
- 24 expected buggy-state failures;
- 24 reference-state passes;
- 0 unexpected outcomes;
- 0 timeouts;
- no model invocation.

Every task failed both evaluator suites in its buggy state and passed the same suites after only
its declared fixed-file overlay was applied. Execution roots were discarded after each attempt.
The persisted report replaces ephemeral execution roots and interpreter installation roots with
stable redaction markers.

Repository-wide acceptance on Windows Python 3.14.3 produced:

- `uv run --frozen pytest -ra`: 389 collected, 380 passed, 9 skipped, 0 failed;
- focused fixture and verifier tests: 34 passed;
- fixture plus preflight state tests: 44 passed;
- `uv run --frozen ruff check .`: passed;
- `uv run --frozen mypy src`: passed for 85 source files;
- `uv run --frozen python -m compileall src`: passed;
- `git diff --check`: passed.

The nine skips are existing live opt-in, POSIX-only, Windows symlink privilege, and POSIX
executable-bit cases. B2.1 introduced no new skip.

## Isolation and integrity

- Registry admission is fixed to the four approved task IDs.
- Manifest fields, fixed pytest argv, path containment, editable coverage, protected coverage,
  overlay targets, symlink absence, and immutable SHA-256 coverage are validated before execution.
- Visible and hidden tests are evaluator-owned siblings of the copied workspace and never appear
  below the editable workspace.
- Reference files are copied only into declared editable targets in a temporary workspace.
- The test process uses an absolute interpreter and does not search `PATH`.
- The process environment is explicit and does not inherit API keys, tokens, SSH variables, or
  other host secrets.
- `TEMP` and `TMP` point inside the per-attempt execution root.
- UTF-8 mode, output encoding, hash seed, and pytest plugin autoload behavior are fixed.
- Static checks reject fixture imports or calls for network, shell, subprocess, and `time.sleep`.

## Task-specific closure

The QuixBugs hidden suite uses a Bellman-Ford-style oracle independent of the buggy heap-based
implementation. It covers fixed edge cases and twelve deterministic generated graphs.

The Black crop uses an explicit CP1252 legacy-default boundary, making the original default
encoding failure deterministic across hosts. Hidden tests cover ASCII, CJK, emoji, Greek,
multiline output, UTF-8 bytes, round trips, and reopening.

The pytest crop verifies setup/call/teardown bindings, repeated clear, post-clear logging, phase
isolation, and list identity. The reference overlay clears the bound list in place.

The parcel crop covers restart after durable dispatch, repeated recovery, completed-result reuse,
stale ownership after dispatch, active ownership before dispatch, and barrier-controlled concurrent
claims. Recovery cannot replace an active owner that has not produced a dispatch. A unique SQLite
dispatch row models the effect boundary and permits result reuse after a crash.

## Limitations

- The fixture verifier is evaluation infrastructure, not an OS sandbox or AgentForge Runtime tool.
- It accepts only repository-owned fixtures admitted through human review. Static AST checks are a
  defense against accidental policy violations, not containment for hostile Python code.
- It uses a bounded root-process timeout; fixtures are statically prohibited from spawning child
  processes, so this stage does not claim arbitrary process-tree containment.
- The Black crop models a legacy platform default with a controlled CP1252 boundary rather than
  changing the host locale.
- The parcel dispatch ledger is a deterministic proxy for an externally visible effect. It does
  not prove exactly-once delivery by an external notification or message system.
- Full upstream repositories and dependency suites are not part of the formal cropped fixtures;
  source relation and original reproduction evidence remain in M7-B1/B2.0 artifacts.
- The remaining six accepted candidates, including four conditional candidates, were not built.
- No real or mock model evaluation was performed.
- AgentForge Runtime, dependencies, `pyproject.toml`, and `uv.lock` were not changed.

## Next gate

The next possible stage is M7-B2.2 model evaluation design. It requires a separate human decision
covering prompt exposure, visible-test feedback, hidden-test secrecy, run counts, scoring, budgets,
and result reporting. This B2.1 PASS does not authorize that stage.
