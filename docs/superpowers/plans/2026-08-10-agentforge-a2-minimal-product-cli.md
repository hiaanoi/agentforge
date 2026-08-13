# AgentForge A2 Minimal Product CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a fresh-installable `agentforge` CLI that completes a durable code-repair Run through cross-process approval, resume, inspection, and verification.

**Architecture:** `AgentApplication` is the only product entrypoint; the CLI parses and renders only. Product and evaluator share a new internal Runtime assembly module, while existing `ModelProvider.generate`, TestProfile and Runtime chains remain canonical.

**Tech Stack:** Python 3.11+, argparse, tomllib, asyncio, Pydantic v2, SQLAlchemy/SQLite, Hatch wheel, pytest, GitHub Actions.

---

## File Map

- Create `src/agentforge/application/commands.py`, `queries.py`, `events.py`, `views.py`, `errors.py`.
- Create `src/agentforge/application/config.py`: layered safe configuration.
- Create `src/agentforge/application/runtime_factory.py`: shared Runtime assembly.
- Create `src/agentforge/application/app.py`: `stream` and `query` deep interface.
- Create `src/agentforge/application/projections.py`: canonical safe event/view mapping.
- Create `src/agentforge/application/doctor.py`: read-only diagnostics.
- Create `src/agentforge/cli/main.py`, `parser.py`, `render.py`, `exit_codes.py`.
- Create `src/agentforge/tools/repository/git_status.py` and `git_log.py`.
- Modify `src/agentforge/evaluation/pilot_factory.py`: consume shared assembly.
- Modify `pyproject.toml`: console script.
- Create `.github/workflows/ci.yml`.
- Add unit, CLI and integration tests by task.

### Task 1: Layered product configuration

**Files:**
- Create: `src/agentforge/application/config.py`
- Create: `tests/unit/test_product_config.py`

- [ ] **Step 1: Write precedence, secret and trust-source tests**

```python
def test_config_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_user_config(tmp_path, model="user")
    write_project_config(tmp_path, model="project")
    monkeypatch.setenv("AGENTFORGE_MODEL", "environment")
    loaded = ProductConfigLoader(user_root=tmp_path).load(tmp_path, cli={"model": "cli"})
    assert loaded.model == "cli"
    assert loaded.sources["model"] == ConfigSource.CLI


def test_project_config_rejects_secret_fields(tmp_path: Path) -> None:
    write_project_config(tmp_path, api_key="forbidden")
    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader().load(tmp_path)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_product_config.py -q`

Expected: FAIL because product config does not exist.

- [ ] **Step 3: Implement strict TOML loading**

```python
class ProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database_path: Path
    model: str
    max_steps: int = Field(gt=0, le=100)
    profile_ids: tuple[str, ...]
    sources: Mapping[str, ConfigSource]


PRECEDENCE = (
    ConfigSource.SAFE_DEFAULT,
    ConfigSource.ENVIRONMENT,
    ConfigSource.USER,
    ConfigSource.PROJECT,
    ConfigSource.CLI,
)
```

Merge in that low-to-high order. Read API key only from `OPENAI_API_KEY` at Provider construction;
exclude it from `ProductConfig.model_dump`, digests and source reporting. Resolve project paths under
workspace and record a digest of non-secret effective configuration.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_product_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/config.py tests/unit/test_product_config.py
git commit -m "feat: load safe layered product configuration"
```

### Task 2: Shared Runtime assembly and fixed Git tools

**Files:**
- Create: `src/agentforge/application/runtime_factory.py`
- Create: `src/agentforge/tools/repository/git_status.py`
- Create: `src/agentforge/tools/repository/git_log.py`
- Modify: `src/agentforge/evaluation/pilot_factory.py`
- Create: `tests/unit/test_git_repository_tools.py`
- Create: `tests/integration/test_shared_runtime_factory.py`

- [ ] **Step 1: Write bounded Git and shared-chain tests**

```python
def test_git_status_uses_fixed_argv_and_bounded_output(git_workspace: Path) -> None:
    result = GitStatusTool(WorkspacePathResolver(git_workspace)).execute(GitStatusArguments())
    assert result.success is True
    assert result.output["truncated"] is False


def test_product_and_pilot_use_same_runtime_components(factories: FactoryPair) -> None:
    product = factories.product.build()
    pilot = factories.pilot.build_components()
    assert type(product.runtime) is type(pilot.runtime)
    assert type(product.test_coordinator) is type(pilot.test_coordinator)
    assert product.common_binding_digest == pilot.common_binding_digest
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_git_repository_tools.py tests/integration/test_shared_runtime_factory.py -q`

Expected: FAIL because shared assembly and Git tools are absent.

- [ ] **Step 3: Implement fixed tools and assembly seam**

```python
class RuntimeComponents(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    runtime: AgentRuntime
    profiles: TestProfileRegistry
    test_coordinator: TestExecutionCoordinator
    tool_names: tuple[str, ...]
    common_binding_digest: str


class RuntimeComponentFactory:
    def build(self, request: RuntimeAssemblyRequest) -> RuntimeComponents:
        resolver = WorkspacePathResolver(request.workspace)
        profiles = self._trusted_profiles.build_registry(request)
        registry = self._build_tools(resolver, profiles, request.policy)
        return self._assemble_existing_chain(request, registry, profiles)
```

Git tools invoke only fixed `git status --short --untracked-files=normal` and
`git log -n <bounded> --pretty=format:%H%x09%s --no-decorate`; use `shell=False`, clear `GIT_*`,
disable pagers/external diff, cap bytes/entries, and expose no arbitrary argv. The common binding
digest covers all tool schemas, mutation/test coordinator types, policy, Profile bindings,
ModelProvider protocol and context policy. Refactor
`PilotRuntimeFactory` to call this component factory, leaving evaluator-only baseline/harness setup
in the pilot module.

- [ ] **Step 4: Run GREEN and evaluator regression**

Run: `uv run --frozen pytest tests/unit/test_git_repository_tools.py tests/integration/test_shared_runtime_factory.py tests/integration/test_pilot_runtime_factory.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/runtime_factory.py src/agentforge/tools/repository/git_status.py src/agentforge/tools/repository/git_log.py src/agentforge/evaluation/pilot_factory.py tests/unit/test_git_repository_tools.py tests/integration/test_shared_runtime_factory.py
git commit -m "refactor: share product runtime assembly"
```

### Task 3: Typed application contracts, safe views and projections

**Files:**
- Create: `src/agentforge/application/commands.py`
- Create: `src/agentforge/application/queries.py`
- Create: `src/agentforge/application/events.py`
- Create: `src/agentforge/application/views.py`
- Create: `src/agentforge/application/errors.py`
- Create: `src/agentforge/application/projections.py`
- Create: `tests/unit/test_application_contracts.py`
- Create: `tests/unit/test_product_projections.py`

- [ ] **Step 1: Write discriminated-contract and redaction tests**

```python
def test_commands_are_closed_discriminated_union() -> None:
    command = APPLICATION_COMMAND_ADAPTER.validate_python(
        {"type": "start_run", "command_id": str(uuid4()), "task": "fix", "workspace": "."}
    )
    assert isinstance(command, StartRun)


def test_public_event_and_export_view_are_safe(run_facts: RunFacts) -> None:
    event = ProductProjector().event(run_facts.tool_event)
    exported = ProductProjector().export_run(run_facts)
    PublicArtifactScanner().validate(event.model_dump_json())
    PublicArtifactScanner().validate(exported.model_dump_json())
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_application_contracts.py tests/unit/test_product_projections.py -q`

Expected: FAIL because application DTOs do not exist.

- [ ] **Step 3: Define exact public unions and error catalog**

```python
ApplicationCommand = Annotated[
    StartRun | ResumeRun | DecideApproval | TrustProfile,
    Field(discriminator="type"),
]
ApplicationQuery = Annotated[
    RunDetails | PendingApprovals | DoctorReport | ProfileTrustDetails | ExportRunDetails,
    Field(discriminator="type"),
]


class ApplicationError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: ApplicationErrorCode
    safe_message: str
    retryable: bool
    exit_code: int
    error_id: UUID
    run_id: UUID | None = None
```

Product events contain schema version, event ID, scope, cursor, optional Run sequence/time and
allowlisted payload. Views expose `lifecycle_status` and nullable `outcome_status`. Unknown
exceptions map to one generic catalog entry; never use `str(exc)`. Local views may contain message
content only after B; all A2 event payloads and export views pass `PublicArtifactScanner`.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_application_contracts.py tests/unit/test_product_projections.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/commands.py src/agentforge/application/queries.py src/agentforge/application/events.py src/agentforge/application/views.py src/agentforge/application/errors.py src/agentforge/application/projections.py tests/unit/test_application_contracts.py tests/unit/test_product_projections.py
git commit -m "feat: define safe product application contracts"
```

### Task 4: AgentApplication command/query/stream orchestration

**Files:**
- Create: `src/agentforge/application/app.py`
- Create: `src/agentforge/application/doctor.py`
- Modify: `src/agentforge/application/run_creation.py`
- Create: `tests/integration/test_agent_application.py`

- [ ] **Step 1: Write start, replay, approval, resume and query tests**

```python
@pytest.mark.asyncio
async def test_start_run_reconnect_and_cross_process_resume(app: AgentApplication) -> None:
    command = start_run_command()
    first = [event async for event in app.stream(command)]
    paused = first[-1]
    replay = [event async for event in app.stream(command, after_cursor=first[0].cursor)]
    assert replay[0].cursor > first[0].cursor
    app2 = recreate_application(app.database_path)
    await collect(app2.stream(DecideApproval.approve(paused.approval_id)))
    completed = await collect(app2.stream(ResumeRun.new(paused.run_id)))
    assert completed[-1].outcome_status is OutcomeStatus.VERIFIED


def test_doctor_is_read_only_and_safe(app: AgentApplication) -> None:
    before = app.business_row_counts()
    report = app.query(DoctorReport())
    assert app.business_row_counts() == before
    PublicArtifactScanner().validate(report.model_dump_json())


@pytest.mark.asyncio
async def test_product_start_run_never_uses_legacy_runtime_creation(
    app: AgentApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("AgentRuntime.create_run is evaluator-only")

    monkeypatch.setattr(AgentRuntime, "create_run", forbidden)
    events = await collect(app.stream(start_run_command()))
    assert events[0].event_type == "run_created"
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_agent_application.py -q`

Expected: FAIL because `AgentApplication` does not exist.

- [ ] **Step 3: Implement the deep interface**

```python
class AgentApplication:
    async def stream(
        self,
        command: ApplicationCommand,
        *,
        after_cursor: int | None = None,
    ) -> AsyncIterator[ProductEvent]:
        result = self._commands.accept_or_replay(command)
        if result.should_drive:
            self._driver.start(result)
        async for event in self._stream.persisted(result.scope, after_cursor):
            yield self._projector.event(event)

    @overload
    def query(self, query: RunDetails) -> LocalRunDetailsView: ...
    @overload
    def query(self, query: ExportRunDetails) -> ExportRunDetailsView: ...
```

Dispatch StartRun, DecideApproval, ResumeRun and TrustProfile through A1 workflows. `CancelRun` is
not part of the Core command union and is introduced only by B. Poll persisted
EventLog while a background driver owns the lease. Iterator close detaches only. Doctor checks
schema, workspace/source, OpenAI environment presence, Profile bindings and Git without model,
mutation, subprocess profile or database writes. Every AgentApplication StartRun dispatches
`RunCreationWorkflow`; `AgentRuntime.create_run` remains evaluator-only and a test monkeypatching it
to raise must not affect product execution. Before A2 is complete, delete that legacy method or
isolate it behind an evaluator-only adapter that cannot be reached from AgentApplication assembly.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/integration/test_agent_application.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/app.py src/agentforge/application/doctor.py src/agentforge/application/run_creation.py tests/integration/test_agent_application.py
git commit -m "feat: expose durable AgentApplication"
```

### Task 5: Minimal CLI adapter and stable exit codes

**Files:**
- Create: `src/agentforge/cli/__init__.py`
- Create: `src/agentforge/cli/parser.py`
- Create: `src/agentforge/cli/render.py`
- Create: `src/agentforge/cli/exit_codes.py`
- Create: `src/agentforge/cli/main.py`
- Create: `tests/cli/test_cli_contract.py`

- [ ] **Step 1: Write subprocess CLI contracts**

```python
@pytest.mark.parametrize("name", ["exec", "inspect", "doctor", "approvals", "approve", "reject", "resume"])
def test_core_subcommand_has_help(cli: CliRunner, name: str) -> None:
    result = cli.run(name, "--help")
    assert result.returncode == 0
    assert result.stderr == ""


def test_exec_pause_prints_only_stable_identifiers(cli: CliRunner) -> None:
    result = cli.run("exec", "--workspace", str(cli.fixture), "fix the defect")
    assert result.returncode == ExitCode.APPROVAL_REQUIRED
    assert "run_id=" in result.stdout and "approval_id=" in result.stdout
    PublicArtifactScanner().validate(result.stdout + result.stderr)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/cli/test_cli_contract.py -q`

Expected: FAIL because the CLI package does not exist.

- [ ] **Step 3: Implement parser, renderer and adapter**

```python
class ExitCode(IntEnum):
    OK = 0
    FAILED = 1
    USAGE = 2
    APPROVAL_REQUIRED = 20
    PAUSED = 21
    UNKNOWN = 22
    CONFIGURATION_ERROR = 30


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = build_application(args)
    try:
        return asyncio.run(dispatch(application, args))
    except ApplicationFailure as failure:
        render_error(failure.error, stream=sys.stderr)
        return failure.error.exit_code
```

`exec`, `approve`, `reject`, and `resume` call `stream`; `inspect`, `doctor`, and `approvals` call
`query`. Normal facts go to stdout, errors to stderr. Ctrl-C detaches and prints Run ID; it does not
offer cancel in Core Release.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/cli/test_cli_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/cli tests/cli/test_cli_contract.py
git commit -m "feat: add minimal AgentForge CLI"
```

### Task 6: Wheel, fresh environment, Windows/Linux CI and Core Demo

**Files:**
- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`
- Create: `tests/cli/test_fresh_install.py`
- Create: `examples/demo-repair/pyproject.toml`
- Create: `examples/demo-repair/src/demo_calc.py`
- Create: `examples/demo-repair/tests/test_demo_calc.py`
- Create: `scripts/demo_core.ps1`
- Create: `scripts/demo_core.sh`
- Create: `scripts/demo_recovery.ps1`
- Create: `scripts/demo_recovery.sh`
- Modify: `README.md`
- Create: `docs/core-demo.md`

- [ ] **Step 1: Write wheel metadata and demo contract tests**

```python
def test_console_script_is_declared() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["agentforge"] == "agentforge.cli.main:main"


def test_demo_scripts_use_only_public_cli() -> None:
    for path in (Path("scripts/demo_core.ps1"), Path("scripts/demo_core.sh"),
                 Path("scripts/demo_recovery.ps1"), Path("scripts/demo_recovery.sh")):
        text = path.read_text(encoding="utf-8")
        assert "agentforge exec" in text
        assert "python -m agentforge" not in text
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/cli/test_fresh_install.py -q`

Expected: FAIL because console script and demo assets are absent.

- [ ] **Step 3: Add packaging, CI and deterministic fixture**

```toml
[project.scripts]
agentforge = "agentforge.cli.main:main"
```

CI matrix is `windows-latest` and `ubuntu-latest`, Python 3.11 and 3.14. Each job runs locked unit/
integration/CLI tests, Ruff, mypy, builds a wheel, creates a clean venv, installs only that wheel,
and runs `agentforge --help`, the Mock-provider Core Demo and the recovery Demo. The recovery script
pauses for approval, invokes approve from a new process, resumes the same Run and verifies one
mutation within ninety seconds. Never run live-model tests in CI.

Update README with wheel installation, seven Core commands, the deterministic three-minute Demo,
the recovery Demo, and explicit limitations. `docs/core-demo.md` records exact commands and expected
exit codes without any unverified real-model metric.

- [ ] **Step 4: Run the A2/Core gate locally**

Run: `uv build && uv run --frozen pytest tests/cli tests/integration/test_agent_application.py -q`

Expected: PASS and `dist/agentforge_runtime-*.whl` exists.

Run in a temporary venv: `python -m pip install dist/agentforge_runtime-*.whl` then
`agentforge --help`, `scripts/demo_core.<platform>` and `scripts/demo_recovery.<platform>`.

Expected: command succeeds; demo ends `lifecycle=TERMINAL outcome=VERIFIED` in under three minutes.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml .github/workflows/ci.yml tests/cli/test_fresh_install.py examples/demo-repair scripts/demo_core.ps1 scripts/demo_core.sh scripts/demo_recovery.ps1 scripts/demo_recovery.sh README.md docs/core-demo.md
git commit -m "build: ship AgentForge core CLI"
```
