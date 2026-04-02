# ADR-0014: CLI streams partial results in completion order

- **Status:** accepted
- **Date:** 2026-03-31
- **Deciders:** @Aksem
- **Tags:** cli, partial-results, ux

## Context

When a CLI `run` command targets multiple projects, some projects may finish
well before others. If the CLI waits until the entire multi-project request has
finished before printing results, the user sees nothing actionable until the
slowest project completes.

There are two properties in tension:

1. **Grouping** — each project's output should appear as a contiguous block, not
   interleaved with output from other projects.
2. **Liveness** — the user benefits from seeing results as soon as they are
   available, not after the slowest project finishes.

Waiting for the final response satisfies (1) perfectly but sacrifices (2). The opposite extreme —
interleaving partial output lines from multiple concurrent projects — satisfies
(2) but violates (1) and makes output unparseable.

### Alternatives considered

| Approach | Grouping | Liveness | Non-TTY safe | Complexity |
|---|---|---|---|---|
| Wait for full request, print all at end | ✓ | ✗ | ✓ | none |
| Stream in completion order | ✓ | ✓ | ✓ | low |
| Partial lines → clear screen → final grouped | ✓ | ✓ | ✗ | high |
| Multi-line per-project status bar | ✓ | ✓ | ✗ | high |
| stderr/stdout split (live stderr, sorted stdout) | partial | ✓ | ✓ | medium |

**Stream in completion order**: once a project's result is fully available, print
its complete block immediately. Because each block is printed atomically as one
unit, grouping is preserved even when multiple projects are running
concurrently. Non-TTY output (pipes, CI logs) remains grouped and readable, but
block order across runs may differ depending on which project finishes first.
Within a single run, output order reflects observed completion order.

**Clear-and-redraw**: show partial per-project lines while running, then clear
them and reprint a sorted final result. Gives the best TTY experience but breaks
in non-TTY contexts (pipes, CI) and requires complex ANSI cursor management.

**stderr/stdout split**: print live per-project results to stderr in completion
order while printing a deterministically-ordered final summary to stdout. Keeps
stdout parseable and gives live feedback. Added complexity of managing two output
streams and reconciling the two representations was not justified at this stage.

## Related ADRs Considered

- [ADR-0009](0009-explicit-partial-result-token-propagation.md) — defines how
  partial result tokens are propagated from WM client through to the server. This
  ADR uses that mechanism so the CLI can receive per-project results incrementally.
- [ADR-0010](0010-progress-reporting-for-actions.md) — progress notifications are
  already handled in the CLI. This ADR extends live output to include result
  content, not just progress messages.
- [ADR-0011](0011-wm-aggregates-progress-across-multi-project-action-runs.md) —
  the WM aggregates progress across projects; partial results arrive per-project
  and are not aggregated.

## Decision

For multi-project runs, the CLI prints each project's complete result block as
soon as it becomes available, in completion order. Blocks are emitted atomically
so per-project output remains grouped and non-interleaved.

This behavior is delivered through the existing partial-result mechanism on the
multi-project execution path. The terminal response is used only to determine
the overall exit code.

Single-project runs are unaffected — there is no liveness benefit and the simpler
non-streaming path can be retained.

Output ordering is **completion order**, not config order. This is an accepted
trade-off: the user sees results sooner, ordering is stable within a run, and the
slight non-determinism across runs is not material for lint or test output.

## Consequences

**Easier:**
- Users running lint or tests across many projects get actionable feedback before
  the slowest project finishes.
- Works identically in TTY and non-TTY (piped/CI) environments.
- No ANSI cursor management or terminal capability detection required.

**Harder:**
- The overall exit code is only known when the request completes; it cannot be
  derived from partial results alone.
- If scripting tools depend on a fixed project ordering in stdout, they will need
  to handle variable ordering. (Deterministic ordering can be added later via a
  `--sort-output` flag if requested. Revisit when repeated user demand appears or
  CI/scripting incidents are attributed to cross-run ordering variance.)
- The CLI must handle the case where a run targets a single project and fall back
  gracefully without duplicating output.
