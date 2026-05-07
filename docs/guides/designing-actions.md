# Designing Actions

This documentation is split into three parts so readers can choose the level of detail they need.

## Start here

- Read [Designing Your First Action](designing-actions-guide.md) if you are creating a FineCode action for the first time.
- Read [Designing Actions Reference](designing-actions-reference.md) when your action needs a more advanced pattern.
- Read [Designing Actions Rules](designing-actions-rules.md) if you need the normative constraints: the things an action design must or should do.

## Which page to use

Use the rules page when you are:

- sanity-checking an action design
- reviewing a PR
- looking for a stable, referenceable rule
- thinking about future automated validation

Use the first-action guide when you are:

- creating a new action from scratch
- adding the first handler for that action
- looking for the smallest complete example
- deciding what files and classes to create
- adding the first automated handler test

Use the reference page when you are:

- deciding between generic vs language-specific actions
- choosing item vs collection boundaries
- wiring discovery, bridge handlers, partial results, or progress
- choosing project scope vs workspace scope

Use [Testing Action Handlers](testing-handlers.md) when you are:

- writing automated tests for action handlers
- testing handler behavior without starting the Workspace Manager
- overriding services such as file editing in a test

## Suggested reading order

1. Use the first-action guide to build the simple version.
2. Open the reference page only for patterns your action actually needs.
3. Return to the rules before merging.

The detailed examples and follow-up documentation ideas are tracked in the repository plan note `docs/plans/designing-actions-docs-followups.md`.
