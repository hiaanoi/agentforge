# ADR 0006: Persist minimized tool audit payloads

## Status

Accepted for Milestone 2.

## Decision

Persist sanitized arguments in `TOOL_REQUESTED`, but never tool output or traceback in terminal
tool events. Recursively redact content, text, query, token, secret, credential, and private-key
keys; bound retained strings; and redact absolute or centrally classified sensitive path values.
Terminal events contain only decision, duration, success, error type, and truncation metadata.

## Consequences

Audits retain operational evidence without duplicating repository content. Sanitization is
best-effort for arbitrary third-party schemas: secrets under semantically unrelated keys cannot
be detected reliably, so future external tool sources require schema-level sensitive annotations.
