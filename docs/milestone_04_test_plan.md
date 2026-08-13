# Milestone 4 TDD Test Plan

## Gate M4A

1. Model domain and error tests fail before adding ModelResponse, usage, attempts, budget, call ID,
   and provider configuration.
2. Schema adapter tests fail before strict object normalization and Responses tool conversion.
3. Fake OpenAI client tests fail before function-call, final-text, usage, and protocol mapping.
4. Retry tests fail before timeout/rate-limit/transport classification and injected backoff.
5. Budget tests fail before atomic physical-request reservation and usage persistence.
6. Event tests fail before attempt-level audit events and sanitized metadata.
7. Snapshot tests fail before unversioned/v1/v2 migration.
8. Runtime provider integration fails before ModelExecutor replaces direct provider parsing.

M4A gate:

```powershell
uv run --frozen pytest tests/unit/test_model_domain.py `
  tests/unit/test_openai_schema.py `
  tests/unit/test_openai_provider.py `
  tests/unit/test_model_executor.py `
  tests/unit/test_snapshot_v2.py -ra
uv run --frozen ruff check .
uv run --frozen mypy src
```

## Gate M4B

1. ContextItem and ContextBuilder retention tests.
2. Per-tool deterministic renderer tests.
3. Complete tool-call/result pair compaction tests.
4. Sensitive-argument non-reappearance tests.
5. Loop warning and terminal threshold tests.
6. LoopState restart tests.
7. Snapshot v2 restart output-consistency tests.
8. End-to-end multi-tool Mock Provider repository analysis.

M4B gate:

```powershell
uv run --frozen pytest tests/unit/test_context_builder.py `
  tests/unit/test_tool_result_renderer.py `
  tests/unit/test_loop_detection.py `
  tests/integration/test_real_model_runtime.py -ra
uv run --frozen ruff check .
uv run --frozen mypy src
```

## Full regression

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

Live tests are marked `live`, require `RUN_LIVE_TESTS=1`, `OPENAI_API_KEY`, and
`AGENTFORGE_OPENAI_MODEL`, and do not affect Offline M4 PASS when skipped.
