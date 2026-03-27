# ADR-0006: Shared action types as the action identity contract

- **Status:** accepted
- **Date:** 2026-03-21
- **Deciders:** @Aksem
- **Tags:** actions, api, architecture, environments

## Context

Handlers may discover and invoke other actions at runtime through `IActionRunner`.
That raises a contract question: how should one action identify another across
extension environments?

Two architectural directions were considered:

- **Type-based identification.** Actions are identified by their shared action
  definition type. Callers depend on the same lightweight definition package and
  use that type when requesting an action.
- **String-based identification.** Actions are identified by a string key that can
  be resolved even when the action definition type is not installed in the caller's
  environment.

The main force behind the string-based option is cross-environment invocation: one
environment may want to call an action implemented elsewhere. If action definitions
are not available everywhere, a string identifier appears to decouple callers from
those packages.

However, introducing string identity shifts the architecture toward looser and less
verifiable contracts. It makes action lookup depend on convention rather than shared
types, and it weakens refactor safety and discoverability.

This decision therefore depends on a broader packaging rule: whether FineCode treats
action definitions as lightweight shared contracts that can be installed in every
participating environment, or as implementation-bound artifacts that may be absent
from some environments.

## Related ADRs Considered

None — no existing ADR covers the `IActionRunner` API design.

## Decision

FineCode will use **type-based action identification**. Actions are identified
through their shared action definition types, not through string-only
identifiers.

This decision establishes the following architectural rule:

- **Action definition packages are shared contract packages.** They must remain
  lightweight, dependency-safe, and installable in any environment that needs to
  refer to those actions.
- **Action implementations belong elsewhere.** Heavy runtime dependencies,
  platform-specific integrations, and tool execution logic belong in handler or
  environment-specific packages, not in the shared action-definition package.

With that boundary in place, cross-environment action invocation still works because
all participating environments can depend on the same shared action-definition
package. A string-based lookup mechanism is therefore not part of the primary
architecture.

## Consequences

- **Action identity is explicit and type-safe.** Callers and runners share the same
  contract type, which improves readability, refactor safety, and tooling support.
- **Package boundaries matter more.** Action-definition packages must be kept small
  and free of heavy implementation dependencies. Extensions that define new actions
  are expected to separate contract types from execution logic.
- **Cross-environment compatibility becomes a packaging concern, not a lookup concern.**
  Environments that collaborate on actions must share the relevant contract package.
- **String-only lookup is deferred, not rejected.** If FineCode later needs to invoke
  actions whose definitions genuinely cannot be installed in the calling environment,
  the architecture should be revisited and an additional identifier mechanism may be
  introduced at that time.
