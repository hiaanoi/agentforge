import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from agentforge.application.contracts import OutcomeStatus
from agentforge.application.kernel_errors import StaleFenceError
from agentforge.application.run_driver import (
    DriverOutcome,
    RunLeaseLostError,
)
from agentforge.domain.enums import ModelRecoveryAction
from agentforge.domain.models import Run
from agentforge.models.base import ModelProvider, ModelRequest
from agentforge.models.domain import ModelErrorCode, ModelResponse
from agentforge.models.errors import (
    ModelOutputInvalidError,
    ModelProtocolError,
    ModelRequestError,
    ProviderContractDeviationError,
)
from agentforge.persistence.event_log import RunAuthorityProvider
from agentforge.persistence.model_workflow import ModelWorkflow


class ModelExecutor:
    def __init__(
        self,
        provider: ModelProvider,
        workflow: ModelWorkflow,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: 0.0,
        provider_identity: str | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._provider = provider
        self._workflow = workflow
        self._sleep = sleep
        self._jitter = jitter
        discovered_identity = getattr(
            provider,
            "journal_identity",
            f"{provider.name}/{type(provider).__module__}.{type(provider).__qualname__}",
        )
        selected_identity = provider_identity or discovered_identity
        if type(selected_identity) is not str:
            raise ValueError("provider must expose a stable model journal identity")
        self._provider_identity = selected_identity
        self._failpoint = failpoint or (lambda _: None)

    @property
    def provider(self) -> ModelProvider:
        return self._provider

    async def generate(
        self,
        run: Run,
        request: ModelRequest,
        *,
        ownership: RunAuthorityProvider,
    ) -> ModelResponse:
        self._require_live_provider_identity()
        state = self._workflow.get_state(run.run_id)
        logical_call_id = uuid4()
        max_attempts = state.budget.max_retries + 1
        for attempt_number in range(1, max_attempts + 1):
            attempt_id = self._workflow.prepare_attempt(
                run.run_id,
                logical_call_id,
                attempt_number,
                request=request,
                provider_identity=self._provider_identity,
                authority=ownership.authority,
            )
            self._failpoint("after_prepared_commit")
            if not self._workflow.claim_dispatch(
                run.run_id,
                attempt_id,
                request=request,
                provider_identity=self._provider_identity,
                authority=ownership.authority,
            ):
                raise RuntimeError("Prepared model attempt lost its dispatch claim")
            self._failpoint("after_dispatching_commit")
            try:
                response = await self._dispatch_claimed_attempt(
                    run,
                    request,
                    attempt_id,
                    attempt_number,
                    ownership=ownership,
                )
            except ModelRequestError as exc:
                if not exc.retryable or attempt_number >= max_attempts:
                    raise
                delay = float(2 ** (attempt_number - 1)) + self._jitter()
                self._workflow.record_retry(
                    run.run_id, attempt_number, delay, authority=ownership.authority
                )
                await self._sleep(delay)
                continue
            if response is None:
                raise RunLeaseLostError()
            return response
        raise RuntimeError("ModelExecutor exhausted attempts without a result")

    async def recover(
        self,
        run: Run,
        request: ModelRequest,
        attempt_id: UUID,
        *,
        ownership: RunAuthorityProvider,
    ) -> DriverOutcome[ModelResponse]:
        """Recover one durable attempt without ever resending uncertain dispatch."""
        decision = self._workflow.recover_attempt(
            run.run_id,
            attempt_id,
            request=request,
            provider_identity=self._provider_identity,
            authority=ownership.authority,
        )
        if decision.action is not ModelRecoveryAction.DISPATCH:
            return DriverOutcome(OutcomeStatus.UNKNOWN, None)
        response = await self._dispatch_claimed_attempt(
            run,
            request,
            attempt_id,
            decision.attempt_number,
            ownership=ownership,
        )
        if response is None:
            return DriverOutcome(OutcomeStatus.UNKNOWN, None)
        return DriverOutcome(OutcomeStatus.UNVERIFIED, response)

    async def _dispatch_claimed_attempt(
        self,
        run: Run,
        request: ModelRequest,
        attempt_id: UUID,
        attempt_number: int,
        *,
        ownership: RunAuthorityProvider,
    ) -> ModelResponse | None:
        """Normalize and persist one Provider outcome for normal and recovery paths."""
        deviation: ProviderContractDeviationError | None = None
        cause: BaseException | None = None
        try:
            self._require_live_provider_identity()
            response = await self._provider.generate(request)
        except ProviderContractDeviationError as exc:
            self._failpoint("after_provider_error")
            deviation = exc
            cause = exc
            error = ModelRequestError(
                ModelErrorCode.MODEL_PROTOCOL_ERROR,
                str(exc),
                retryable=True,
            )
        except (ModelProtocolError, ModelOutputInvalidError) as exc:
            self._failpoint("after_provider_error")
            cause = exc
            error = ModelRequestError(
                (
                    ModelErrorCode.MODEL_PROTOCOL_ERROR
                    if isinstance(exc, ModelProtocolError)
                    else ModelErrorCode.MODEL_OUTPUT_INVALID
                ),
                str(exc),
                retryable=False,
            )
        except ModelRequestError as exc:
            self._failpoint("after_provider_error")
            error = exc
        else:
            self._failpoint("after_provider_response")
            try:
                self._workflow.complete_attempt(
                    run.run_id,
                    attempt_id,
                    response.usage,
                    response.duration_ms,
                    authority=ownership.authority,
                )
            except (RuntimeError, StaleFenceError, RunLeaseLostError):
                return None
            return response.model_copy(update={"attempt_count": attempt_number})

        try:
            if deviation is not None:
                self._workflow.record_provider_deviation(
                    run.run_id,
                    deviation.info,
                    authority=ownership.authority,
                )
            self._workflow.fail_attempt(
                run.run_id,
                attempt_id,
                error,
                authority=ownership.authority,
            )
        except (RuntimeError, StaleFenceError, RunLeaseLostError):
            return None
        if cause is not None:
            raise error from cause
        raise error

    def _require_live_provider_identity(self) -> None:
        """Fail closed if a mutable provider no longer matches its journal binding."""
        try:
            name = self._provider.name
            identity = self._provider.journal_identity
        except Exception as exc:
            raise ModelProtocolError("Provider identity is unavailable") from exc
        if (
            type(name) is not str
            or type(identity) is not str
            or identity != self._provider_identity
            or not identity.startswith(f"{name}/")
        ):
            raise ModelProtocolError("Provider identity changed after assembly")
