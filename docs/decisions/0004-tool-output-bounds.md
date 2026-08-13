# ADR 0004: Bound output at both tool and executor layers

## Status

Accepted for Milestone 2.

## Decision

Repository tools apply domain-specific limits that preserve structured output. `ToolExecutor`
then JSON-serializes any successful output and applies a final character limit. If that final
limit is exceeded, it returns a bounded serialized preview, marks `truncated`, and records the
original character count in metadata.

## Consequences

No tool can bypass the global output budget. A globally truncated structured result becomes a
string preview; consumers must inspect `truncated` before assuming the tool-specific shape.
The Git subprocess currently captures stdout before applying this bound, so the bound protects
the model context and persisted history rather than serving as a process-memory limit.
