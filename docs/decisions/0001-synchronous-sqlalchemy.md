# ADR 0001: Use synchronous SQLAlchemy for the first SQLite store

## Status

Accepted for Milestone 1.

## Context

The first runtime stores small run, event, and checkpoint records in a local SQLite file.
Model generation and tool execution are asynchronous, but database requests are short and no
concurrent service workload exists yet.

## Decision

Use SQLAlchemy 2.x synchronous sessions behind repository interfaces. Keep the runtime's model
and tool boundaries asynchronous.

## Consequences

This removes an unnecessary async database dependency and keeps transaction behavior easy to
test. Before introducing concurrent API workers, the persistence implementation must be
revisited for non-blocking access and robust concurrent event sequence allocation.

