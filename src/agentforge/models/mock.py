from copy import deepcopy

from agentforge.domain.errors import ModelProviderError
from agentforge.models.base import ModelRequest, parse_model_output
from agentforge.models.domain import ModelResponse


class MockModelProvider:
    def __init__(
        self,
        responses: list[object],
        *,
        model_id: str = "mock",
        provider_name: str = "mock",
        response_by_step: bool = False,
    ) -> None:
        self._responses = deepcopy(responses)
        self._position = 0
        self._model_id = model_id
        self._provider_name = provider_name
        self._response_by_step = response_by_step
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def journal_identity(self) -> str:
        return f"{self.name}/{self._model_id}"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        position = request.step_number - 1 if self._response_by_step else self._position
        if position >= len(self._responses):
            raise ModelProviderError("MockModelProvider has no responses remaining")
        response = deepcopy(self._responses[position])
        if not self._response_by_step:
            self._position += 1
        return ModelResponse(
            action=parse_model_output(response),
            provider=self.name,
            model=self._model_id,
            duration_ms=0,
            attempt_count=1,
        )
