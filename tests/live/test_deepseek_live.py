from __future__ import annotations

import os

import pytest

from agentforge.domain.enums import ToolRisk, ToolSource
from agentforge.domain.models import ToolSpec
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.deepseek_provider import DeepSeekModelProvider
from agentforge.models.domain import ModelProviderConfig

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_deepseek_returns_one_bounded_action() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("DEEPSEEK_MODEL")
    if os.getenv("DEEPSEEK_LIVE_TEST") != "1" or not api_key or not model:
        pytest.skip("Live DeepSeek test requires explicit opt-in, API key, and exact model")

    provider = DeepSeekModelProvider(
        ModelProviderConfig(
            api_key=api_key,
            model=model,
            timeout_seconds=30,
            max_retries=0,
            max_output_tokens=400,
        )
    )
    request = ModelRequest(
        task="Inspect README.md with the registered read_file tool, or answer briefly.",
        instructions="Return at most one action. Do not invent additional tools.",
        step_number=1,
        history=[],
        tools=[
            ToolSpec(
                name="read_file",
                description="Read one workspace-relative text file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                risk_level=ToolRisk.READ,
                source=ToolSource.LOCAL,
                requires_approval=False,
            )
        ],
    )

    response = await provider.generate(request)

    if isinstance(response.action, ToolCall):
        assert response.action.tool == "read_file"
    else:
        assert isinstance(response.action, FinalAnswer)
        assert response.action.answer.strip()
