from agentforge.models.deepseek_provider import DeepSeekModelProvider


class OpenAIChatCompletionsProvider(DeepSeekModelProvider):
    """OpenAI-identity adapter for OpenAI-compatible relay Chat Completions."""

    @property
    def name(self) -> str:
        return "openai"
