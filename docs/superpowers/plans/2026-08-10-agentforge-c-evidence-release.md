# AgentForge C Evidence Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish reproducible engineering and real-model evidence, two stable demos, dual-platform CI, and recruiting material whose claims trace to frozen artifacts.

**Architecture:** Extend A1 failpoints to eight deterministic crash windows, freeze baseline and candidate arms with four tasks x three repetitions each, collect 24 planned scoring slots only after explicit live-run authorization, regenerate reports from persisted facts offline, and derive public claims from keyed validated facts.

**Tech Stack:** Python 3.11+, pytest, existing evaluator, JSON/Markdown manifests, PowerShell/Bash demo scripts, GitHub Actions.

---

## File Map

- Extend `src/agentforge/testing/failpoints.py` and recovery integration tests.
- Create `src/agentforge/evaluation/release_manifest.py` and `release_reports.py`.
- Create `evaluation/release/portfolio-study.json` and generated safe reports.
- Create `scripts/run_release_study.py` and `scripts/regenerate_evidence.py`; validate the recovery scripts delivered by A2.
- Modify `.github/workflows/ci.yml` and add release-artifact workflow.
- Modify `README.md`; create `README.zh-CN.md`, `docs/architecture.md`, `docs/recovery.md`,
  `docs/failure-cases.md`, `docs/portfolio-star.md`, `docs/interview-questions.md`.

### Task 1: Complete eight-window fault-injection matrix

**Files:**
- Modify: `src/agentforge/testing/failpoints.py`
- Create: `tests/integration/test_release_failpoints.py`

- [ ] **Step 1: Encode all expected classifications**

```python
CASES = {
    "approval_before_commit": "RETRY_SAFE", "approval_after_commit": "REPLAY_FACT",
    "mutation_prepared": "RETRY_SAFE", "mutation_writing": "RECONCILE_OR_UNKNOWN",
    "test_started": "UNKNOWN_UNLESS_TERMINATED", "provider_prepared": "RETRY_SAFE",
    "provider_dispatching": "UNKNOWN_NO_RESEND", "lease_takeover": "STALE_WRITER_REJECTED",
}
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_release_failpoints.py -q`

Expected: FAIL until all named injection points exist.

- [ ] **Step 3: Add missing named boundaries and persisted-fact assertions**

Each case launches a fresh process, crashes once at the exact named point, recreates the
application, and asserts classification, selected fence, Receipt status, Event cursor continuity,
Provider request count where knowable, and at-most-once confirmed mutation.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/integration/test_core_failpoints.py tests/integration/test_release_failpoints.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/testing/failpoints.py tests/integration/test_release_failpoints.py
git commit -m "test: cover eight durable recovery windows"
```

### Task 2: Frozen release manifest and descriptive 4x3 study

**Files:**
- Create: `src/agentforge/evaluation/release_manifest.py`
- Create: `evaluation/release/portfolio-study.json`
- Create: `tests/unit/test_release_manifest.py`

- [ ] **Step 1: Write freeze and interleaving tests**

```python
def test_release_manifest_fixes_every_comparison_binding() -> None:
    manifest = load_release_manifest(Path("evaluation/release/portfolio-study.json"))
    assert len(manifest.tasks) == 4 and all(task.repetitions == 3 for task in manifest.tasks)
    assert tuple(arm.name for arm in manifest.arms) == ("baseline", "candidate")
    assert manifest.planned_scoring_slots == 24
    assert manifest.schedule == "BASELINE_CANDIDATE_INTERLEAVED"
    assert all(task.source_digest and task.hidden_verifier_digest for task in manifest.tasks)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_release_manifest.py -q`

Expected: FAIL because release manifest is absent.

- [ ] **Step 3: Implement frozen manifest model and checked-in instance**

```python
class ReleaseArmBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["baseline", "candidate"]
    git_commit_sha: str
    runtime_source_digest: str
    dependency_lock_digest: str
    python_executable_digest: str
    platform_identity_digest: str
    fixture_registry_digest: str
    model_snapshot: str
    provider_digest: str
    prompt_digest: str
    tool_schema_digest: str
    context_policy_digest: str
    profile_digest: str
    budget_digest: str
    stopping_digest: str
    pricing_binding_digest: str


class ReleaseStudyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    tasks: tuple[ReleaseTask, ReleaseTask, ReleaseTask, ReleaseTask]
    arms: tuple[ReleaseArmBinding, ReleaseArmBinding]
    planned_scoring_slots: Literal[24] = 24
    schedule: Literal["BASELINE_CANDIDATE_INTERLEAVED"]
```

Populate only from verified repository/evaluation facts. Live execution remains a separately
authorized action and is never part of ordinary CI.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_release_manifest.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/release_manifest.py evaluation/release/portfolio-study.json tests/unit/test_release_manifest.py
git commit -m "feat: freeze portfolio study manifest"
```

### Task 3: Explicitly authorized live execution and fact freeze

**Files:**
- Create: `scripts/run_release_study.py`
- Create: `tests/unit/test_release_live_authorization.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write authorization and 24-slot schedule tests**

```python
def test_live_collection_requires_explicit_gate() -> None:
    with pytest.raises(RealModelAuthorizationError):
        build_release_runner(manifest=release_manifest(), authorization=None)


@pytest.mark.parametrize("argv", [[], ["--run-live"], ["--authorization", "gate.json"]])
def test_missing_either_live_gate_makes_zero_provider_calls(argv: list[str]) -> None:
    result = invoke_release_runner(argv)
    assert result.provider_call_count == 0


def test_schedule_interleaves_arms_without_changing_planned_denominator() -> None:
    schedule = build_release_schedule(release_manifest())
    assert len(schedule) == 24
    assert [slot.arm for slot in schedule[:4]] == ["baseline", "candidate", "baseline", "candidate"]
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_release_live_authorization.py -q`

Expected: FAIL because the release runner does not exist.

- [ ] **Step 3: Implement gated collection and immutable export**

`run_release_study.py` validates both `--run-live` and the existing `RealModelExecutionGate`, exact
manifest digest, clean commit and budget before any Provider call. For each arm it creates an
isolated worktree at the fixed commit, builds the fixed wheel, installs it with the fixed lock into
a separate venv, and invokes a versioned JSON export protocol in a subprocess. Baseline and
candidate never share imported Python modules or a mutable workspace. It writes append-only raw facts to
`evaluation/release/facts/<study-id>/`, including one row per selected/replacement attempt,
arm/task/repetition identity, response model identity, source/profile/protocol digests and a fact
manifest SHA-256. `.gitignore` excludes credentials, SQLite working state and unselected raw
Provider bodies, but not the sanitized frozen fact bundle selected for publication.

- [ ] **Step 4: Run the offline authorization tests**

Run: `uv run --frozen pytest tests/unit/test_release_live_authorization.py -q`

Expected: PASS without a Provider call.

When and only when the user separately authorizes cost-bearing execution, run:

`uv run --frozen python scripts/run_release_study.py --run-live --manifest evaluation/release/portfolio-study.json --authorization evaluation/release/live-authorization.json`

Expected: 24 valid selected slots or an explicitly INCOMPLETE fact bundle; replacement attempts do
not change the 24 planned scoring slots.

- [ ] **Step 5: Commit code; commit sanitized facts separately after review**

```powershell
git add scripts/run_release_study.py tests/unit/test_release_live_authorization.py .gitignore
git commit -m "feat: gate portfolio study fact collection"
```

### Task 4: Offline evidence regeneration and cross-validation

**Files:**
- Create: `src/agentforge/evaluation/release_reports.py`
- Create: `scripts/regenerate_evidence.py`
- Create: `tests/integration/test_release_reports.py`

- [ ] **Step 1: Write offline and denominator tests**

```python
def test_regeneration_uses_frozen_facts_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OpenAIModelProvider, "generate", pytest.fail)
    bundle = regenerate_release_bundle(FROZEN_FACTS)
    assert bundle.comparison.planned_slots == 24
    assert bundle.baseline.planned_slots == 12
    assert bundle.candidate.planned_slots == 12
    assert bundle.comparison.valid_slots + bundle.comparison.invalid_slots + bundle.comparison.unexecuted_slots == 24
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_release_reports.py -q`

Expected: FAIL because release regeneration is absent.

- [ ] **Step 3: Implement deterministic generation**

The command reads frozen SQLite/export facts, validates A0 schema-v2 metrics and release-manifest
digests, renders JSON/Markdown/manifest, scans each artifact, and verifies all headline numbers.
It can render an explicitly INCOMPLETE report with missing slots and ineligible pass@k. The release
gate requires 12 valid selected slots in each arm before calling the comparison complete. It never
calls a Provider.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/integration/test_release_reports.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/release_reports.py scripts/regenerate_evidence.py tests/integration/test_release_reports.py
git commit -m "feat: regenerate release evidence offline"
```

### Task 5: CI release gates and reproducible demos

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/demo_recovery.ps1`
- Modify: `scripts/demo_recovery.sh`
- Create: `tests/cli/test_demo_contracts.py`

- [ ] **Step 1: Write timing, command and no-live-CI tests**

```python
def test_ci_never_enables_live_marker() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--run-live" not in workflow and "OPENAI_API_KEY" not in workflow


def test_demo_scripts_have_bounded_timeout_and_public_commands() -> None:
    for script in demo_scripts():
        text = script.read_text(encoding="utf-8")
        assert "agentforge" in text and "timeout" in text.casefold()
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/cli/test_demo_contracts.py -q`

Expected: FAIL until recovery demos exist.

- [ ] **Step 3: Add gates and demos**

Windows/Linux CI runs full offline suite, Ruff, mypy, compileall, wheel/fresh-install, CLI contract,
artifact scan, A0 formula tests, Core and eight failpoints, and offline report regeneration. Demo
scripts use only public CLI, deterministic Mock input, bounded timeouts, and assert VERIFIED plus
single mutation. Recovery demo kills at approval, approves from a new process, and resumes the same
Run in under ninety seconds.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/cli/test_demo_contracts.py tests/integration/test_release_failpoints.py tests/integration/test_release_reports.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add .github/workflows/ci.yml scripts/demo_recovery.ps1 scripts/demo_recovery.sh tests/cli/test_demo_contracts.py
git commit -m "ci: enforce product evidence release gates"
```

### Task 6: Recruiting documentation and final claim audit

**Files:**
- Modify: `README.md`
- Create: `README.zh-CN.md`
- Create: `docs/architecture.md`
- Create: `docs/recovery.md`
- Create: `docs/failure-cases.md`
- Create: `docs/portfolio-star.md`
- Create: `docs/interview-questions.md`
- Create: `tests/unit/test_public_claims.py`

- [ ] **Step 1: Write traceability and prohibited-claim tests**

```python
@pytest.mark.parametrize("claim", ["AgentForge is production-ready", "AgentForge provides an OS sandbox", "Provider requests are exactly-once"])
def test_public_docs_do_not_make_positive_overclaims(claim: str) -> None:
    assert claim.casefold() not in all_public_docs().casefold()


def test_public_docs_include_required_limitations() -> None:
    docs = all_public_docs().casefold()
    assert "not an official swe-bench score" in docs
    assert "provider requests are not exactly-once" in docs


def test_every_evidence_claim_key_matches_release_bundle() -> None:
    assert extract_keyed_claims(all_public_docs()) == release_bundle_claims()
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_public_claims.py -q`

Expected: FAIL until docs and claim helpers exist.

- [ ] **Step 3: Write evidence-backed materials**

Lead with tool calling, structured feedback, controlled mutation and verification; then explain
durability. Include Pico/harness comparison, install/quickstart, architecture/recovery diagrams,
three-minute and recovery demos, real failures, limitations, STAR story and interview follow-ups.
All evidence statements use structured markers such as `evidence:baseline.pass_at_1`; versions,
dates and demo-duration prose are not parsed as benchmark claims. All metrics are generated or
traceable to the release bundle.

- [ ] **Step 4: Run the final release gate**

Run: `uv run --frozen pytest -q && uv run --frozen ruff check src tests && uv run --frozen mypy src && uv build`

Expected: PASS.

Run: `uv run --frozen python scripts/regenerate_evidence.py --check`

Expected: exits 0 with `evidence bundle verified`; public artifact scan reports zero findings.

- [ ] **Step 5: Commit**

```powershell
git add README.md README.zh-CN.md docs/architecture.md docs/recovery.md docs/failure-cases.md docs/portfolio-star.md docs/interview-questions.md tests/unit/test_public_claims.py
git commit -m "docs: publish AgentForge recruiting evidence"
```
