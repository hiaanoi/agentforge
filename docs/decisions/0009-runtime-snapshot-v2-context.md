# ADR 0009: Make RuntimeSnapshot v2 the Context Recovery Contract

## Status

Accepted for Milestone 4.

## Decision

RuntimeSnapshot v2 stores typed ContextItems, compatibility history, pending tool/approval state,
model usage and request count, context accounting, exact-repeat LoopState, bounded provider
metadata, policy versions, and resume phase. Normal tool and approval checkpoints both write v2.

Unversioned history and M3 v1 snapshots migrate deterministically on read. Unknown future versions
fail explicitly. Context compaction removes only complete call/result pairs; consumed approval
results and loop state survive process recreation.

## Consequences

- Local checkpoints, not provider conversation state, define recovery.
- Schema evolution has an explicit strict boundary and migration path.
- The compatibility history duplicates some persisted context data during migration.
- Checkpoints contain recovery-sensitive raw arguments/results and require database protection.
