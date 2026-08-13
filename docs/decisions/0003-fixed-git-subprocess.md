# ADR 0003: Use a fixed Git subprocess for read-only diff

## Status

Accepted for Milestone 2.

## Context

Adding GitPython solely for one read-only operation would expand dependencies and lock-file
churn without improving the current safety boundary.

## Decision

Use fixed argument arrays for repository probing and `git diff`. Set the workspace as `cwd`, use
`shell=False`, disable external diff and text conversion, set a timeout, capture output, and
never place model input on the command line. Remove inherited `GIT_*` variables before adding the
small fixed environment required by this tool, preventing `GIT_DIR`, `GIT_WORK_TREE`, and config
injection from changing the repository or command behavior.

## Consequences

The implementation depends on an installed Git executable but has a small auditable command
surface. Milestone 2 returns unstaged working-tree changes only. The returned diff is bounded,
but the current `subprocess.run` capture is in memory; streaming capture remains technical debt.
