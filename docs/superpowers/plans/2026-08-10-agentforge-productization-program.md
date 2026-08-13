# AgentForge Productization Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a trustworthy, installable code-repair Agent portfolio product for Agent application development recruiting.

**Architecture:** Keep the existing single-model `AgentRuntime`, controlled tools, approval workflow, test profiles, and evaluator. Add a durable product kernel first, expose it through a deep `AgentApplication`, then add interactive conversation and independently reproducible evidence.

**Tech Stack:** Python 3.11+, Pydantic v2, SQLAlchemy 2, SQLite, asyncio, pytest/pytest-asyncio, Ruff, mypy strict, Hatch wheel, GitHub Actions.

---

## Source of Truth

- Design: `docs/superpowers/specs/2026-08-10-agentforge-recruiting-productization-design.md`
- A0: `docs/superpowers/plans/2026-08-10-agentforge-a0-evidence-truth.md`
- A1: `docs/superpowers/plans/2026-08-10-agentforge-a1-durable-product-kernel.md`
- A2: `docs/superpowers/plans/2026-08-10-agentforge-a2-minimal-product-cli.md`
- B: `docs/superpowers/plans/2026-08-10-agentforge-b-interactive-conversation.md`
- C: `docs/superpowers/plans/2026-08-10-agentforge-c-evidence-release.md`

## Dependency Order

```text
A0 Evidence Truth
        |
        v
A1 Durable Product Kernel
        |
        v
A2 AgentApplication + Minimal CLI ----> Core Release
        |                                      |
        v                                      v
B Interactive Conversation ------------> Full C Release
```

D (`Anthropic`, MCP, long-term memory) is intentionally absent. It starts only after the Core or
Interactive Release has shipped and has a separate design and evaluation.

## Program Gates

### Gate 1: Evidence is honest

- A0 tests and report migration pass.
- No schema-v1 artifact is published as current evidence.
- `pass@1`, `pass@3`, `first_attempt_success`, slot denominators, and incomplete reports reconcile.

### Gate 2: Durable kernel is safe to expose

- Atomic Run creation, receipt idempotency, scope-aware EventLog, source revision chain,
  lease/fencing, Provider attempt journal, and profile trust tests pass.
- Core failpoints have deterministic terminal/recovery classifications.
- No CLI command is added before this gate.

### Gate 3: Core Release

- Fresh wheel exposes `agentforge`.
- `exec -> approval -> approve -> new-process resume -> inspect` passes on Windows and Linux.
- Default event/view output passes the public artifact scanner.
- Three-minute deterministic repair and ninety-second recovery demos pass from scripts.

### Gate 4: Interactive Release

- Conversation/Turn/Message idempotency and cross-process chat recovery pass.
- Cooperative cancellation reports `REQUESTED`, `CANCELLED`, or `INDETERMINATE` honestly.

### Gate 5: Evidence Release

- Eight failpoints, two frozen 4x3 real-model arms (24 planned slots), regenerated reports, CI artifacts, README,
  architecture diagrams, failure cases, and STAR cards agree with the same manifests.

## Execution Rules

- Execute plans in order; do not parallelize A1 with A2 or B.
- Within a plan, use TDD and commit after each task.
- Never run live-model tests unless the command explicitly includes `--run-live` and the user has
  separately authorized cost-bearing execution.
- Stop on any failing release gate. Do not weaken a test, denominator, fencing check, or redaction
  rule to make a gate green.
- Preserve legacy raw evaluation facts; mark them legacy rather than deleting them.
