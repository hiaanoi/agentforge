# DeepSeek Provider and SWE-bench Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native, auditable DeepSeek provider to AgentForge and produce one standard prediction record for an independently scored official SWE-bench Lite canary.

**Architecture:** Keep the DeepSeek endpoint fixed and translate AgentForge requests to Chat Completions behind the existing `ModelProvider` contract. Wire the provider into the product runtime and frozen provider binding, then add a bounded Git-diff exporter whose output is consumed by the pinned official SWE-bench harness rather than scored inside AgentForge.

**Tech Stack:** Python 3.11+, Pydantic 2, OpenAI Python SDK used as the DeepSeek HTTP client, pytest/pytest-asyncio, Ruff, mypy, Git, Docker, official SWE-bench harness.

---

## File map

New files:

- `src/agentforge/models/deepseek_schema.py` — Chat Completions function-tool conversion.
- `src/agentforge/models/deepseek_provider.py` — fixed-endpoint DeepSeek provider.
- `src/agentforge/evaluation/swebench_prediction.py` — bounded Git patch capture and JSONL model.
- `evaluation/export_swebench_prediction.py` — narrow CLI wrapper for prediction export.
- `tests/unit/test_deepseek_schema.py` — schema conversion tests.
- `tests/unit/test_deepseek_provider.py` — request, response, usage, policy, and error tests.
- `tests/unit/test_swebench_prediction.py` — Git binding and JSONL tests.
- `tests/live/test_deepseek_live.py` — opt-in authenticated provider canary.
- `docs/deepseek-swebench-canary.md` — secure cloud runbook and claim boundary.

Modified files:

- `src/agentforge/application/bootstrap.py` — allow `provider.kind = "deepseek"` and select `DEEPSEEK_API_KEY`.
- `src/agentforge/application/doctor.py` — report the credential required by the selected provider.
- `src/agentforge/evaluation/protocol.py` — allow an exact-identity DeepSeek real-model binding.
- `src/agentforge/evaluation/provider_factory.py` — add `DeepSeekEvaluationProviderFactory`.
- `src/agentforge/evaluation/public_artifacts.py` — reject DeepSeek secret field names.
- `src/agentforge/tools/mutation/security.py` — detect `DEEPSEEK_API_KEY` in attempted writes.
- `tests/cli/test_cli_contract.py` — product runtime configuration coverage.
- `tests/integration/test_agent_application.py` — provider-aware doctor coverage.
- `tests/unit/test_evaluation_protocol.py` — DeepSeek identity rules.
- `tests/unit/test_evaluation_provider_factory.py` — factory gate and secret redaction.
- `tests/security/test_mutation_security.py` — DeepSeek secret-pattern regression.
- `README.md`, `README.zh-CN.md`, `docs/evaluation_guide.md`, and `docs/evaluation_guide.zh-CN.md` — supported-provider and canary limitations.

The existing four-fixture `RealModelPilotApplication` remains OpenAI-specific. The DeepSeek
SWE-bench canary uses the normal product runtime plus the new external prediction exporter;
this avoids silently changing the semantics of previously frozen B2.4 studies.

---

### Task 1: Add the DeepSeek Chat Completions tool schema

**Files:**
- Create: `tests/unit/test_deepseek_schema.py`
- Create: `src/agentforge/models/deepseek_schema.py`

- [ ] **Step 1: Write the failing schema tests**

```python
from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.deepseek_schema import convert_tool_spec


def test_deepseek_tool_uses_nested_chat_completions_shape() -> None:
    spec = ToolSpec(
        name="read_file",
        description="Read one file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk_level=ToolRisk.READ,
    )

    assert convert_tool_spec(spec) == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file.",
            "parameters": spec.input_schema,
        },
    }


def test_deepseek_tool_converter_does_not_mutate_the_source_schema() -> None:
    schema = {"type": "object", "properties": {}}
    spec = ToolSpec(
        name="list_files",
        description="List files.",
        input_schema=schema,
        risk_level=ToolRisk.READ,
    )

    converted = convert_tool_spec(spec)
    converted["function"]["parameters"]["additionalProperties"] = False

    assert "additionalProperties" not in spec.input_schema
```

- [ ] **Step 2: Run the tests and verify import failure**

Run:

```powershell
uv run --frozen pytest tests/unit/test_deepseek_schema.py -q
```

Expected: collection fails with `ModuleNotFoundError: agentforge.models.deepseek_schema`.

- [ ] **Step 3: Implement the converter**

```python
from copy import deepcopy

from pydantic import JsonValue

from agentforge.domain.models import ToolSpec


def convert_tool_spec(spec: ToolSpec) -> dict[str, JsonValue]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": deepcopy(spec.input_schema),
        },
    }
```

Do not add `strict`; DeepSeek strict tool mode uses a separate beta endpoint that is outside
the accepted design.

- [ ] **Step 4: Run the schema tests**

Run: `uv run --frozen pytest tests/unit/test_deepseek_schema.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/models/deepseek_schema.py tests/unit/test_deepseek_schema.py
git commit -m "feat: add DeepSeek tool schema conversion"
```

---

### Task 2: Implement the native DeepSeek provider with TDD

**Files:**
- Create: `tests/unit/test_deepseek_provider.py`
- Create: `src/agentforge/models/deepseek_provider.py`

- [ ] **Step 1: Add fake-client helpers and a failing request-translation test**

```python
from types import SimpleNamespace

import pytest

from agentforge.context.models import ContextItem, ContextItemKind
from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.base import ModelRequest, ToolCall
from agentforge.models.deepseek_provider import DeepSeekModelProvider
from agentforge.models.domain import ModelProviderConfig


class FakeCompletions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: object) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


def model_request() -> ModelRequest:
    return ModelRequest(
        task="repair the repository",
        instructions="Use only registered tools.",
        step_number=2,
        history=[
            ContextItem(
                kind=ContextItemKind.TOOL_CALL,
                call_id="call_1",
                payload={"tool": "read_file", "arguments": {"path": "README.md"}},
            ).model_dump(mode="json"),
            ContextItem(
                kind=ContextItemKind.TOOL_RESULT,
                call_id="call_1",
                payload={"content": "hello"},
            ).model_dump(mode="json"),
        ],
        tools=[
            ToolSpec(
                name="read_file",
                description="Read a file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                risk_level=ToolRisk.READ,
            )
        ],
    )


@pytest.mark.asyncio
async def test_provider_maps_messages_tools_and_fixed_options() -> None:
    response = SimpleNamespace(
        id="chat_1",
        model="deepseek-account-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_2",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments='{"path":"pyproject.toml"}',
                            ),
                        )
                    ],
                )
            )
        ],
        usage=None,
    )
    client = FakeClient(response)
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=client,
    )

    result = await provider.generate(model_request())

    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "call_2"
    sent = client.chat.completions.requests[0]
    assert sent["model"] == "deepseek-account-model"
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["stream"] is False
    assert [message["role"] for message in sent["messages"]] == [
        "system", "user", "assistant", "tool"
    ]
    assert sent["tools"][0]["function"]["name"] == "read_file"
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `uv run --frozen pytest tests/unit/test_deepseek_provider.py -q`

Expected: import failure because `DeepSeekModelProvider` does not exist.

- [ ] **Step 3: Implement client construction and deterministic message translation**

Create these public and private boundaries in `deepseek_provider.py`:

```python
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ChatCompletionsResource(Protocol):
    async def create(self, **kwargs: Any) -> object: ...


class ChatResource(Protocol):
    completions: ChatCompletionsResource


class DeepSeekClient(Protocol):
    chat: ChatResource


class DeepSeekModelProvider:
    def __init__(
        self,
        config: ModelProviderConfig,
        *,
        client: DeepSeekClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or cast(
            DeepSeekClient,
            AsyncOpenAI(
                api_key=config.api_key.get_secret_value(),
                base_url=DEEPSEEK_BASE_URL,
                timeout=config.timeout_seconds,
                max_retries=0,
            ),
        )

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def journal_identity(self) -> str:
        return f"{self.name}/{self._config.model}"
```

Build messages with canonical JSON (`ensure_ascii=False`, `sort_keys=True`, compact
separators). A `TOOL_CALL` becomes an assistant `tool_calls` message and a result becomes a
`tool` message with `tool_call_id`. Send the vendor extension through the SDK-supported
`extra_body={"thinking": {"type": "disabled"}}`, plus `stream=False`, `n=1`, converted tools,
and `max_tokens` only when configured. Do not pass `thinking` as a direct keyword because the
OpenAI SDK Chat Completions signature does not define it.

- [ ] **Step 4: Add failing final-answer, invalid-output, and usage tests**

Add tests asserting:

```python
assert result.action.answer == "finished"
assert result.usage.input_tokens == 20
assert result.usage.output_tokens == 7
assert result.usage.total_tokens == 27
assert result.usage.cached_input_tokens == 4
assert result.usage.reasoning_tokens == 0
```

Also assert `ModelProtocolError` for zero choices, empty content, or simultaneous content and
tool calls; `ModelOutputInvalidError` for invalid JSON or non-object arguments.

- [ ] **Step 5: Implement response and usage normalization**

Use the first and only requested choice. Read Chat Completions usage fields as:

```python
ModelUsage(
    input_tokens=_optional_int(getattr(usage, "prompt_tokens", None)),
    output_tokens=_optional_int(getattr(usage, "completion_tokens", None)),
    total_tokens=_optional_int(getattr(usage, "total_tokens", None)),
    cached_input_tokens=_first_optional_int(
        getattr(usage, "prompt_cache_hit_tokens", None),
        getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    ),
    reasoning_tokens=_optional_int(
        getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    ),
)
```

`_first_optional_int(*values)` returns the first actual `int`, including zero, and otherwise
returns `None`; this preserves a reported zero cache hit instead of treating it as false.

Return only bounded metadata: choice finish reason, returned/discarded call counts, selected
multi-tool policy, and contract-deviation boolean.

- [ ] **Step 6: Add and satisfy the multi-tool policy matrix**

Copy the behavioral matrix from `tests/unit/test_openai_provider.py` for DeepSeek-shaped calls:

- two known local read-only calls select the first under `SEQUENTIAL_READ_ONLY`;
- strict mode rejects two calls;
- unknown, write, dangerous, MCP, and approval-required calls reject the whole response;
- configured call-count overflow rejects the whole response;
- discarded call IDs and arguments never enter `ModelResponse` serialization.

Use `ProviderContractDeviationError` and `MultiToolResponseInfo` with
`provider="deepseek"`, the returned model ID, and the provider request ID.

- [ ] **Step 7: Add and satisfy the SDK error mapping matrix**

Parametrize the same OpenAI SDK exception classes already used by the OpenAI provider and assert
the accepted mapping:

```python
(
    (AuthenticationError, ModelErrorCode.MODEL_AUTH_ERROR, False),
    (PermissionDeniedError, ModelErrorCode.MODEL_AUTH_ERROR, False),
    (RateLimitError, ModelErrorCode.MODEL_RATE_LIMITED, True),
    (APITimeoutError, ModelErrorCode.MODEL_TIMEOUT, True),
    (APIConnectionError, ModelErrorCode.MODEL_TRANSPORT_ERROR, True),
    (BadRequestError, ModelErrorCode.MODEL_BAD_REQUEST, False),
)
```

For generic `APIStatusError`, retry only status codes `>= 500`. Error messages identify
DeepSeek but contain neither response bodies nor secrets.

- [ ] **Step 8: Run provider tests and static checks**

Run:

```powershell
uv run --frozen pytest tests/unit/test_deepseek_provider.py tests/unit/test_deepseek_schema.py -q
uv run --frozen ruff check src/agentforge/models/deepseek_provider.py src/agentforge/models/deepseek_schema.py tests/unit/test_deepseek_provider.py tests/unit/test_deepseek_schema.py
uv run --frozen mypy src/agentforge/models/deepseek_provider.py src/agentforge/models/deepseek_schema.py
```

Expected: all commands exit `0`.

- [ ] **Step 9: Commit**

```powershell
git add src/agentforge/models/deepseek_provider.py tests/unit/test_deepseek_provider.py
git commit -m "feat: add native DeepSeek model provider"
```

---

### Task 3: Wire DeepSeek into the product runtime and doctor

**Files:**
- Modify: `src/agentforge/application/bootstrap.py`
- Modify: `src/agentforge/application/doctor.py`
- Modify: `tests/cli/test_cli_contract.py`
- Modify: `tests/integration/test_agent_application.py`

- [ ] **Step 1: Write failing product-provider tests**

Add a runtime TOML case with:

```toml
[provider]
kind = "deepseek"
```

Assert `ProductRuntimeDefinitionLoader` accepts it, product construction fails safely when
`DEEPSEEK_API_KEY` is absent, and a monkeypatched `DeepSeekModelProvider` receives a
`ModelProviderConfig` whose model equals the frozen product model.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
uv run --frozen pytest tests/cli/test_cli_contract.py tests/integration/test_agent_application.py -q
```

Expected: the DeepSeek runtime case fails validation because the provider literal is closed.

- [ ] **Step 3: Add the closed provider branch**

Change the provider definition to:

```python
kind: Literal["openai", "deepseek", "mock"]
```

Change `_build_provider` to select an exact environment name and constructor:

```python
if definition.kind == "openai":
    environment_name = "OPENAI_API_KEY"
    provider_type = OpenAIModelProvider
else:
    environment_name = "DEEPSEEK_API_KEY"
    provider_type = DeepSeekModelProvider
value = os.environ.get(environment_name)
if not value:
    raise UnsafeConfigurationError()
return provider_type(ModelProviderConfig(api_key=SecretStr(value), model=config.model))
```

The `mock` early return remains unchanged.

- [ ] **Step 4: Make doctor provider-aware**

Pass the provider kind into `Doctor`, replace `_openai_environment` with a provider credential
check, and emit one of the stable check names `openai_environment`, `deepseek_environment`, or
`mock_environment`. The message may state whether credentials are configured but must never show
the value.

- [ ] **Step 5: Run product tests and commit**

```powershell
uv run --frozen pytest tests/cli/test_cli_contract.py tests/integration/test_agent_application.py -q
git add src/agentforge/application/bootstrap.py src/agentforge/application/doctor.py tests/cli/test_cli_contract.py tests/integration/test_agent_application.py
git commit -m "feat: configure DeepSeek product runtimes"
```

---

### Task 4: Add frozen DeepSeek evaluation bindings without changing old studies

**Files:**
- Modify: `src/agentforge/evaluation/protocol.py`
- Modify: `src/agentforge/evaluation/provider_factory.py`
- Modify: `tests/unit/test_evaluation_protocol.py`
- Modify: `tests/unit/test_evaluation_provider_factory.py`

- [ ] **Step 1: Write failing exact-identity protocol tests**

Add tests proving:

```python
binding = provider_binding(
    provider="deepseek",
    model_id="account-model",
    response_model_id="account-model",
)
assert binding.response_model_id == "account-model"

with pytest.raises(ValidationError, match="response model"):
    provider_binding(
        provider="deepseek",
        model_id="account-model",
        response_model_id="different-model",
    )
```

Also assert an authorized `REAL_MODEL` protocol accepts either `openai` or `deepseek`, while
`OFFLINE_TEST` continues to require `mock`.

- [ ] **Step 2: Run protocol tests and verify failure**

Run: `uv run --frozen pytest tests/unit/test_evaluation_protocol.py -q`

Expected: `deepseek` is rejected by the current provider literal.

- [ ] **Step 3: Implement provider-specific identity validation**

Use:

```python
provider: Literal["mock", "openai", "deepseek"]

if self.provider == "openai":
    valid_response_model = is_exact_or_dated_openai_snapshot(
        self.model_id, response_model_id
    )
else:
    valid_response_model = response_model_id == self.model_id
```

Change the real-model protocol validator to accept the closed set `{"openai", "deepseek"}`.
Do not alter `EvaluationStudyDefinition`, `RealModelAuthorization`, or the old B2.4 pilot gate;
their OpenAI-only digests and semantics remain frozen.

- [ ] **Step 4: Write failing DeepSeek evaluation factory tests**

Mirror the OpenAI factory tests with `provider="deepseek"`. Assert the factory requires a gate,
maps every frozen `ModelProviderConfig` field, returns provider name `deepseek`, and excludes the
secret and `api_key` field from serialized output and repr.

- [ ] **Step 5: Implement `DeepSeekEvaluationProviderFactory`**

Add a sibling factory whose constructor and `create` shape match the OpenAI factory, but whose
closed provider check is `deepseek` and default builder is `DeepSeekModelProvider`. Reuse a private
function to map `ProviderBinding` to `ModelProviderConfig`, preventing OpenAI/DeepSeek drift.

- [ ] **Step 6: Run tests and commit**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol.py tests/unit/test_evaluation_provider_factory.py -q
git add src/agentforge/evaluation/protocol.py src/agentforge/evaluation/provider_factory.py tests/unit/test_evaluation_protocol.py tests/unit/test_evaluation_provider_factory.py
git commit -m "feat: freeze DeepSeek evaluation bindings"
```

---

### Task 5: Add secure DeepSeek secret scanning

**Files:**
- Modify: `src/agentforge/evaluation/public_artifacts.py`
- Modify: `src/agentforge/tools/mutation/security.py`
- Modify: `tests/security/test_mutation_security.py`
- Modify: `tests/evaluation/test_candidate_assets.py`

- [ ] **Step 1: Add failing secret-pattern tests**

Assert both public artifact scanning and mutation-content scanning reject:

```text
DEEPSEEK_API_KEY=sk-sensitive-value
deepseek_api_key = "sk-sensitive-value"
```

Also assert the plain documentation token `DEEPSEEK_API_KEY` without an assignment remains allowed
in runbooks.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
uv run --frozen pytest tests/security/test_mutation_security.py tests/evaluation/test_candidate_assets.py -q
```

- [ ] **Step 3: Extend the closed secret patterns**

Add `DEEPSEEK_API_KEY` beside `OPENAI_API_KEY` in the existing case-insensitive assignment
patterns. Do not introduce a broad `DEEPSEEK` ban because provider names and documentation are
public.

- [ ] **Step 4: Run tests and commit**

```powershell
uv run --frozen pytest tests/security/test_mutation_security.py tests/evaluation/test_candidate_assets.py -q
git add src/agentforge/evaluation/public_artifacts.py src/agentforge/tools/mutation/security.py tests/security/test_mutation_security.py tests/evaluation/test_candidate_assets.py
git commit -m "security: redact DeepSeek runtime secrets"
```

---

### Task 6: Export a standard, base-commit-bound SWE-bench prediction

**Files:**
- Create: `tests/unit/test_swebench_prediction.py`
- Create: `src/agentforge/evaluation/swebench_prediction.py`
- Create: `evaluation/export_swebench_prediction.py`

- [ ] **Step 1: Write failing model and Git-binding tests**

Create a temporary Git repository, commit `source.py`, modify it, and assert:

```python
binding = SWEbenchInstanceBinding(
    instance_id="sympy__sympy-20590",
    repo="sympy/sympy",
    base_commit=base_commit,
)
prediction = SWEbenchPredictionExporter().capture(
    workspace,
    binding=binding,
    model_identity="deepseek/account-model",
)
assert prediction.instance_id == "sympy__sympy-20590"
assert prediction.model_name_or_path == "agentforge:deepseek/account-model"
assert "diff --git a/source.py b/source.py" in prediction.model_patch
assert prediction.base_commit == base_commit
assert len(prediction.patch_sha256) == 64
```

Add rejection cases for wrong `HEAD`, empty patch, symlinked workspace, patch output over 1 MiB,
untracked files, and Git command failure.

- [ ] **Step 2: Run tests and verify import failure**

Run: `uv run --frozen pytest tests/unit/test_swebench_prediction.py -q`

- [ ] **Step 3: Implement frozen models and bounded Git execution**

Use these public models:

```python
class SWEbenchInstanceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instance_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class SWEbenchPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instance_id: str
    model_name_or_path: str
    model_patch: str
    base_commit: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def harness_record(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }
```

Resolve a canonical Git executable once. Run only fixed argument vectors with `shell=False`,
`stdin=DEVNULL`, a minimal locale environment, a 30-second timeout, and a 1 MiB combined output
limit:

```text
git rev-parse --verify HEAD
git status --porcelain=v1 --untracked-files=normal --ignored=no
git diff --binary --no-ext-diff --src-prefix=a/ --dst-prefix=b/ HEAD --
```

Reject any untracked status line (`??`) so the standard patch cannot silently omit created files.
Tracked new files staged by AgentForge remain represented by `git diff HEAD`.

- [ ] **Step 4: Implement atomic JSONL output and CLI**

The CLI accepts only:

```text
--workspace PATH
--instance-id ID
--repo OWNER/REPO
--base-commit SHA
--model-identity PROVIDER/MODEL
--output PATH
```

Write exactly one UTF-8 JSON object plus newline via a sibling temporary file and `os.replace`.
Print only `prediction_sha256=<digest>` and the output filename, never the patch contents.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run --frozen pytest tests/unit/test_swebench_prediction.py -q
uv run --frozen ruff check src/agentforge/evaluation/swebench_prediction.py evaluation/export_swebench_prediction.py tests/unit/test_swebench_prediction.py
uv run --frozen mypy src/agentforge/evaluation/swebench_prediction.py
git add src/agentforge/evaluation/swebench_prediction.py evaluation/export_swebench_prediction.py tests/unit/test_swebench_prediction.py
git commit -m "feat: export bounded SWE-bench predictions"
```

---

### Task 7: Add an opt-in live canary and cloud runbook

**Files:**
- Create: `tests/live/test_deepseek_live.py`
- Create: `docs/deepseek-swebench-canary.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/evaluation_guide.md`
- Modify: `docs/evaluation_guide.zh-CN.md`

- [ ] **Step 1: Add the gated live test**

Follow `tests/live/test_openai_live.py`. Skip unless both `DEEPSEEK_LIVE_TEST=1` and
`DEEPSEEK_API_KEY` are present. Require `DEEPSEEK_MODEL` rather than hard-coding a model ID.
Construct `DeepSeekModelProvider`, send a bounded request with a single read-only function, and
assert either a valid `ToolCall` for that function or a non-empty `FinalAnswer`. Never print the
response or key.

- [ ] **Step 2: Run the live test in skipped mode**

Run: `uv run --frozen pytest tests/live/test_deepseek_live.py -q`

Expected: one skipped test and exit `0`.

- [ ] **Step 3: Write the exact cloud runbook**

Document this safe key entry, with the model ID obtained from the authenticated model listing:

```bash
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY && printf '\n'
export DEEPSEEK_API_KEY
read -rp 'Exact DeepSeek model id: ' DEEPSEEK_MODEL
export DEEPSEEK_MODEL
DEEPSEEK_LIVE_TEST=1 uv run --frozen pytest tests/live/test_deepseek_live.py -q
```

Document the AgentForge product runtime provider block, bounded run, approval handling, prediction
export, and pinned official harness command:

```bash
uv run --frozen python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --split test \
  --instance_ids sympy__sympy-20590 \
  --predictions_path /absolute/path/predictions.jsonl \
  --max_workers 1 \
  --timeout 1800 \
  --run_id agentforge-deepseek-canary-20260816
```

Use `HF_ENDPOINT=https://hf-mirror.com` only for dataset transport. End with container checks,
`unset DEEPSEEK_API_KEY`, artifact hashes, and CVM shutdown.

- [ ] **Step 4: State the claim boundary in both languages**

Add the same factual claim to the README and evaluation guides: AgentForge supports DeepSeek as a
provider, and the official SWE-bench result is a one-instance canary, not a leaderboard score or
general repair-quality claim.

- [ ] **Step 5: Commit**

```powershell
git add tests/live/test_deepseek_live.py docs/deepseek-swebench-canary.md README.md README.zh-CN.md docs/evaluation_guide.md docs/evaluation_guide.zh-CN.md
git commit -m "docs: add DeepSeek SWE-bench canary runbook"
```

---

### Task 8: Verify the complete change and prepare the cloud handoff

**Files:**
- Modify only files required by failures found in this task.

- [ ] **Step 1: Run focused feature tests**

```powershell
uv run --frozen pytest \
  tests/unit/test_deepseek_schema.py \
  tests/unit/test_deepseek_provider.py \
  tests/unit/test_evaluation_protocol.py \
  tests/unit/test_evaluation_provider_factory.py \
  tests/unit/test_swebench_prediction.py \
  tests/security/test_mutation_security.py \
  tests/live/test_deepseek_live.py -ra
```

Expected: all non-live tests pass; live test skips without explicit credentials.

- [ ] **Step 2: Run the full regression suite**

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

Expected: every command exits `0`.

- [ ] **Step 3: Audit secret and endpoint boundaries**

Run:

```powershell
git grep -n -E "DEEPSEEK_API_KEY[[:space:]]*=" -- ':!docs/deepseek-swebench-canary.md'
git grep -n "base_url" -- src/agentforge
git status --short
```

Expected: no committed secret assignments; the only DeepSeek `base_url` value is the fixed
`https://api.deepseek.com` constant; `.superpowers/` remains the only unrelated untracked path.

- [ ] **Step 4: Review the branch diff against the accepted design**

Verify each acceptance criterion in
`docs/superpowers/specs/2026-08-16-deepseek-swebench-design.md` has code, tests, or an explicit
cloud-only verification step. Confirm no official score is claimed before the cloud harness runs.

- [ ] **Step 5: Commit any verification-only corrections**

If Step 1–4 required tracked fixes, commit only those exact files with:

```powershell
git commit -m "test: complete DeepSeek canary verification" -- <changed-tracked-files>
```

If no fixes were required, do not create an empty commit.

- [ ] **Step 6: Record the handoff facts**

Report the branch name, commit list, exact verification commands, pass/skip counts, and the first
cloud command from `docs/deepseek-swebench-canary.md`. Keep the CVM off until this handoff is ready.
