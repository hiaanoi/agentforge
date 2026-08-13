# Deterministic Run Ownership Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the lease-release race from the production Run driver and make cross-application ownership tests wait on durable conditions instead of fixed sleeps.

**Architecture:** Add an explicit `RunOwnership.expect_release()` handshake immediately before the approval workflow atomically pauses a Run and releases its lease. The heartbeat treats a stale renewal as expected only after that handshake; every other ownership loss remains `UNKNOWN`. Test providers expose events at their blocking boundaries, and database assertions use condition polling instead of scheduling delays.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy/SQLite, pytest, pytest-asyncio, Ruff, mypy, GitHub Actions.

---

### Task 1: Explicit intentional-release handshake

**Files:**
- Modify: `src/agentforge/application/run_driver.py`
- Modify: `src/agentforge/runtime/engine.py`
- Test: `tests/unit/test_run_leases.py`

- [ ] **Step 1: Write a failing driver test**

Add a test whose operation calls `ownership.expect_release()`, releases the exact lease, then stays alive until the heartbeat task has stopped. Assert that `run_outcome()` returns `UNVERIFIED`, not `UNKNOWN`.

```python
@pytest.mark.asyncio
async def test_driver_waits_for_operation_after_expected_boundary_release(tmp_path: Path) -> None:
    database, run_id, _, leases = kernel(tmp_path)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="intentional-pause",
        ttl=timedelta(seconds=3),
        heartbeat_interval=timedelta(milliseconds=20),
    )

    async def pause_at_command_boundary(ownership: RunOwnership) -> str:
        leases.release(ownership.expect_release())
        heartbeat = driver.heartbeat_task
        assert heartbeat is not None
        await asyncio.shield(heartbeat)
        return "PAUSED"

    result = await asyncio.wait_for(driver.run_outcome(pause_at_command_boundary), timeout=2)
    assert result.outcome is OutcomeStatus.UNVERIFIED
    assert result.value == "PAUSED"
    database.close()
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_leases.py::test_driver_waits_for_operation_after_expected_boundary_release -q
```

Expected: FAIL because `RunOwnership` has no `expect_release` method.

- [ ] **Step 3: Implement the handshake**

Extend `RunOwnership` with an `_expect_release` callback and method. Add `_release_expected` state to `RunDriver`, reject further side-effect authority after the handshake, suppress `_lost_event` only when a heartbeat observes the expected release, and reset the state during cleanup.

```python
@dataclass(frozen=True, slots=True)
class RunOwnership:
    _snapshot: Callable[[], RunLeaseAuthority]
    _expect_release: Callable[[], RunLeaseAuthority] | None = None

    def expect_release(self) -> RunLeaseAuthority:
        if self._expect_release is None:
            raise RunLeaseLostError()
        return self._expect_release()
```

In `AgentRuntime._pause_for_approval`, atomically close side-effect access and capture the final authority with `ownership.expect_release()`, then pass that authority into `workflow.pause_for_approval(...)`.

- [ ] **Step 4: Verify the driver tests GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_leases.py tests/integration/test_run_fencing.py -q
```

Expected: all tests pass; unexpected lease loss still returns `UNKNOWN`.

- [ ] **Step 5: Commit the production handshake**

```powershell
git add src/agentforge/application/run_driver.py src/agentforge/runtime/engine.py tests/unit/test_run_leases.py
git commit -m "Make intentional lease release explicit"
```

### Task 2: Replace scheduling sleeps with observable conditions

**Files:**
- Modify: `tests/integration/test_agent_application.py`

- [ ] **Step 1: Add provider boundary events and a receipt waiter**

Give `SlowMockProvider` a `started` event and `BlockingSequenceProvider` a `blocked` event. Set each event immediately before awaiting its release gate. Add an async helper that polls command receipts until the required IDs exist or a monotonic timeout expires.

```python
async def wait_for_receipts(database: Database, command_ids: tuple[UUID, ...]) -> None:
    async def ready() -> bool:
        with database.session() as session:
            return all(
                session.get(ApplicationCommandReceiptRow, str(command_id)) is not None
                for command_id in command_ids
            )

    async with asyncio.timeout(10):
        while not await ready():
            await asyncio.sleep(0)
```

- [ ] **Step 2: Migrate the three concurrency tests**

Replace fixed `asyncio.sleep(0.05)` calls with provider events, receipt conditions, or consuming the observer stream's first durable event. Preserve assertions that observer providers remain unused and only one execution record exists.

- [ ] **Step 3: Run stress repetitions**

Run the three migrated tests 20 times in fresh pytest processes.

```powershell
1..20 | ForEach-Object {
  .venv\Scripts\python.exe -m pytest `
    tests/integration/test_agent_application.py::test_distinct_concurrent_resumes_have_terminal_receipts_and_one_driver `
    tests/integration/test_agent_application.py::test_same_resume_command_across_apps_attaches_without_corrupting_owner `
    tests/integration/test_agent_application.py::test_same_start_command_across_apps_has_one_owner_and_observer -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: 20 consecutive green runs.

- [ ] **Step 4: Commit deterministic application tests**

```powershell
git add tests/integration/test_agent_application.py
git commit -m "Make ownership concurrency tests condition-driven"
```

### Task 3: Validate the release gate and synchronize repositories

**Files:**
- Modify: `docs/superpowers/plans/2026-08-13-deterministic-run-ownership-tests.md` (checkbox state only)
- Verify: `scripts/run_ci_tests.py`
- Verify: `.github/workflows/ci.yml`

- [ ] **Step 1: Run focused quality checks**

```powershell
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe -m mypy --platform win32 src scripts
.venv\Scripts\python.exe -m pytest tests/unit/test_run_leases.py tests/integration/test_agent_application.py tests/integration/test_approval_resume.py tests/integration/test_run_fencing.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the exact local CI orchestrator**

```powershell
.venv\Scripts\python.exe scripts/run_ci_tests.py
```

Expected: every offline group exits zero.

- [ ] **Step 3: Synchronize the private development branch**

Push `release/public-v0.1.0-prep` to `agentforge-dev` after confirming the worktree is clean and the intended commits are present.

- [ ] **Step 4: Rebuild the one-commit public candidate**

Copy the verified release snapshot into the clean candidate, amend `Initial public release candidate`, and force-push with lease to `agentforge/main`. Confirm `git rev-list --count HEAD` remains `1`.

- [ ] **Step 5: Require the four-job GitHub Actions matrix**

Wait for Windows and Ubuntu on Python 3.11 and 3.14 to complete. Do not change repository visibility unless all four jobs pass and the user explicitly proceeds with the public step.

---

## Self-review

- Spec coverage: explicit intentional release, unexpected loss preservation, removal of fixed scheduling sleeps, stress verification, full release validation, and dual-repository synchronization are all assigned to tasks.
- Placeholder scan: no TBD/TODO or unspecified implementation step remains.
- Type consistency: `RunOwnership.expect_release()`, `OutcomeStatus`, `Database`, `ApplicationCommandReceiptRow`, and the existing driver/runtime types match the current codebase.
