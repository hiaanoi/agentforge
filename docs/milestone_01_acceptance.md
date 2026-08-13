# Milestone 0/1 Acceptance Audit

Audit baseline: commit `0a3e1c86376044a65abd67f29a19b99ec657b34e` on Python 3.14.3. The audit reran environment sync,
tests, lint, strict type checking, and bytecode compilation before Milestone 2 work.

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Create and persist a Run | PASS | `domain/models.py`, `persistence/repositories.py`; `test_persistence.py::test_run_survives_database_reopen` |
| 2 | Reject invalid state transitions | PASS | `domain/enums.py`, `domain/models.py`; `test_domain.py::test_illegal_run_transition_is_rejected` |
| 3 | Event sequence strictly increases within a Run | PASS | `persistence/repositories.py`; `test_persistence.py::test_event_sequence_is_monotonic_and_isolated_per_run` |
| 4 | Event sequences are isolated between Runs | PASS | `persistence/repositories.py`; `test_persistence.py::test_event_sequence_is_monotonic_and_isolated_per_run` |
| 5 | Mock provider returns configured tool_call/final sequence | PASS | `models/mock.py`; `test_model_and_tools.py::test_mock_provider_returns_responses_in_order` |
| 6 | Pydantic validates model output | PASS | `models/base.py`; `test_model_and_tools.py::test_structured_model_output_accepts_only_supported_actions` |
| 7 | Runtime executes tool_call to tool result to final | PASS | `runtime/engine.py`; `test_runtime.py::test_runtime_completes_tool_loop_and_persists_audit_trail` |
| 8 | Persist final output on Run | PASS | `runtime/engine.py`, `persistence/repositories.py`; `test_runtime.py::test_runtime_completes_tool_loop_and_persists_audit_trail` |
| 9 | Save a checkpoint after every successful tool step | PASS | `runtime/engine.py`; `test_runtime.py::test_runtime_saves_checkpoint_after_each_successful_tool_step` |
| 10 | Fail clearly after max_steps | PASS | `runtime/engine.py`; `test_runtime.py::test_runtime_fails_when_max_steps_is_exhausted` |
| 11 | Standardize missing-tool failure and Runtime behavior | PASS | `tools/executor.py`, `runtime/engine.py`; `test_runtime.py::test_runtime_records_unknown_tool_as_failure` |
| 12 | Standardize invalid model output and persist failed Run | PASS | `models/base.py`, `runtime/engine.py`; `test_runtime.py::test_runtime_rejects_invalid_model_output` |
| 13 | Read Run, Event, and Checkpoint after database reopen | PASS | `persistence/repositories.py`; `test_persistence.py::test_run_event_and_checkpoint_survive_the_same_database_reopen` |
| 14 | Keep consecutive Runs and events isolated | PASS | `runtime/engine.py`, `persistence/repositories.py`; `test_runtime.py::test_two_runtime_runs_keep_state_and_events_isolated` |
| 15 | Tests do not call the network | PASS | `tests/conftest.py` blocks connection and DNS APIs; all tests use mock providers and temporary paths |
| 16 | Tests require no real API key | PASS | `models/mock.py`, `.env.example`; `test_model_and_tools.py`, `test_runtime.py` |
| 17 | Milestone 0/1 tests execute no shell | PASS | `tools/safe.py`; all baseline tests call only in-process tools |
| 18 | All persisted timestamps are UTC | PASS | `domain/models.py`; `test_persistence.py::test_run_event_and_checkpoint_survive_the_same_database_reopen` |
| 19 | pyproject and lock file agree | PASS | `pyproject.toml`, `uv.lock`; `test_project_contract.py::test_lock_uses_official_pypi_and_covers_project_dependencies`; `uv sync --frozen` passed |
| 20 | README claims only implemented behavior | PASS | `README.md`; `test_project_contract.py::test_readme_distinguishes_implemented_and_deferred_capabilities` |

## Verification

Before Milestone 2 changes:

- `uv sync --frozen`: passed, 21 packages checked.
- `uv run pytest -q`: 14 tests passed.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed for 19 source files.
- `uv run python -m compileall -q src`: passed.

The added acceptance tests strengthen checkpoint, restart, UTC, and cross-Run isolation
evidence; they do not change Milestone 0/1 behavior or weaken existing assertions.

The Milestone 2 pre-change regression rerun produced `4 passed` for `test_domain.py`, `4 passed`
for `test_persistence.py`, `3 passed` for `test_model_and_tools.py`, and `7 passed` for
`test_runtime.py`. No M0/1 interface regression was found.
