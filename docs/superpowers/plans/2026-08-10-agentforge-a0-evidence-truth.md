# AgentForge A0 Evidence Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct AgentForge's pass@k semantics, version the public report, and prevent legacy or incomplete artifacts from being presented as current evidence.

**Architecture:** Put the standard estimator in a small pure metrics module. Upgrade report DTOs and renderers to schema v2, retain raw legacy artifacts unchanged, and add a deterministic evidence-summary validator that rejects schema v1 publication.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, existing evaluation Study models and public artifact scanner.

---

## File Map

- Create `src/agentforge/evaluation/pass_at_k.py`: pure standard estimator and eligibility errors.
- Modify `src/agentforge/evaluation/study_reports.py`: schema-v2 task/aggregate DTOs, builders, JSON and Markdown.
- Modify `src/agentforge/evaluation/study_models.py`: aggregate metric vocabulary.
- Create `src/agentforge/evaluation/evidence_summary.py`: load, validate, regenerate and cross-check public evidence.
- Create `evaluation/results/LEGACY.md`: dated legacy policy; no raw result deletion.
- Modify `README.md`: current/legacy evidence wording only.
- Test `tests/unit/test_pass_at_k.py`.
- Modify `tests/unit/test_evaluation_study_reports.py`.
- Modify `tests/unit/test_evaluation_study_models.py`.
- Create `tests/unit/test_evidence_summary.py`.

### Task 1: Standard pass@k estimator

**Files:**
- Create: `src/agentforge/evaluation/pass_at_k.py`
- Create: `tests/unit/test_pass_at_k.py`

- [ ] **Step 1: Write the failing formula and eligibility tests**

```python
import pytest

from agentforge.evaluation.pass_at_k import PassAtKNotEligibleError, estimate_pass_at_k


@pytest.mark.parametrize(
    ("successful", "k", "expected"),
    [(0, 1, 0.0), (1, 1, 1 / 3), (2, 1, 2 / 3), (3, 1, 1.0),
     (0, 3, 0.0), (1, 3, 1.0), (2, 3, 1.0), (3, 3, 1.0)],
)
def test_standard_pass_at_k(successful: int, k: int, expected: float) -> None:
    assert estimate_pass_at_k(total=3, successful=successful, k=k) == pytest.approx(expected)


def test_pass_at_k_rejects_incomplete_or_invalid_denominators() -> None:
    with pytest.raises(PassAtKNotEligibleError):
        estimate_pass_at_k(total=2, successful=1, k=3)
    with pytest.raises(ValueError):
        estimate_pass_at_k(total=3, successful=4, k=1)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_pass_at_k.py -q`

Expected: FAIL because `agentforge.evaluation.pass_at_k` does not exist.

- [ ] **Step 3: Implement the pure estimator**

```python
from math import comb


class PassAtKNotEligibleError(ValueError):
    pass


def estimate_pass_at_k(*, total: int, successful: int, k: int) -> float:
    if total < 0 or successful < 0 or successful > total or k <= 0:
        raise ValueError("Invalid pass@k counts")
    if total < k:
        raise PassAtKNotEligibleError("pass@k requires total >= k")
    if total - successful < k:
        return 1.0
    return 1.0 - comb(total - successful, k) / comb(total, k)
```

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_pass_at_k.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/pass_at_k.py tests/unit/test_pass_at_k.py
git commit -m "fix: add standard pass at k estimator"
```

### Task 2: Report schema v2 and task metrics

**Files:**
- Modify: `src/agentforge/evaluation/study_reports.py`
- Modify: `tests/unit/test_evaluation_study_reports.py`

- [ ] **Step 1: Replace order-sensitive assertions with schema-v2 assertions**

```python
def test_task_metrics_use_standard_estimator() -> None:
    task = task_report(0, scored=3, successful=2, successful_indices=(1, 2))
    assert task.pass_at_1 == pytest.approx(2 / 3)
    assert task.pass_at_3 == pytest.approx(1.0)
    assert task.first_attempt_success is False
    assert task.any_success_in_3 is True


def test_incomplete_task_has_no_pass_at_3() -> None:
    task = task_report(0, scored=2, successful=1)
    assert task.pass_at_1 == pytest.approx(0.5)
    assert task.pass_at_3 is None


def test_task_with_no_scored_slots_has_no_pass_at_one() -> None:
    task = task_report(0, scored=0, successful=0)
    assert task.pass_at_1 is None
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_evaluation_study_reports.py -q`

Expected: FAIL because `pass_at_1` is boolean and `first_attempt_success` is absent.

- [ ] **Step 3: Upgrade task DTO and builder**

```python
class PublicTaskStudyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # retain existing identity, denominator, cost, telemetry and slot fields
    pass_at_1: float | None = Field(default=None, ge=0, le=1)
    pass_at_3: float | None = Field(default=None, ge=0, le=1)
    first_attempt_success: bool
    any_success_in_3: bool
    majority_success: bool
    stable_success: bool


pass_at_1 = (
    estimate_pass_at_k(total=scored_count, successful=successful, k=1)
    if scored_count else None
)
pass_at_3 = (
    estimate_pass_at_k(total=3, successful=successful, k=3)
    if scored_count == 3 else None
)
```

Update validation so `first_attempt_success` reconciles only with slot zero, `pass_at_1`
reconciles with `successful_slots / scored_slots`, and pass@3 is null unless all three slots are
valid and scored.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_evaluation_study_reports.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/study_reports.py tests/unit/test_evaluation_study_reports.py
git commit -m "fix: correct portfolio task pass at k metrics"
```

### Task 3: Aggregate vocabulary and rendering

**Files:**
- Modify: `src/agentforge/evaluation/study_models.py`
- Modify: `src/agentforge/evaluation/study_reports.py`
- Modify: `tests/unit/test_evaluation_study_models.py`
- Modify: `tests/unit/test_evaluation_study_reports.py`
- Modify: `tests/integration/test_offline_evaluation_matrix.py`
- Modify: `tests/integration/test_real_model_pilot_entrypoint.py`

- [ ] **Step 1: Write aggregate and rendering tests**

```python
def test_aggregate_reports_macro_pass_at_k_and_first_attempts() -> None:
    summary = aggregate_study_summary(tuple(task_report(i, scored=3, successful=i) for i in range(4)))
    assert summary.macro_pass_at_1 == pytest.approx((0 + 1 / 3 + 2 / 3 + 1) / 4)
    assert summary.macro_pass_at_3 == pytest.approx(3 / 4)
    assert summary.first_attempt_success_count == 3


def test_schema_v2_rendering_names_metrics_honestly() -> None:
    payload = json.loads(render_public_study_json(summary, tasks, scanner=PublicArtifactScanner()))
    assert payload["report_schema_version"] == 2
    assert "task_pass_at_1_count" not in payload["summary"]
    assert "first_attempt_success_count" in payload["summary"]
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_evaluation_study_models.py tests/unit/test_evaluation_study_reports.py tests/integration/test_offline_evaluation_matrix.py tests/integration/test_real_model_pilot_entrypoint.py -q`

Expected: FAIL on missing macro and renamed fields.

- [ ] **Step 3: Implement schema-v2 aggregate fields**

```python
class EvaluationStudySummary(BaseModel):
    macro_pass_at_1: float | None = Field(default=None, ge=0, le=1)
    pass_at_1_eligible_task_count: int = Field(ge=0, le=4)
    macro_pass_at_3: float | None = Field(default=None, ge=0, le=1)
    pass_at_3_eligible_task_count: int = Field(ge=0, le=4)
    first_attempt_success_count: int = Field(ge=0, le=4)
    any_success_in_3_count: int = Field(ge=0, le=4)
    stable_success_count: int = Field(ge=0, le=4)
```

Macro pass@1 averages only non-null task values and is null when no task is eligible. Macro pass@3
is null until all four tasks are eligible; eligible counts remain visible. Keep
`planned_slot_success_rate` as the conservative success/planned denominator. Set
`PublicEvaluationStudyReport.report_schema_version` and standalone JSON rendering to literal
`2`. Render numeric pass@k values, raw `c/3`, first attempt, any, majority, stable, and all slot
denominators. Never render pass@3 as `no` when it is ineligible; render `N/A`.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_evaluation_study_models.py tests/unit/test_evaluation_study_reports.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/study_models.py src/agentforge/evaluation/study_reports.py tests/unit/test_evaluation_study_models.py tests/unit/test_evaluation_study_reports.py tests/integration/test_offline_evaluation_matrix.py tests/integration/test_real_model_pilot_entrypoint.py
git commit -m "fix: publish schema v2 evidence metrics"
```

### Task 4: Legacy quarantine and evidence-summary validation

**Files:**
- Create: `src/agentforge/evaluation/evidence_summary.py`
- Create: `tests/unit/test_evidence_summary.py`
- Create: `evaluation/results/LEGACY.md`

- [ ] **Step 1: Write failing legacy and cross-check tests**

```python
def test_summary_rejects_schema_v1_as_current(tmp_path: Path) -> None:
    report = tmp_path / "study_report.json"
    report.write_text('{"report_schema_version":1}', encoding="utf-8")
    with pytest.raises(LegacyEvidenceError):
        load_current_report(report)


def test_summary_requires_json_markdown_and_manifest_to_agree(tmp_path: Path) -> None:
    paths = write_schema_v2_fixture(tmp_path, markdown_successes=3, json_successes=2)
    with pytest.raises(EvidenceMismatchError):
        validate_evidence_bundle(paths)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_evidence_summary.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement strict loading and bundle validation**

```python
class LegacyEvidenceError(RuntimeError):
    pass


class EvidenceMismatchError(RuntimeError):
    pass


def load_current_report(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("report_schema_version") != 2:
        raise LegacyEvidenceError("Only schema-v2 evidence may be published as current")
    return payload
```

`validate_evidence_bundle` must parse the schema-v2 Pydantic model and reconcile report digest,
planned/executed/valid/invalid/scored/replacement/unexecuted counts, task order and pass@k
eligibility. Markdown is regenerated from that validated model rather than parsed as a fact source.
Write `LEGACY.md` listing every existing result directory as historical, preserving raw files and
forbidding copy-forward of schema-v1 headline metrics.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_evidence_summary.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/evidence_summary.py tests/unit/test_evidence_summary.py evaluation/results/LEGACY.md
git commit -m "feat: quarantine legacy evidence reports"
```

### Task 5: Documentation correction and A0 gate

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-10-agentforge-recruiting-productization-design.md` only if implemented names differ from the approved contract.

- [ ] **Step 1: Add a documentation contract test**

```python
def test_readme_does_not_publish_legacy_pass_at_one() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "first repetition" not in readme.casefold()
    assert "schema v2" in readme.casefold()
    assert "non-official" in readme.casefold()
```

Place it in `tests/unit/test_project_contract.py`.

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_project_contract.py -q`

Expected: FAIL until README distinguishes current schema-v2 evidence from legacy results.

- [ ] **Step 3: Correct README evidence wording**

Document standard pass@k, first-attempt naming, incomplete eligibility, curated/non-official scope,
and link `evaluation/results/LEGACY.md`. Do not invent new run results.

- [ ] **Step 4: Run the A0 gate**

Run: `uv run --frozen pytest tests/unit/test_pass_at_k.py tests/unit/test_evaluation_study_models.py tests/unit/test_evaluation_study_reports.py tests/unit/test_evidence_summary.py tests/unit/test_project_contract.py -q`

Expected: PASS.

Run: `uv run --frozen pytest tests/integration/test_offline_evaluation_matrix.py tests/integration/test_real_model_pilot_entrypoint.py -q`

Expected: PASS without any live Provider call.

Run: `uv run --frozen ruff check src tests && uv run --frozen mypy src`

Expected: both PASS.

- [ ] **Step 5: Commit**

```powershell
git add README.md tests/unit/test_project_contract.py
git commit -m "docs: correct AgentForge evidence claims"
```
