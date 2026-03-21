# ADR-0001: Use ADRs for architecture decisions

- **Status:** accepted
- **Date:** 2026-03-19
- **Deciders:** @Aksem
- **Tags:** meta

## Context

FineCode has several important architectural decisions that
are currently documented implicitly across code, comments, and CLAUDE.md. When
new contributors or AI agents work on the codebase, they lack visibility into
*why* decisions were made, what alternatives were considered, and what
constraints must be preserved.

As the project grows and automated testing is introduced, we need a lightweight
way to record decisions so they can be referenced, reviewed, and superseded
over time.

## Related ADRs Considered

None — this is the first ADR.

## Decision

We will use Architecture Decision Records stored in `docs/adr/` following a
simplified [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records)
template. The required sections are Context, Related ADRs Considered, Decision,
and Consequences. Each ADR is a sequentially numbered Markdown file.

The template also documents optional sections (Alternatives Considered, Risks,
Related Decisions, References, Implementation Notes, Review Date) that can be
added when they provide value, but are not required.

ADRs are immutable once accepted. Changed decisions produce a new ADR that
supersedes the previous one.

## Consequences

- Every architecturally significant decision gets a permanent, discoverable
  record with its rationale.
- New contributors and AI agents can understand *why* the codebase is shaped
  the way it is.
- Slightly more process overhead per decision — mitigated by keeping the
  template minimal.
- Existing implicit decisions can be backfilled as ADRs when they become
  relevant.
