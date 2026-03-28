# ADR-0007: Single registration per action definition

- **Status:** accepted
- **Date:** 2026-03-21
- **Deciders:** @Aksem
- **Tags:** actions, architecture, languages

## Context

FineCode actions are architectural contracts that callers discover and invoke
across projects and environments. If a project can register the same action
definition more than once under different project-local names, callers must know
those registration details to select the intended action. That makes action
identity depend on configuration rather than on the shared contract itself.

This is especially problematic after [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md),
which establishes shared action definition types as the identity contract. Allowing
multiple registrations of the same action definition would make that lookup
ambiguous at the project level.

Multiple registrations also create avoidable routing work for file-oriented
actions. If the same generic action definition is registered once per language,
each registration or handler must independently decide which subset of files it
should process. That pushes grouping and dispatch concerns into repeated
project-local registrations instead of making them an explicit part of the
action architecture.

Multi-language actions still need a way to specialize behavior by language, but
that need should be addressed through explicit composition or specialization
mechanisms, not by registering the same action definition multiple times.

## Related ADRs Considered

- [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md) —
  establishes action definition types as the identity contract. This ADR extends
  that rule from action lookup to project registration.

## Decision

FineCode permits **at most one registration of a given action definition within a
project**.

A project-local registration name or configuration may vary, but it does not
create a new action identity and does not justify additional registrations of the
same action definition.

Variation in tool behavior, language specialization, or environment-specific
execution belongs in handlers, specialized action definitions, or other explicit
composition mechanisms, not in repeated registrations of the same action
definition.

For multi-language file-oriented actions, the architecture must use some
coordination mechanism other than "register the same generic action definition
once per language".

## Consequences

- **Stable cross-project action identity.** Any caller can reference an action by
  its shared definition, regardless of which project or preset is active. This
  keeps action lookup APIs based on action definitions unambiguous.
- **Project configuration cannot model language variants as duplicate registrations.**
  Multi-language behavior must be represented through explicit action
  specialization or routing design.
- **Routing can be centralized instead of repeated.** When a dispatch-style
  implementation is used, file grouping and delegation can happen once rather than
  being reimplemented in each registration or handler.
- **Some designs become more explicit.** Projects that support multiple ecosystems
  may need additional specialized action definitions or orchestration handlers, but
  the boundary between action identity and execution strategy stays clearer.

### Alternatives Considered

**Multiple registrations of the same action definition under different names.**
Rejected because it makes action selection depend on project-local conventions,
creates ambiguity for action-definition-based lookup, and encourages routing logic
to be expressed as duplicate registrations instead of as explicit architecture.

## Related Decisions

- Refines [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md)

## Implementation Notes

One supported implementation for multi-language file actions is a dispatch
handler that invokes language-specific actions. That is an implementation pattern,
not the architectural rule recorded by this ADR.
