# Architecture Decision Records

This directory captures architecturally significant decisions for FineCode
using the [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records)
format — a lightweight, structured template that scales from simple to complex
decisions.

## What is an ADR?

An Architecture Decision Record (ADR) is a short document that captures a
single decision along with its context and consequences. ADRs are numbered
sequentially, and once accepted they are **immutable** — if a decision changes,
a new ADR supersedes the old one rather than editing it.

## How to create a new ADR

1. **Search first.** Look through the index below (filter by tags) and search
   the `docs/adr/` directory for related decisions. Fill in the "Related ADRs
   Considered" section with what you found — even if the answer is "None".
2. Copy [template.md](template.md) to `NNNN-short-title.md` (next sequential number).
   Use a title that states the decision, not just the topic —
   "Auto-shutdown on disconnect timeout" rather than "WM server lifecycle".
3. Fill in the required sections (Context, Related ADRs Considered, Decision, Consequences).
4. Set status to `proposed` and open a PR for review.
5. Once merged, update status to `accepted` and add a row to the index table below.

## How to write a good ADR

ADRs should capture the **durable architectural decision**, not the current
implementation shape. A useful test is: if we refactor the API next month but
keep the same decision, would the ADR still read correctly?

- Write the decision at the level of architecture, contracts, boundaries,
  ownership, compatibility, or policy.
- Prefer titles that describe the enduring rule, not the current mechanism.
- Record the forces and constraints that make the decision necessary, not just
  the selected solution.
- Use one primary term consistently throughout the ADR. If a language-specific
  implementation term is helpful, introduce it once as an example rather than
  switching terms back and forth.
- Keep method names, function signatures, field layouts, and matching logic out
  of the main narrative unless they are themselves the architectural decision.
- Put narrow implementation details in `Implementation Notes` only when they are
  needed to avoid ambiguity.
- If an alternative is deferred rather than rejected, say when the decision
  should be revisited and what condition would trigger that review.
- Keep examples short. Prefer explaining the rule and trade-off over documenting
  the present code structure.

## Index

| #    | Title                                                                                                                          | Status   | Date       | Tags                           |
|------|--------------------------------------------------------------------------------------------------------------------------------|----------|------------|--------------------------------|
| 0001 | [Use ADRs for architecture decisions](0001-use-adr.md)                                                                         | accepted | 2026-03-19 | meta                           |
| 0002 | [Port-file discovery for the WM server](0002-port-file-discovery-for-wm-server.md)                                             | accepted | 2026-03-19 | ipc, wm-server                 |
| 0003 | [One Extension Runner process per execution environment](0003-process-isolation-per-extension-environment.md)                  | accepted | 2026-03-19 | architecture, extension-runner |
| 0004 | [Auto-shutdown on disconnect timeout](0004-auto-shutdown-on-disconnect-timeout.md)                                             | accepted | 2026-03-19 | lifecycle, wm-server           |
| 0005 | [Zero-based line numbers and ResourceUri fields in action payloads and results](0005-zero-based-lines-and-resourceuri-fields-in-action-payloads-and-results.md) | accepted | 2026-03-20 | actions, conventions           |
| 0006 | [Shared action types as the action identity contract](0006-shared-action-types-as-the-action-identity-contract.md)            | accepted | 2026-03-21 | actions, api, architecture, environments     |
| 0007 | [Single registration per action definition](0007-single-registration-per-action-definition.md)                                | accepted | 2026-03-21 | actions, architecture          |
| 0008 | [Explicit specialization metadata for language-specific actions](0008-explicit-specialization-metadata-for-language-actions.md) | accepted | 2026-03-21 | actions, architecture, languages |
| 0009 | [Explicit handler mediation of partial results across action boundaries](0009-explicit-partial-result-token-propagation.md)    | accepted | 2026-03-24 | actions, partial-results, architecture |
| 0010 | [Explicit action-scoped progress reporting](0010-progress-reporting-for-actions.md)                                            | accepted | 2026-03-27 | actions, progress, architecture |
| 0011 | [WM aggregates progress for multi-execution requests](0011-wm-aggregates-progress-across-multi-project-action-runs.md)       | accepted | 2026-03-28 | actions, progress, architecture, wm-server |
