# ADR-0013: Action declares handler execution strategy

- **Status:** accepted
- **Date:** 2026-03-29
- **Deciders:** @Aksem
- **Tags:** actions, architecture

## Context

When an action has multiple handlers, FineCode needs an explicit rule for how
those handlers relate to one another.

Some actions are **ordered pipelines**: each handler transforms the result of
the previous one, so handler order is part of the action's semantics. Other
actions are **independent fan-out**: handlers produce separate results, so the
action does not depend on a particular order.

If this relationship is not declared explicitly, the framework must infer it
from naming conventions or other implementation-specific heuristics. That makes
the action contract harder to review, easier to misapply, and less durable as
the implementation evolves.

The design also needs to stay clear about scope:

- This ADR concerns the relationship **between handlers within one action**.
- It does not decide whether a collection action processes different items
  sequentially or in parallel.
- It must apply consistently whether the action processes one unit of work
  directly or schedules work that is later associated back to individual items.

The architectural question is therefore not "how should the current runtime
implement concurrency," but "where does the action's handler relationship
belong as part of the contract?"

### Where to declare the strategy

Three levels were considered:

- **Per-handler**: each handler declares its own execution preference. Rejected
  because the relationship is between handlers, not inside one handler.
  Individual handlers do not have enough context to define the contract for the
  whole action, and conflicting declarations would be ambiguous.
- **Per-action**: the action definition declares the strategy for how its
  handlers relate. This places the decision with the action designer, who owns
  the action's semantics.
- **Per-invocation**: the caller or dispatcher chooses the strategy at runtime.
  Rejected because the handler relationship is part of what the action means,
  not a caller-specific tuning option.

## Related ADRs Considered

- [ADR-0012](0012-item-level-and-collection-level-action-granularity.md) — defines item-level and collection-level action granularity. The handler execution strategy applies at both levels: item actions with sequential handlers (format), collection actions with concurrent handlers (lint). The two ADRs are complementary.
- [ADR-0009](0009-explicit-partial-result-token-propagation.md) — defines partial result flow across action boundaries. This ADR complements it by defining how one action's handlers relate when that action emits work incrementally.

## Decision

The **action definition** declares one handler execution strategy for the
action. The framework applies that strategy uniformly wherever that action's
handlers are composed.

- **Sequential** (default): handlers run in definition order. Each handler's output can feed the next handler's input. Appropriate for transform pipelines (formatting: isort → ruff → save).

- **Concurrent**: handlers run in parallel. Handlers produce independent results. Appropriate for independent analyzers (linting: ruff + mypy).

### The action designer owns this decision

The execution strategy describes how handlers within the action interact — whether they form a sequential pipeline (each transforms the previous output) or run independently (each produces separate results). This is a property of the action's semantics:

- A formatting action is a sequential pipeline by design: handler order is part of the semantics.
- A linting action runs independent analyzers by design: handler order is irrelevant.

Individual handlers cannot correctly determine this. A handler only knows about itself — it cannot know whether other handlers in the chain depend on its output or run independently. Placing this decision on handlers would allow contradictory declarations and shift responsibility to the wrong party.

### No mixed execution within a single action

If one action would need some handlers to form a sequential pipeline and others
to behave as independent concurrent peers during the same processing step, that
is a sign that the action is combining distinct concerns. The correct response
is to split it into multiple focused actions, each with a clear handler
relationship, and compose them through orchestration.

This rule is about **handler processing semantics**, not about every lifecycle
phase that may exist around handler execution. Framework-managed setup,
teardown, or other lifecycle behavior may still evolve independently so long as
the action exposes one coherent handler relationship during processing.

This is consistent with [ADR-0012](0012-item-level-and-collection-level-action-granularity.md) and the design principle "prefer multiple focused actions over one overloaded action."

### Item-level parallelism is a separate concern

Whether a collection action processes different items sequentially or in
parallel is not governed by this decision. That is an orchestration concern. An
action may therefore have sequential handler semantics per item while still
being orchestrated across multiple items in parallel.

## Consequences

- **Action semantics become explicit.** Reviewers can see whether an action is
  an ordered pipeline or a set of independent handlers without relying on
  implementation-specific behavior.
- **Handler responsibilities stay narrow.** Individual handlers do not need to
  negotiate concurrency policy with one another.
- **The same contract applies across action shapes.** The action's handler
  relationship does not change just because the action is used for one item or
  for a collection.
- **Mixed processing semantics require composition.** Designs that genuinely
  need both pipeline and fan-out semantics must express that through multiple
  actions or explicit orchestration boundaries.
- **The model can be extended later if needed.** If future action designs show a
  recurring architectural need for more than one handler-processing dimension, a
  later ADR can refine this rule without changing the meaning of the current
  one.

### Alternatives Considered

**Handler-level declaration.** Each handler declares whether it should run
sequentially or concurrently. Rejected because handler execution strategy is a
relationship across the action, not a local property of one handler.

**Configuration-level override.** The execution strategy is supplied from
project configuration rather than from the action definition. Rejected as the
primary mechanism because handler execution strategy is intrinsic to the action
contract and should travel with the action definition.

**Separate strategies for direct handler composition and per-item handler
composition.** Deferred because current use cases treat these as the same
architectural relationship. Revisit this if FineCode gains stable action
semantics where handlers must be sequential in one processing context and
concurrent in another, and that difference cannot be expressed cleanly as a
lifecycle concern rather than a processing contract.

### Related Decisions

- Complements [ADR-0012](0012-item-level-and-collection-level-action-granularity.md) (item/collection granularity)
- Clarifies the handler-composition side of incremental per-item work described alongside [ADR-0009](0009-explicit-partial-result-token-propagation.md)

## Implementation Notes

- In the current implementation, the same declared strategy should be applied
  both when handlers run directly and when handler-produced work is later
  grouped per item through `partial_result_scheduler`.
- Runtime mechanisms such as task groups, scheduler internals, or future
  lifecycle hooks are implementation choices rather than the architectural
  decision itself.
