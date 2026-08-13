# ADR 0005: Bound waiting time for synchronous tools without claiming termination

## Status

Accepted for Milestone 2.

## Decision

Run synchronous Python tools through `asyncio.to_thread` and apply the ToolSpec timeout around
the await. Return `TOOL_TIMEOUT` when the waiting deadline expires. Do not claim that cancellation
stops the worker thread: Python cannot safely kill an arbitrary thread.

## Consequences

The runtime remains responsive and records a terminal failure, but timed-out synchronous code can
continue in the process. Milestone 2 exposes only bounded read-only tools. Process isolation is
required before this mechanism can safely host write or dangerous tools.
