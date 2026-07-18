# FineCode DevContainer

This directory contains the DevContainer configuration for developing FineCode in a reproducible environment.

- `devcontainer.json`: Main configuration file for VS Code DevContainers.
- `docker-compose.devcontainer.yml`: Compose service definition for the main workspace container.

## Local observability stack (opt-in)

The devcontainer includes the repository-level `docker-compose.otel.yml`, but all of
its services (OTel Collector, Jaeger, Tempo, Prometheus, Loki, Grafana) are gated
behind the `otel` Compose profile and stay **down by default** — the devcontainer
starts lightweight (workspace).

Bring the stack up either way:

- **Persistent** — set `COMPOSE_PROFILES=otel` in `.env` (see `.env.example`), then
  (re)open the devcontainer. Docker Compose reads `.env` automatically, so no manual
  pre-start or CLI flags are needed.
- **On demand** — run `scripts/observability.sh up` on the host (the devcontainer does
  not mount the Docker socket); `scripts/observability.sh down|status` to manage it.

WAL events are recorded on disk regardless of whether the stack is running, so you can
bring it up later and ingest the history retroactively. See
[ADR-0052](../../finecode_internal_docs/adr/0052-observability-stack-opt-in-via-compose-profile.md).

## Optional private internal docs mount

The workspace service supports an optional bind mount for private internal docs.

- Container target path: `/workspaces/internal-docs`
- Host source path: `${FINECODE_INTERNAL_DOCS_PATH}`
- Fallback when unset: `./.devcontainer/empty-internal-docs`

This means developers without private docs access can still start the devcontainer successfully.

The mount is writable from inside the container so you can edit docs directly.

If you have private docs locally, set `FINECODE_INTERNAL_DOCS_PATH` in your shell or a local `.env` file before opening the devcontainer.
