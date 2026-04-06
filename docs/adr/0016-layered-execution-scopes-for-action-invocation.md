# ADR-0016: Layered execution scopes for action invocation

- **Status:** accepted
- **Date:** 2026-04-05
- **Deciders:** @Aksem
- **Tags:** actions, architecture, orchestration, extension-runner, wm-server

## Context

Action invocation in FineCode occurs across architectural components and
multiple execution scopes. In particular, FineCode distinguishes the Workspace
Manager (WM) from Extension Runners (ERs), and invocation can cross those
boundaries:

- one action may invoke another within a single ER runtime boundary
- one project-scoped request may require coordination across multiple execution
  environments and runners
- one workspace-scoped request may fan out across multiple projects under WM
  coordination

These scopes have different topology visibility, ownership boundaries, and
operational controls.

If one contract tries to cover all of them, local invocation semantics become
coupled to orchestration concerns that they do not own. That makes the system
harder to evolve and reason about because:

- local nested action invocation and cross-boundary orchestration have
  different guarantees
- recursion and fan-out control apply to orchestration scopes, not local scope
- WM-managed project/workspace topology should not leak into local ER
  execution contracts

FineCode needs an explicit architectural rule that distinguishes execution
scopes while preserving generic, action-agnostic orchestration.

## Related ADRs Considered

- Reviewed [ADR-0003](0003-process-isolation-per-extension-environment.md) -
  defines environment process isolation; this ADR defines invocation scope
  boundaries across those processes.
- Reviewed [ADR-0011](0011-wm-aggregates-progress-across-multi-project-action-runs.md) -
  defines WM ownership for aggregated progress in fan-out requests; this ADR
  defines execution interface layering for such fan-out.
- Reviewed [ADR-0013](0013-action-declares-handler-execution-strategy.md) -
  defines intra-action handler relationship; this ADR defines cross-boundary
  invocation scope and ownership.

## Decision

FineCode adopts a layered execution model by scope:

- Local execution scope: invocation within a single ER runtime boundary.
- Project execution scope: invocation for one project under a project-scoped
  contract.
- Workspace execution scope: invocation across multiple projects under a
  workspace-scoped contract.

### Architectural boundaries

1. Local execution contracts remain local and do not carry project/workspace
   topology concerns.
2. Cross-boundary orchestration is represented by separate higher-scope
   contracts, not by widening local contracts.
3. Project and workspace orchestration remain generic and action-agnostic, and
   are owned by WM-level execution capabilities rather than ER-local contracts.
4. Coordination that depends on shared resources across projects must model
   those resources explicitly rather than relying on implicit shared state.

### Ownership rule

- ER-local components own local action invocation semantics.
- WM-level orchestration owns cross-runner and cross-project coordination
  mechanics.
- Action-specific orchestration policy belongs to the orchestrating
  action/service, not to generic orchestration infrastructure.

## Consequences

- One scope, one contract. Callers can choose local, project, or workspace
  execution contracts explicitly.
- Future-safe evolution. Single-project execution can evolve from local to
  orchestrated without redefining local contracts.
- No action-specific WM coupling. WM remains a generic execution mechanism for
  project/workspace fan-out.
- Additional interface surface. Introducing layered contracts increases
  architectural surface area and requires clear naming/documentation.
- Operational safeguards required. Orchestration scopes require recursion and
  fan-out controls.

### Alternatives Considered

Directly using one unified action-runner interface across all scopes. Rejected
because it conflates local and orchestration concerns, reducing contract clarity
and increasing accidental coupling.

Extending only the local runner interface to cover project/workspace
orchestration. Rejected because topology-aware behavior is not a local concern
and would blur ownership boundaries.

Keeping orchestration implicit behind internal mechanisms without
scope-specific contracts. Rejected because orchestration would remain implicit
and difficult to reuse consistently from callers that need it.

### Related Decisions

- Complements [ADR-0003](0003-process-isolation-per-extension-environment.md)
  by clarifying invocation semantics across isolated runner processes.
- Refines [ADR-0011](0011-wm-aggregates-progress-across-multi-project-action-runs.md)
  by defining broader execution-scope layering.
