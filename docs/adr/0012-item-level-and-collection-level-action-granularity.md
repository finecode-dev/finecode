# ADR-0012: Actions that process multiple items distinguish item and collection granularity

- **Status:** accepted
- **Date:** 2026-03-29
- **Deciders:** @Aksem
- **Tags:** actions, architecture

## Context

Some FineCode actions process more than one item of the same kind in one request, such as files, tests, or artifacts. This ADR addresses those actions.

That creates a tension between two handler needs:

1. **Per-item handlers** — formatters, simple linters — process each item independently. They gain nothing from seeing the full item list and become simpler when they receive exactly one item.

2. **Batch-aware handlers** — type checkers, cross-file analyzers — benefit from receiving all items at once for whole-program analysis, shared caches, or batch-optimized tool invocations.

If every such action is modeled only as a collection-level contract, single-item usage and naturally per-item handlers become awkward. A caller with one item must still use a batch-shaped payload and result model, and a handler that only transforms one item must still adapt itself to a collection contract.

If every such action is modeled only as an item-level contract, batch-aware handlers become harder to express. Tools that need the full item set for correctness, shared context, or efficiency would be forced through per-item invocation patterns that do not match their natural unit of work.

The design needs to support both per-item and batch-aware handlers without forcing one model onto every action that processes multiple items.

## Related ADRs Considered

- [ADR-0008](0008-explicit-specialization-metadata-for-language-actions.md) — defines language-specific specialization metadata that applies to both item-level and collection-level actions. This ADR is orthogonal: it addresses payload cardinality, not language dispatch.
- [ADR-0009](0009-explicit-partial-result-token-propagation.md) — defines how partial results flow across action boundaries. Collection-level actions use partial results to stream per-item results; item-level actions typically do not need partial results since they produce a single result.
- [ADR-0010](0010-progress-reporting-for-actions.md) — defines progress reporting for actions. Collection-level actions own per-item progress; item-level actions do not report progress for their single item (the parent orchestrator owns the narrative).

## Decision

FineCode will support **two complementary action granularity levels** for actions that process multiple items:

1. **Item action** — the payload carries a single item, the result describes that one item. Handlers receive and return exactly one unit of work.

2. **Collection action** — the payload carries a list of items, and the result describes that batch, typically with entries keyed per item. Handlers receive the full list and may iterate over items, batch-optimize, or cross-reference items.

Both levels are ordinary actions in the FineCode action model. The distinction is architectural rather than hierarchical: the difference is in payload cardinality and handler intent, not in a separate framework category.

### Main handler action

When both item and collection actions exist for the same kind of operation, one usually serves as the **main handler action**. That is the action where the main handler logic naturally lives. The other action mainly adapts to it.

- **Item-primary**: the main handlers attach to the item action. Appropriate when each tool processes one item independently (e.g. formatting).

- **Collection-primary**: the main handlers attach to the collection action. An item action may exist as a convenience entry point for single-item callers. Appropriate when tools benefit from seeing all items at once (e.g. linting with type checkers).

### Coexistence of both levels

Both granularities may exist for the same kind of operation. In most cases, one of the corresponding actions serves as the main handler action: either an item-primary design or a collection-primary design. A collection-primary design may offer an item action as a convenience entry point for single-item callers, but it is not required.

The architectural rule is that item and collection granularity remain distinct contracts. How a non-primary level adapts to the primary level is a design-guide concern rather than part of this decision record.

### Language subactions at both levels

Language-specific specialization ([ADR-0008](0008-explicit-specialization-metadata-for-language-actions.md)) applies independently at each level. An item-primary design may have:

- `format_file` / `format_python_file` (item-level, language-specific — handlers register here)
- `format_files` / `format_python_files` (collection-level, language-specific — orchestrates via delegation)

A collection-primary design may have:

- `lint_files` / `lint_python_files` (collection-level, language-specific — handlers register here)
- `lint_file` / `lint_python_file` (item-level, language-specific — delegates, optional)

## Consequences

- **Cleaner single-item contract.** Callers and handlers can work with a contract shaped around one item rather than adapting a batch-oriented payload and result model.
- **Batch optimization remains available.** Handlers that benefit from the full item list can still be exposed through collection-level actions or specialized subactions without forcing every invocation through a batch-oriented contract.
- **More actions to define.** Each granularity level is a separate action with its own payload, context, and result types. This is additional surface area, but each type is simpler and more focused.
- **Action designers must choose where the main handler logic lives.** The designing-actions guide documents criteria for this choice. Choosing incorrectly leads to awkward delegation patterns but is correctable without breaking handler contracts.

### Alternatives Considered

**Single collection-level action with framework-provided single-item adapter.** The framework would automatically unwrap single-element lists and wrap single results. Rejected because it hides the semantic difference between "one item" and "a batch of one" — the caller's intent and the handler's contract are genuinely different, and conflating them makes the API harder to reason about.

**Force all multi-item work to item-level, with framework-provided batching.** Rejected because batch-aware handlers (type checkers, cross-file analyzers) need the full item list for correctness or performance. Forcing per-item invocation would either degrade their results or require them to maintain external state across calls.

**Single collection-level design as an implementation approach.** These operations could be modeled only as collection-level actions, with single-item work represented as a batch of one. Rejected as the architectural rule because it makes the public contract batch-shaped even when the natural unit of work is one item, and because it pushes per-item handlers through an interface that does not match their semantics.

### Related Decisions

- Builds on [ADR-0008](0008-explicit-specialization-metadata-for-language-actions.md) (language subactions apply at both levels)
- Builds on [ADR-0009](0009-explicit-partial-result-token-propagation.md) (collection actions use partial results for per-item streaming)
- Builds on [ADR-0010](0010-progress-reporting-for-actions.md) (collection actions own per-item progress)
