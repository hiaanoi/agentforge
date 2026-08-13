# ADR 0008: Persist Physical Model Attempts Before Network Access

## Status

Accepted for Milestone 4.

## Decision

Keep Run step counts as logical model turns and persist physical provider requests separately.
`ModelExecutor` reserves each attempt before network access, disables SDK retries, classifies safe
retry categories, and applies injected backoff/jitter outside database transactions.

Request budgets count physical attempts. Token totals use only provider-reported usage and update
after a successful response. Character/byte context limits are not recorded as token usage.

## Consequences

- Retry and budget behavior is auditable and deterministic in offline tests.
- A failed retry still consumes request budget.
- A process crash after provider acceptance can leave an ambiguous billed attempt and a later
  duplicate request; exactly-once provider execution is not claimed.
- Precise pre-request token limits require future tokenizer integration.

