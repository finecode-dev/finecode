# ADR-0010: Explicit action-scoped progress reporting

- **Status:** accepted
- **Date:** 2026-03-27
- **Deciders:** @Aksem
- **Tags:** actions, progress, architecture

## Context

FineCode actions can already stream **partial results**: incremental result data
that reaches the client before the action fully completes. However, there is no
corresponding way for an action to report **progress**: status metadata such as
what it is doing, how far along it is, or whether it can be cancelled.

That gap matters most for long-running actions such as workspace linting, test
discovery, or dependency installation. Today those actions may appear silent
even when they are working correctly.

Progress is a separate concern from partial results:

- Partial results are part of the action's result contract.
- Progress is metadata about the action's execution.

FineCode also needs one rule that still works when an action orchestrates other
actions, runs work concurrently, or is invoked from different client
environments. Handlers should not need environment-specific branches just to
report progress.

## Related ADRs Considered

- [ADR-0009](0009-explicit-partial-result-token-propagation.md) - defines how
  partial results behave across action boundaries. This ADR applies the same
  boundary and ownership thinking to progress, which is metadata rather than
  result data.

## Decision

FineCode will support **progress as an explicit, action-scoped channel of
metadata**, separate from partial results.

- A handler may report progress for the action contract it owns.
- Progress does not implicitly flow across action boundaries. If a parent action
  delegates work and wants client-visible progress, the parent handler owns that
  progress narrative.
- The framework must provide a single handler-facing progress mechanism that
  works in sequential and concurrent code and degrades to a no-op when the
  caller did not request progress.
- The handler-facing mechanism must make correct progress lifecycle management
  the default, so handlers do not need ad hoc begin/report/end coordination on
  every code path.
- Progress remains transport-agnostic at the handler boundary. Different
  FineCode clients may render or transport the same progress events differently
  without changing handler logic.
- Progress and partial results remain orthogonal. An action may use either, both,
  or neither.

## Consequences

- **Long-running actions become observable.** Clients can show live status for
  operations that would otherwise appear stalled.
- **Handler ownership stays explicit.** Parent actions keep control of the
  client-visible progress contract at their own boundary.
- **Handler code stays mode-agnostic.** The same handler logic works whether or
  not progress was requested by the caller.
- **The handler-facing API grows.** FineCode needs a public progress mechanism
  for action authors, even though simple handlers may ignore it.
- **Cancellation remains forward-compatible.** Progress may expose whether an
  action is cancellable, but end-to-end cancellation semantics should be
  revisited when client-originated cancellation is wired through to handlers.
- **Request-level aggregation is a separate concern.** How FineCode combines
  multiple child progress streams into one client-visible stream will be addressed
  separately in further ADRs.

### Alternatives Considered

**Standalone progress calls without lifecycle mediation.** Rejected because
progress protocols generally require start, update, and end events in the right
order, including on error paths. FineCode should make the correct lifecycle the
default rather than relying on every handler to coordinate it manually.

**Reuse the partial-result channel for progress updates.** Rejected because it
mixes result data and execution metadata into one stream and weakens the action
boundary that [ADR-0009](0009-explicit-partial-result-token-propagation.md)
made explicit.

**Automatic framework-generated progress.** Rejected because the framework
cannot reliably infer meaningful status messages or units of work for arbitrary
actions. Progress needs explicit handler ownership.

### Implementation Notes

- The current handler-facing API uses `ProgressSender`, `ProgressContext`, and
  `RunActionContext.progress()`.
- Progress uses a token distinct from `partial_result_token`.
- Current transports may adapt the same progress events for LSP, CLI, MCP, or
  other clients without changing the architectural rule in this ADR.
