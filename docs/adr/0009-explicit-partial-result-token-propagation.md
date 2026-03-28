# ADR-0009: Explicit handler mediation of partial results across action boundaries

- **Status:** accepted
- **Date:** 2026-03-24
- **Deciders:** @Aksem
- **Tags:** actions, partial-results, architecture

## Context

FineCode supports **partial results**: incremental client-visible data that can
be sent before an action fully completes. A direct action invocation can already
emit partial results when the caller provides a `partial_result_token`.

The gap appears when one action delegates to another. Higher-level actions such
as `lint` and `format` may orchestrate more specific subactions such as
`lint_files` or `format_files`. Without an explicit cross-action rule, partial
results produced inside a delegated action either stop at the delegation
boundary or bypass the parent action's contract entirely.

That boundary matters for three reasons:

1. The parent action owns the client-facing contract and may need to reshape a
   delegated action's partial result before exposing it.
2. Orchestrator handlers should use the same logic whether partial results are
   enabled or not.
3. The rule must work for both sequential delegation and concurrent
   orchestration.

## Related ADRs Considered

- [ADR-0007](0007-single-registration-per-action-definition.md) - requires
  multi-language orchestration to use explicit composition rather than duplicate
  registrations. This ADR defines how partial results behave across that
  composition boundary.
- [ADR-0008](0008-explicit-specialization-metadata-for-language-actions.md) -
  specialization metadata for language-specific actions. Related to dispatch and
  orchestration, but not to partial-result flow.

## Decision

When one action delegates work to another, **partial results do not implicitly
propagate across the action boundary**.

If the parent action wants client-visible partial results while delegating, the
parent handler must explicitly:

- consume partial results from the delegated action
- decide whether to expose them at all
- map them into the parent action's client-facing result shape before
  re-emitting them

The parent handler therefore owns the partial-result contract at its boundary in
the same way it owns the final result contract.

The framework must support this rule with explicit handler-facing mechanisms
that allow a handler to:

- consume delegated partial results incrementally
- emit partial results from handler code without branching on whether partial
  results are active
- use the same architectural rule in both sequential and concurrent
  orchestration patterns

## Consequences

- **Parent action boundaries stay explicit.** Nested actions cannot
  accidentally stream directly through a parent action without that parent's
  involvement.
- **Result-shape ownership is clear.** A parent handler can translate,
  filter, enrich, or suppress delegated partial results before the client sees
  them.
- **Orchestrator handlers stay mode-agnostic.** The same handler logic can run
  with or without partial-result support, with framework-provided no-op
  behavior when streaming is inactive.
- **Sequential and concurrent orchestration are both supported.** The
  architectural rule is the same even when the concrete emission mechanism
  differs.
- **Orchestrator APIs become slightly broader.** Handlers that compose other
  actions need explicit framework support for consuming and re-emitting partial
  results. Simple non-orchestrating handlers are unaffected.

### Alternatives Considered

**Implicit propagation through nested action calls.** Rejected because it lets a
delegated action bypass the parent action's contract boundary. That makes
result-shape mismatches and accidental exposure of subaction behavior much more
likely.

**Forward the token but let the subaction send results directly.** Rejected
because the parent handler would lose control over client-visible partial-result
shape and timing. A parent action needs to own both.

**Only support manual handler-side emission.** Rejected because it makes the
common sequential orchestration case more verbose than necessary.

**Only support yield-style emission.** Rejected because some concurrent
orchestration patterns cannot emit from the place where results become
available.

**Introduce a separate streaming-handler protocol now.** Deferred. Revisit if
streaming handlers create recurring type-checking friction or require repeated
runtime-only workarounds during handler authoring.

## Implementation Notes

The current implementation uses two handler-facing mechanisms:

- `IActionRunner.run_action_iter()` for consuming delegated partial results
- `RunActionContext.send_partial_result()` for explicit emission from handler
  code

Async-generator handler `run()` methods are the preferred pattern for simple
sequential mapping. Explicit `send_partial_result()` remains the escape hatch
for concurrent patterns such as `TaskGroup`, where yielding from the point of
completion is not practical.

Current runtime details such as async-generator detection, queue-based bridging,
and how final return values are handled after explicit partial emission are
implementation choices rather than the architectural decision itself.
