# ADR-0008: Explicit specialization metadata for language-specific actions

- **Status:** accepted
- **Date:** 2026-03-21
- **Deciders:** @Aksem
- **Tags:** actions, architecture, languages

## Context

After [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md),
FineCode treats shared action definition types as the architectural identity
contract. After [ADR-0007](0007-single-registration-per-action-definition.md),
projects also cannot model language variants by registering the same generic
action definition multiple times under different names.

Multi-language actions therefore need an explicit way to represent that one
action definition is a language-specific specialization of another. That
relationship must be discoverable without depending on project-local
registration names or naming conventions.

Name-based discovery is too fragile for that role. A project may register a
Python lint action under any project-local name, and the discovery mechanism
must still recognize both:

1. which language the specialized action targets
2. which parent action definition it specializes

Those are intrinsic properties of the action design, not of project
configuration.

## Related ADRs Considered

- [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md) —
  establishes action definition types as the identity contract. This ADR
  extends that rule to specialization relationships between action definitions.
- [ADR-0007](0007-single-registration-per-action-definition.md) —
  establishes that a project may register a given action definition at most
  once. This ADR defines how language-specific specialization is represented
  without relying on duplicate registrations.

## Decision

FineCode will represent language-specific specialization through **explicit
metadata on the shared action definition itself**.

A language-specific action definition must declare:

- the language it targets
- the parent action definition it specializes

This metadata is part of the shared action contract. It must not be inferred
from project-local registration names, and it must not live only in project
configuration.

Discovery and dispatch mechanisms may use that metadata to find specialized
actions, but routing behavior remains a separate concern owned by handlers or
other orchestration logic.

## Consequences

- **Discovery is independent of project-local names.** A project may choose any
  registration name for a specialized action without breaking discovery.
- **Specialization becomes part of the contract package.** The relationship
  between a specialized action and its parent is expressed alongside the action
  definition itself rather than being split across naming conventions or project
  configuration.
- **Language alone is not sufficient.** Multiple parent actions may have
  specializations for the same language, so discovery needs both the target
  language and the parent action definition.
- **Project configuration has a narrower role.** Configuration may control
  whether an action is registered or how it is executed, but it does not define
  the architectural specialization relationship.

### Alternatives Considered

**Inheritance from the parent action definition.** Rejected because the
specialization relationship does not always imply substitutability. Some
language-specific actions may need payload or contract differences that make
inheritance an unreliable architectural signal.

**Registration-time metadata in project config.** Rejected because it places an
intrinsic property of the shared action contract into project-local
configuration, where it can drift and where callers cannot rely on it across
environments.

**Name conventions.** Rejected because project-local registration names are not a
stable architectural contract.

## Related Decisions

- Refines [ADR-0006](0006-shared-action-types-as-the-action-identity-contract.md)
- Supports [ADR-0007](0007-single-registration-per-action-definition.md)

## Implementation Notes

The current implementation represents this metadata with class-level fields on
the `Action` base class for the target language and the parent action
definition.

Helpers that discover language-specific actions should use that explicit
metadata rather than parsing registration names.

When a specialized action extends its parent payload with ecosystem-specific
fields, dispatch-oriented construction should rely only on values available from
the parent payload. Any specialized fields therefore need defaults that are
meaningful for dispatch-based invocation, while workflows that need non-default
values should invoke the specialized action directly or use a more specific
orchestration path.
