# Milestone 7-B2.1 Formal Fixture Design

## Status and scope

This design implements the four fixtures authorized by the M7-B2.0 human decision:

- `quixbugs-shortest-path-length`
- `bugsinpy-black-21`
- `swebench-pytest-10051`
- `self-durable-double-consumption`

M7-B2.1 creates offline evaluation assets and verifies their buggy and reference states. It does
not run a model, change AgentForge Runtime behavior, admit conditional candidates, or start M7-B2.2.

## Chosen approach

Each task is a self-contained fixture under `evaluation/fixtures/tasks/<task_id>/`. A fixture has
an editable `workspace/`, evaluator-owned `tests/visible/` and `tests/hidden/`, a private
`reference/fixed_files/` overlay, attribution, and a schema-validated `task_manifest.json`.

The verifier copies a fixture to a temporary directory for every attempt. It executes the buggy
workspace first, overlays only the declared reference files for the reference state, and executes
the same test suites again. This keeps repository assets immutable, prevents one attempt from
contaminating another, and makes reference changes auditable without implementing a patch parser.

Alternatives rejected:

1. Full upstream repository snapshots were rejected because dependency and size variance would
   weaken offline determinism and obscure the causal repair.
2. A single generated Python module per task was rejected because it would erase the multi-module
   reasoning required by the engineering fixtures.
3. Embedding hidden tests in the editable workspace was rejected because a repair agent could read
   or mutate the evaluator oracle.

## Fixture contract

`task_manifest.json` is versioned and records:

- task identity, source family, difficulty, license, and provenance URLs;
- editable and protected paths;
- visible and hidden test commands as fixed argument arrays;
- declared reference overlay files;
- expected buggy and reference outcomes;
- offline, network, external-service, and runtime limits;
- SHA-256 digests for every immutable evaluator and reference file.

The registry contains exactly the four authorized task IDs. Paths must be relative, normalized,
free of traversal, and contained by the fixture root. Workspace files may not overlap protected
tests, manifests, attribution, or reference files.

## Execution and isolation

`evaluation/fixtures/verify_fixtures.py` is evaluator infrastructure, not an AgentForge tool. It:

1. validates the registry, manifests, containment, file digests, and declared path sets;
2. creates a fresh temporary copy for each task, state, suite, and repetition;
3. overlays declared fixed files only for the reference state;
4. launches `sys.executable -m pytest` with a fixed cwd, bounded timeout, and a minimal explicit
   environment containing only required interpreter and platform values;
5. captures bounded output and emits a versioned JSON verification report.

The test process receives no API keys, tokens, secret variables, network configuration, or
AgentForge workspace path. Hidden tests are copied to an evaluator-owned sibling directory and are
never placed under the editable workspace. B2.1 verification does not claim an OS sandbox; the
fixtures themselves contain no network, shell, subprocess, or external-service behavior.
Only repository-owned fixtures admitted through human review may be executed. Static source checks
reduce accidental policy violations but are not a security boundary for hostile fixture code.

## Task designs

### QuixBugs shortest path length

The crop retains a small graph node type and the original relaxation defect. The visible suite
shows one competing-route failure. The hidden suite uses an independent Bellman-Ford-style oracle
over direct, indirect, equal-cost, unreachable, stale-frontier, and identity cases. The reference
overlay changes only the relaxation expression.

### BugsInPy Black diagnostic encoding

The crop retains diagnostic text creation and a controlled legacy default encoding boundary. The
buggy implementation opens diagnostics through that boundary without forcing UTF-8. The visible
suite uses CJK text; hidden cases include emoji, ASCII, multiline content, and repeated opens. The
reference overlay explicitly selects UTF-8 and does not suppress or replace characters.

### SWE-bench pytest phase capture

The crop models setup/call/teardown record bindings. Clearing the current capture incorrectly
replaces the list and leaves the phase registry pointing at stale data. Visible tests expose stale
records after clear; hidden tests cover every phase, repeated clear, post-clear logging, phase
isolation, and list identity. The reference overlay clears the existing list in place.

### Original durable parcel dispatch

The original fixture uses `models.py`, `store.py`, `dispatcher.py`, and `service.py` with SQLite as
the durable boundary. The buggy flow records a customer-visible dispatch separately from receipt
completion, so recovery can dispatch again. The visible suite demonstrates restart duplication.
Hidden tests cover completed-result reuse, repeated recovery, stale claims, and barrier-controlled
concurrent ownership without sleeps. The reference overlay uses conditional ownership, a unique
durable dispatch record, and result reuse. AgentForge names and implementation code are excluded.

The dispatch table is a deterministic proxy for the customer-visible effect. B2.1 proves one
durable ledger record across the modeled crash and concurrency windows; it does not claim
exactly-once delivery by an external notification service. A claimed receipt without a dispatch is
treated as actively owned and cannot be taken over automatically. Recovery takeover is allowed
only after the durable dispatch record exists.

## Acceptance criteria

For every task:

- the manifest and immutable-file digests validate;
- visible and hidden tests fail in the buggy state for the intended causal reason;
- the same tests pass after applying only the declared reference overlay;
- buggy and reference verification each repeat three times in fresh temporary copies;
- no test writes outside its temporary root or requires network, secrets, or external services;
- protected tests and reference files are absent from the editable workspace;
- attribution is complete for public-source crops;
- project-wide pytest, Ruff, mypy, compileall, and `git diff --check` remain green.

Passing B2.1 authorizes a later human decision about M7-B2.2. It does not itself authorize model
evaluation.
