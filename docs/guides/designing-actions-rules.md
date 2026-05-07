# Designing Actions Rules

This page is the normative layer for action design. Each rule is intentionally short, numbered, and phrased so it can be referenced in reviews and gradually turned into validation checks.

Use `MUST` for hard constraints and `SHOULD` for strong defaults.

Rule IDs use reserved numeric ranges by group so we can extend one area later without renumbering the others:

- `R-100` to `R-199`: Action contract
- `R-200` to `R-299`: Inputs and run context
- `R-300` to `R-399`: Execution model
- `R-400` to `R-499`: Naming and documentation

## Action Contract

## R-101: Generic actions must stay inter-language

Generic action payloads and results MUST contain only concepts that make sense across ecosystems.

Language- or ecosystem-specific runtime parameters belong in a language-specific subaction, not in the generic action contract.

## R-102: Do not force one tool's workflow into the action contract

An action SHOULD accommodate multiple valid workflows in the target problem space instead of encoding one tool's model as the only supported shape.

If two workflows need genuinely different payloads or semantics, define separate actions.

## R-103: Prefer focused actions over overloaded actions

An action MUST NOT accumulate optional fields that only apply to some handlers, tools, or workflows unless those fields are part of the action's actual contract.

If handlers ignore large parts of the payload or only some field combinations are valid, the action boundary is probably wrong.

## R-104: Ecosystem-wide runtime parameters belong in language-specific subactions

When a parameter is ecosystem-specific but tool-independent, it MUST live on a language-specific subaction rather than in handler config.

Tool-specific choices belong in handler config.

## R-105: Language-specific subactions must declare specialization metadata

A language-specific subaction MUST declare both `LANGUAGE` and `PARENT_ACTION`.

Using only one of them is not enough for reliable dispatch.

## R-106: Extended subaction payload fields must be dispatch-safe

Fields added by a language-specific subaction MUST have defaults that make sense when the subaction is constructed from the parent payload during dispatch.

`None` meaning "auto-detect from project config or environment" is the standard default.

## R-107: Language-specific subactions are required for handler routing, not only for payload fields

A language-specific subaction MUST be created whenever handlers are language-specific — even if the generic action's payload has no extra language-specific fields.

Without a subaction boundary, a dispatch handler has no routing target: all registered handlers would run for every file regardless of language, which is both incorrect and inefficient.

The absence of extra payload fields on the subaction is NOT a reason to omit it. Routing and payload extension are independent concerns. A subaction with no additional payload fields beyond its parent is a valid and common design.

## R-108: Action declares its execution scope

An action MUST declare `SCOPE = ActionScope.WORKSPACE` when it needs to reason about all workspace projects together (e.g. de-duplicating results across projects, or orchestrating per-project sub-actions from a single entry point). All other actions use the default `ActionScope.PROJECT`.

A workspace-scoped action runs exactly once per invocation, hosted in the workspace root project. Its handler is responsible for any per-project fan-out. See [ADR-0035](../adr/0035-action-declares-execution-scope-project-or-workspace.md).

## R-109: Workspace handlers must not perform per-project data collection directly

When a workspace handler needs structured data from individual projects, it MUST
obtain that data by calling a project-scoped action via `IWorkspaceActionRunner`.
It MUST NOT directly read per-project files or invoke per-project APIs from
within the workspace handler.

Each project is responsible for exposing its own structured data through a
project-scoped action. The workspace handler's role is to fan out that action,
aggregate the results, and perform any cross-project reasoning (e.g.
cross-referencing which dependencies are local).

Direct per-project work in a workspace handler bypasses the project's action
layer: users cannot extend the collection with additional handlers per project,
and the collection logic cannot be reused by other callers (e.g. an incremental
update triggered by a single file change).

## Inputs And Run Context

## R-201: Runtime discovery and static defaults are different mechanisms

Inputs that depend on runtime state MUST use dynamic discovery.

Inputs that are stable project-level defaults SHOULD use `payload_defaults` in project configuration.

Do not encode runtime discovery as a static default.

## R-202: Discovery state must have explicit semantics

For discovery-driven fields, `None` MUST mean "system should discover" and an empty value MUST mean "discovery ran and found nothing" or "caller explicitly requested no items", whichever the action contract defines.

Those semantics MUST be documented on the payload or context field.

## R-203: The run context becomes the source of truth after initialization

When a value can be discovered or normalized into the run context, downstream handlers MUST read the run context rather than re-checking the payload.

This avoids split semantics and duplicate discovery logic.

## R-204: Custom run contexts must preserve framework-injected senders

If a `RunActionContext` subclass overrides `__init__`, it MUST accept and forward framework-injected sender parameters needed for progress and partial-result features.

If possible, prefer not overriding `__init__`.

## R-205: Mutable context state should be isolated in a dedicated serializable state dataclass

When a `RunActionContext` subclass holds mutable domain state that handlers read
or write, that state SHOULD be placed in a separate `@dataclass` declared as
`STATE_TYPE` on the context class. The framework serializes and restores this
dataclass across env boundaries automatically.

This rule applies regardless of whether the action currently uses one env or
several. The env layout can change — a future handler may be added in a different
env — and context state that is not serializable is silently lost at any env
boundary it crosses.

Non-serializable objects — DI-resolved services, framework helpers, async
resources — MUST remain as direct attributes on the context instance and MUST
NOT appear inside the state dataclass.

See [ADR-0025](../adr/0025-cross-env-context-state-dedicated-state-type.md).

## Execution Model

## R-301: Action boundaries must be explicit about item vs collection granularity

An action that processes multiple items MUST have a clear contract about whether it is item-level or collection-level.

Choose the level that matches the primary caller contract and handler behavior.

## R-302: Results are project-scoped

`RunActionResult.update()` MUST be designed for within-project merging only.

Cross-project aggregation belongs to callers above the action layer.

## R-303: Handler execution strategy is owned by the action

An action with multiple handlers MUST declare whether handlers interact sequentially or concurrently.

If one logical workflow seems to need both, split it into multiple actions and orchestrate them explicitly.

## R-304: Partial results and progress are separate contracts

Partial results MUST carry structured result data in the action's result shape.

Progress MUST carry execution status, not result data.

Parent actions that delegate to child actions MUST explicitly re-emit any partial results or progress narrative they want callers to observe.

## R-305: Bridge handlers must translate contracts explicitly

When a handler on Action A calls Action B, it MUST map payload, context, and results between the two contracts intentionally.

Do not rely on implicit propagation across action boundaries.

## R-306: Concurrent handler results must carry domain-model data, not wire-format data

When an action uses `HANDLER_EXECUTION = CONCURRENT`, each handler contributes an independent partial result that is merged via `RunActionResult.update()`. The result type MUST represent data in a domain-model form (absolute positions, business objects, plain values) so that independent contributions can be correctly merged.

Pre-encoded or delta-encoded wire formats MUST NOT be used as result fields in concurrent actions. Appending two independently delta-encoded arrays is not equivalent to delta-encoding the union — the second array's deltas are relative to its own baseline, not to the end of the first array, so the concatenated result is incorrect.

Any wire-format transformation (delta encoding, compression, protocol serialization) MUST happen at the endpoint or caller boundary after merging, not inside the action result.

## Naming And Documentation

## R-401: Naming must be self-describing at the flat action list level

Action names MUST be understandable without module context because callers see a flat action surface.

Handler class names SHOULD include a qualifier that distinguishes them from the action class and clarifies the handler's role.

## R-402: Actions and fields must be documented for runtime consumers

Every action SHOULD set a `DESCRIPTION` class attribute — a short, caller-facing string exposed to MCP clients as `Tool.description`.

The class docstring is reserved for developer-facing documentation: contracts, invariants, scope notes, and other information useful to handler authors but not to MCP callers.

Payload fields with non-obvious semantics, defaults, or valid combinations SHOULD have attribute docstrings so their schema descriptions reach MCP consumers.

## Handler Observability

Reserved range: `R-500` to `R-599`.

## R-501: External process exit codes must be handled explicitly

When a handler spawns an external tool, it MUST check the process exit code and document the semantics of each relevant code for that tool. Relying solely on the absence of an expected output file as the error indicator is insufficient — files can be missing for reasons unrelated to the exit code.

Normal and error codes differ per tool. Document which codes are tolerated and why (e.g. "pytest exit codes 0/1/5 are expected outcomes; 2/3/4 indicate abnormal termination") so future maintainers understand the design intent.

## R-502: External process output should be logged at DEBUG

A handler that spawns an external process SHOULD log the exit code, stdout, and stderr at `DEBUG` level. The result summary (counts, key metrics from the tool's report) SHOULD also be logged at `DEBUG`. This makes failures diagnosable without modifying handler code.

## R-503: Unexpectedly empty results must trigger a WARNING

When a handler's primary job is to produce results (test results, lint diagnostics, build outputs) but returns nothing, it MUST emit a `WARNING` that includes enough context to diagnose the cause: the command run, the exit code, and any summary the tool produced.

## R-504: Do not repeat payload logging in handlers

Handlers MUST NOT log the incoming action payload. The framework already logs the full payload at `TRACE` level automatically (enable it via `"finecode_extension_runner" = "TRACE"` in the ER log groups config). Duplicate payload logs create noise and make relevant information harder to find.

Log tool-specific context instead: exit codes, process output, and result summaries — things the framework cannot know.

## Review checklist

Before merging a new action, verify:

1. The generic action contract is still inter-language.
2. Any ecosystem-specific runtime fields live on a language-specific subaction.
3. Discovery, defaults, and context ownership are unambiguous.
4. Item vs collection granularity is intentional.
5. Handler execution strategy is explicit.
6. Mutable context state that handlers share is isolated in a `STATE_TYPE` dataclass containing only serializable values.
7. Partial results, progress, and bridge boundaries are not being mixed together.
8. `DESCRIPTION` is set and clear to a caller who only sees the flat action surface. Docstrings carry contracts and other developer-facing notes.
9. Handlers that spawn external processes log exit code, stdout/stderr, and result summary at DEBUG; warn on unexpectedly empty results (R-501 to R-503).
10. If the action is workspace-scoped: per-project data collection is delegated to a project-scoped action via `IWorkspaceActionRunner`, not performed directly in the workspace handler (R-109).
