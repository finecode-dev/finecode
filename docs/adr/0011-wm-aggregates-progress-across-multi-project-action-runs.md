# ADR-0011: WM aggregates progress for multi-execution requests

- **Status:** accepted
- **Date:** 2026-03-28
- **Deciders:** @Aksem
- **Tags:** actions, progress, architecture, wm-server

## Context

[ADR-0010](0010-progress-reporting-for-actions.md) defines progress as an
explicit action-scoped contract owned by the current handler boundary. That
works cleanly for a single action execution, but some WM-level requests fan out
into multiple executions behind one client-visible operation.

This happens in at least two shapes:

- one action executed across multiple projects
- multiple actions executed as one batch request such as `runBatch`

In both cases, the client still expects one progress stream for the overall
request, while the WM may dispatch work to multiple child executions. Unlike
partial results, multiple progress lifecycles cannot simply be interleaved under
one client token. Competing start, update, and end events would produce erratic
percentages and confusing messages.

FineCode therefore needs an architectural rule for who owns the client-visible
progress stream whenever one request fans out into multiple progress-emitting
executions, and how those child progress streams should be combined without
making handlers aware of WM-level coordination.

## Related ADRs Considered

- [ADR-0009](0009-explicit-partial-result-token-propagation.md) - partial
  results can be interleaved across delegated work because each item is result
  data. Progress lifecycle events do not have that property.
- [ADR-0010](0010-progress-reporting-for-actions.md) - defines the handler-side
  progress contract. This ADR defines how the WM combines multiple project-level
  progress streams for one client-visible operation.

## Decision

For any client-visible request that fans out into multiple child executions, the
**WM owns the client-visible progress stream**.

- The WM must not forward multiple child progress lifecycles directly under one
  client token.
- The WM must issue distinct internal progress identities for child executions
  and aggregate them into the single progress stream expected by the client.
- Handler and ER code remain unaware of WM-level aggregation. They report
  progress for their own action scope only.
- This rule applies whether the fan-out dimension is multiple projects,
  multiple actions, or both.
- When child progress includes measurable totals, the WM should aggregate
  client-visible percentage proportionally to total work across child
  executions rather than giving every child equal weight.
- When child progress is indeterminate, the WM may forward narrative updates
  without inventing false precision. If no determinate totals are available,
  the client-visible progress should remain indeterminate until the work
  completes.

## Consequences

- **One client request gets one coherent progress stream.** Clients do not see
  conflicting begin/report/end sequences from different child executions.
- **Progress remains accurate across uneven work sizes.** A large project or
  action can contribute more of the overall percentage than a small one when
  totals are known.
- **Handler contracts stay simple.** Handlers do not need request-level
  aggregation logic or awareness of sibling executions.
- **The WM gains aggregation state and protocol responsibilities.** It must
  track internal progress identities, combine updates, and normalize what it
  forwards.
- **Narrative updates may be richer than percentages.** Some child executions will
  contribute messages even when they cannot contribute meaningful percentage
  values.

### Alternatives Considered

**Directly interleave child progress under one client token.** Rejected
because progress lifecycle events conflict when multiple executions share one
visible stream.

**Only report coarse completion at the WM level.** Rejected because it throws
away useful progress from within each child execution and makes large fan-out
requests feel artificially coarse.

**Give every child execution an equal slice of the percentage range.** Rejected
because it distorts overall progress when the amount of work differs
substantially.

### Implementation Notes

- A natural implementation is for the WM to mint one internal progress token per
  child execution and map those internal streams onto the client-visible token.
- When totals are available, the WM can combine them into a shared total and
  derive global completion from per-child completion.
- If FineCode later needs nested or hierarchical progress UI, this ADR should be
  revisited before layering additional semantics onto the same stream.
