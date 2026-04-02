# ADR-0015: Dedicated per-process WAL streams for durable execution lifecycle events

- **Status:** accepted
- **Date:** 2026-04-01
- **Deciders:** @finecode-maintainers
- **Tags:** architecture, reliability, recovery, logging, wal

## Context

FineCode already emits diagnostic logs for troubleshooting, but diagnostic logs
are not a durable replay contract. They may be rotated, truncated, or mixed
with unrelated operational messages.

FineCode now executes work across multiple process boundaries, primarily the
Workspace Manager (WM) and Extension Runners (ERs). Recovery, audit, and
post-mortem analysis need a machine-readable event history that survives process
restarts and can correlate related activity across those writers.

We also need a clear ownership boundary. Durable execution lifecycle state is
owned by WM and ERs, while CLI, LSP, and MCP act as stateless clients or
proxies. The architecture should preserve that boundary rather than duplicating
durable lifecycle records in every frontend process.

## Related ADRs Considered

- Reviewed [ADR-0003](0003-process-isolation-per-extension-environment.md) — establishes per-environment runner process boundaries, but not durable cross-process event storage.
- Reviewed [ADR-0009](0009-explicit-partial-result-token-propagation.md) — defines partial-result boundary semantics, not durable persistence or replay.
- Reviewed [ADR-0011](0011-wm-aggregates-progress-across-multi-project-action-runs.md) — defines WM-side aggregation behavior, not recovery/audit storage.

## Decision

FineCode uses write-ahead logging (WAL) as the dedicated durable event source
for execution lifecycle state.

The architectural rules are:

1. WAL is separate from diagnostic logging. Diagnostic logs remain
   human-oriented operational output; WAL carries machine-readable durable
   lifecycle events.
2. Each process that owns durable lifecycle transitions writes its own
   append-only WAL stream. In the current architecture, that includes WM and ER.
3. Related records across process-local streams are correlated by stable
   identifiers rather than by shared writers or shared mutable state.
4. Replay, audit, and recovery are read-side concerns. Readers may merge
   multiple WAL streams logically, but writers remain process-local.
5. Stateless clients and proxies such as CLI, LSP, and MCP propagate
   correlation identifiers when needed, but do not own independent WAL streams
   for execution lifecycle state.
6. WAL provides durable history first. Resumable execution, checkpointing, or
   other higher-level recovery behavior may build on that history later, but are
   not implied by WAL alone.

## Consequences

Benefits:

- Recovery and audit rely on dedicated durable event streams instead of parsing
  human-oriented logs.
- Diagnostic logging can stay focused on operability without becoming the
  product's replay contract.
- The model scales across process boundaries by adding process-local writers
  under one correlation contract instead of introducing shared write paths.
- Frontend processes stay simpler because they do not need their own durable
  execution-state stores.

Trade-offs:

- WAL adds disk I/O, storage management, and retention policy ownership.
- Readers must correlate and order records across multiple streams during
  replay, recovery analysis, or audit.
- WAL alone does not provide resumable execution; higher-level recovery
  features would require additional policy and state management.
