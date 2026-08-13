from agentforge.context.models import LoopObservation, LoopPolicy, LoopState


class LoopDetector:
    def __init__(self, policy: LoopPolicy | None = None) -> None:
        self._policy = policy or LoopPolicy()

    def observe(
        self,
        state: LoopState,
        *,
        action_digest: str,
        result_digest: str,
        error_code: str | None,
    ) -> LoopObservation:
        same_pair = bool(
            state.recent_action_digests
            and state.recent_result_digests
            and state.recent_action_digests[-1] == action_digest
            and state.recent_result_digests[-1] == result_digest
        )
        same_error = bool(
            error_code
            and state.recent_error_codes
            and state.recent_error_codes[-1] == error_code
        )
        pair_count = state.consecutive_same_action_result + 1 if same_pair else 1
        error_count = state.consecutive_same_error + 1 if same_error else (1 if error_code else 0)
        warning = (
            pair_count >= self._policy.warning_threshold
            or error_count >= self._policy.warning_threshold
        )
        terminal = (
            pair_count >= self._policy.terminal_threshold
            or error_count >= self._policy.terminal_threshold
        )
        next_state = LoopState(
            recent_action_digests=[*state.recent_action_digests[-9:], action_digest],
            recent_result_digests=[*state.recent_result_digests[-9:], result_digest],
            recent_error_codes=[
                *state.recent_error_codes[-9:],
                *( [error_code] if error_code else [] ),
            ],
            consecutive_same_action_result=pair_count,
            consecutive_same_error=error_count,
            warning_count=state.warning_count + (1 if warning else 0),
        )
        return LoopObservation(state=next_state, warning=warning, terminal=terminal)
