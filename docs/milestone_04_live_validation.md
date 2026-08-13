# Milestone 4.1 Report: Live OpenAI Validation

## Status

**Provider-deviation policy PASS; live completion 2/3.** Completed on July 14, 2026.

## Scope

The opt-in test used a generated temporary repository containing only `runtime.py` and `policy.py`.
Runtime exposed `list_files`, `read_file`, and `search_text`; no write, shell, Git mutation,
general network tool, or real AgentForge workspace access was enabled.

Each Run had a five-request model budget and five-call tool budget. Response storage and parallel
tool execution were disabled. Metrics contained only counts, booleans, statuses, and event
presence.

## Provider Contract Deviation

The initial live test proved that a real Responses request could return multiple function calls
despite `parallel_tool_calls=False`. AgentForge no longer treats unconditional first-call selection
as an implicit default.

The formal policy is:

- STRICT: no tool execution, stable MODEL_PROTOCOL_ERROR, sanitized
  MODEL_PROVIDER_DEVIATION, and bounded physical-request retry.
- SEQUENTIAL_READ_ONLY: allowed only when every call is a registered local READ tool without
  approval and the response stays within `max_function_calls_per_response`.

Only the selected call arguments cross the Provider boundary. Discarded call IDs and arguments are
not decoded into domain objects, events, context, approvals, or checkpoints.

## Three live runs

### Run 1

```text
result: PASS
duration: 66.46s
provider_returned_multiple_calls: true
max_returned_call_count: 2
selected_call_count: 2
discarded_call_count: 2
model_request_count: 5
tool_call_count: 3
completed_final_answer: true
cited_runtime_path: true
cited_policy_path: true
within_request_budget: true
within_tool_budget: true
provider_deviation_event: true
normalization_event: true
```

### Run 2

```text
result: FAILED
duration: 47.47s
error: MODEL_TIMEOUT
provider_response_received: false
provider_returned_multiple_calls: false
returned_call_count: 0
selected_call_count: 0
discarded_call_count: 0
model_request_count: 2
tool_call_count: 0
completed_final_answer: false
within_request_budget: true
within_tool_budget: true
provider_deviation_event: false
model_retry_scheduled: true
```

Both configured 20-second attempts timed out before a Provider response. The Run failed closed and
executed no tool. The live-only timeout was then raised to 45 seconds; production timeout remains
configuration-driven.

### Run 3

```text
result: PASS
duration: 68.02s
provider_returned_multiple_calls: true
max_returned_call_count: 2
selected_call_count: 1
discarded_call_count: 1
model_request_count: 5
tool_call_count: 3
completed_final_answer: true
cited_runtime_path: true
cited_policy_path: true
within_request_budget: true
within_tool_budget: true
provider_deviation_event: true
normalization_event: true
```

## Offline regression

- Full suite: 157 collected, 154 passed, 3 skipped, 0 failed.
- Skips: the opt-in live test in the offline run and two Windows symlink tests blocked by
  `WinError 1314`.
- Ruff: passed.
- strict mypy: 44 source files, no issues.
- compileall: passed.
- `git diff --check`: passed with line-ending conversion warnings only.

## Remaining debt

- Live completion was 2/3 because of external Provider latency.
- STRICT retries resend the same logical request without a Provider-specific correction message.
- SEQUENTIAL_READ_ONLY may require extra model turns and therefore additional latency/tokens.
- Only one OpenAI model/account/environment has been exercised.
