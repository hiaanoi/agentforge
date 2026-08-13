# Multi-Tool Provider Deviation Implementation Plan

**Execution status:** Implemented and verified. Checklist items below preserve the TDD execution
record; no Git commit was created.

> **For agentic workers:** Execute inline with strict red-green-refactor checkpoints. Do not commit,
> push, or enter Milestone 5.

**Goal:** Replace implicit first-call selection with explicit STRICT and SEQUENTIAL_READ_ONLY
policies that are auditable, retryable, recovery-safe, and verified offline and live.

**Architecture:** The OpenAI adapter classifies names against request ToolSpecs and prevents
discarded arguments from crossing the provider boundary. ModelExecutor owns STRICT retries and
deviation-attempt events. Runtime owns successful normalization events, protected context feedback,
tool execution, and checkpoint persistence.

**Tech Stack:** Python 3.14, Pydantic, SQLAlchemy/SQLite, OpenAI Responses SDK, pytest,
pytest-asyncio, Ruff, strict mypy.

---

### Task 1: Domain policy and sanitized deviation facts

**Files:**
- Modify: `src/agentforge/domain/enums.py`
- Modify: `src/agentforge/models/domain.py`
- Modify: `src/agentforge/models/errors.py`
- Test: `tests/unit/test_model_domain.py`

- [ ] Add failing enum/config/info validation and secret-free serialization tests.
- [ ] Run the focused test and verify RED.
- [ ] Add MultiToolResponsePolicy, MultiToolResponseInfo, ModelResponse field, config limit, and
      ProviderContractDeviationError.
- [ ] Run focused tests and verify GREEN.

### Task 2: Provider classification and argument minimization

**Files:**
- Modify: `src/agentforge/models/openai_provider.py`
- Test: `tests/unit/test_openai_provider.py`

- [ ] Add failing tests for STRICT two-READ rejection.
- [ ] Add failing SEQUENTIAL_READ_ONLY first-call-only test with sensitive discarded arguments.
- [ ] Add parameterized mixed WRITE, DANGEROUS, approval-required, non-local, and unknown tests.
- [ ] Add over-limit and call-plus-final-text tests.
- [ ] Implement count-first/name-only classification and parse only the selected arguments.
- [ ] Verify metadata names and counts, then run all provider tests.

### Task 3: STRICT persisted retry and deviation audit

**Files:**
- Modify: `src/agentforge/domain/enums.py`
- Modify: `src/agentforge/models/executor.py`
- Modify: `src/agentforge/persistence/model_workflow.py`
- Test: `tests/unit/test_model_executor.py`

- [ ] Add failing retry-then-success and retry-exhaustion tests.
- [ ] Assert MODEL_PROVIDER_DEVIATION, MODEL_ATTEMPT_FAILED, and MODEL_RETRY_SCHEDULED ordering.
- [ ] Implement sanitized deviation event persistence and retryable protocol wrapping.
- [ ] Verify ambiguous/malformed non-deviation errors remain non-retryable.

### Task 4: Runtime normalization and protected context feedback

**Files:**
- Modify: `src/agentforge/context/models.py`
- Modify: `src/agentforge/context/builder.py`
- Modify: `src/agentforge/runtime/engine.py`
- Test: `tests/integration/test_real_model_runtime.py`
- Test: `tests/unit/test_context_builder.py`

- [ ] Add failing tests for deviation/normalization events and one selected tool execution.
- [ ] Assert discarded tools consume no budget and emit no tool events.
- [ ] Add protected MULTI_TOOL_NORMALIZATION context tests.
- [ ] Implement sanitized Runtime events and post-result context injection.
- [ ] Include sanitized provider metadata in MODEL_RESPONDED.

### Task 5: Approval, checkpoint, and restart isolation

**Files:**
- Test: `tests/integration/test_approval_resume.py`
- Test: `tests/integration/test_real_model_runtime.py`

- [ ] Add mixed approval/unknown response tests proving complete STRICT rejection.
- [ ] Add checkpoint assertions proving discarded call IDs/arguments are absent.
- [ ] Add restart-through-later-approval test proving a discarded READ call is never replayed.
- [ ] Verify no ApprovalRequest is created by a discarded or STRICT response.

### Task 6: Offline gates

**Commands:**

- [ ] `uv run --frozen pytest -ra`
- [ ] `uv run --frozen ruff check .`
- [ ] `uv run --frozen mypy src`
- [ ] `uv run --frozen python -m compileall src`
- [ ] `git diff --check`

Expected: no failures; only the opt-in live test and unavailable Windows symlink tests may skip.

### Task 7: Three live validations

**Files:**
- Modify: `tests/live/test_openai_live.py`
- Modify: `docs/milestone_04_live_validation.md`

- [ ] Extend live assertions/diagnostics to collect sanitized deviation and budget metrics.
- [ ] Run the fixture test three separate times with `uv run --env-file .env`.
- [ ] Record each run's returned/selected/discarded counts, model/tool counts, final completion,
      fixture citations, budget status, and deviation event presence.
- [ ] Do not print API keys, arguments, complete model output, or file contents.

### Task 8: ADR and final documentation

**Files:**
- Add: `docs/decisions/0010-multi-tool-provider-deviation.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security_model.md`
- Modify: `docs/milestone_04_design.md`
- Modify: `docs/milestone_04_report.md`
- Modify: `docs/implementation_plan.md`

- [ ] Document Provider distrust, single-action Runtime, policy boundaries, retry behavior, context
      feedback, and future side-effect-tool STRICT default.
- [ ] Record exact offline and three-run live evidence.
- [ ] Confirm no documentation claims batch execution or unimplemented write capability.
