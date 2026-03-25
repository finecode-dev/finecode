# ADR-0005: Zero-based line numbers and ResourceUri fields in action payloads and results

- **Status:** accepted
- **Date:** 2026-03-20
- **Deciders:** @Aksem
- **Tags:** actions, conventions

## Context

Action payload and result types that carry source-code locations must agree on
two conventions: how line numbers are represented and how resource locations
are represented.

**Line numbers.** LSP uses 0-based `Position.line` throughout. Test runner CLIs
and linters typically emit 1-based line numbers. A mismatch between the two
means that whoever builds the result (the handler) and whoever consumes it (e.g. the
LSP server) must each know which convention is in use.

**Resource locations.** Action payloads and results cross process boundaries
and should remain stable across languages and transports. Using runtime-specific
objects such as `pathlib.Path` in boundary DTOs leaks implementation details
into the contract. A semantic `ResourceUri` type, serialized as a URI string,
avoids this and leaves room for future non-local resources.

This decision applies to all actions that expose source-code locations in their
payload or result schema.

## Related ADRs Considered

None — no existing ADR covers payload/result field conventions for
source-code locations.

## Decision

- **Line numbers in payload and result fields are 0-based**, consistent with
  `Position.line` in the Language Server Protocol. Handlers that read 1-based
  line numbers from CLI output must subtract 1 before populating a field.
  Display code that shows lines to users must add 1.

- **Resource locations in payload and result fields use `ResourceUri`
  values**, not `pathlib.Path` objects. `ResourceUri` is serialized as a URI
  string. Local files use `file://` URIs; future non-local resources may use
  other URI schemes. This keeps the field type simple across transports,
  languages, and runtimes.

## Consequences

- **Handlers bear the conversion cost.** Handlers that consume 1-based CLI
  output subtract 1; they do not pass the raw value through. This is a one-line
  transformation and the correct place to isolate runner-specific quirks.
- **Display code adds 1.** Any code that renders a line number to a user
  (terminal output, hover text) must add 1 to recover the 1-based number a
  developer expects to see.
- **No Path objects in payload or result fields.** Producers populate location
  fields with `ResourceUri` values serialized as URI strings. Consumers that
  need a local `Path` may derive one only when the URI scheme is `file`.
- **Future-proof resource model.** The contract is not limited to local
  filesystem paths, so future handlers can report locations for non-file
  resources without redefining the result schema.
- **Consistency across actions.** `LintMessage.range`, `TestCaseResult.line`,
  and `TestItem.line` all follow the same source-location convention, and the
  same rule applies to any future action payload or result type that carries
  source-code locations rather than requiring per-action documentation.
