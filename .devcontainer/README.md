# FineCode DevContainer

This directory contains the DevContainer configuration for developing FineCode in a reproducible environment.

- `devcontainer.json`: Main configuration file for VS Code DevContainers.
- `docker-compose.devcontainer.yml`: Compose service definition for the main workspace container.

## Node.js

Node.js 22 is a runtime dependency of `setup_system` handlers that install
npm-distributed tools (e.g. pi coding agent, which rejects Node older than
22.19.0). The base image already ships `nvm` itself (no Node version installed), so
`setup-node.sh` installs Node 22 through that pre-existing `nvm` in
`postCreateCommand`. Because Node is installed via nvm under
the `vscode` user, `npm install -g` works without sudo.

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

## Persistent WM server

`FINECODE_WM_AUTOSTART=1` is set in `.env.example`, so `postStartCommand` runs
`start-wm-server --detach --keep-alive` on every container start, via
`start-wm-server.sh`. The workspace stays warm across commands instead of rebuilding
its config and runners each time. Comment the variable out to go back to a server
per client; the script is also a no-op when the `dev_workspace` venv does not exist
yet.

Changing the variable needs the container **recreated**, not reopened — Compose
resolves `.env` into a container's environment only when that container is created
(the same caveat as `FINECODE_OTLP_ENDPOINT` above; see [Developing
FineCode](../docs/guides/developing-finecode.md#local-observability-stack) for the
rebuild commands).

What comes with a keep-alive server — resident extension runners, a fixed log level,
and why a server started lazily after a crash is not one — is described under
[`start-wm-server`](../docs/cli.md#start-wm-server). Re-run
`sh .devcontainer/start-wm-server.sh` to get the persistent one back.

## Profiling the WM with py-spy

When the WM looks stalled, `py-spy` shows what its threads are doing without
restarting it or changing its code. It lives in the root `dev_workspace` group, so
it is at `.venvs/dev_workspace/bin/py-spy` after `prepare-envs --env=dev_workspace`
(or `scripts/setup-dev-workspace.sh`).

Use `sudo`: Yama's `ptrace_scope=1` lets a process attach only to its own
children, and the `vscode` user has no effective caps even with `SYS_PTRACE` in the
container's bound set. `sudo` also resets `PATH`, so invoke the binary by path from
the repo root.

```bash
# find the pid — a dedicated WM, or the shared keep-alive one
pgrep -f 'start-wm-server --port-file'
pgrep -f 'start-wm-server.*--keep-alive'

# one stack dump per thread
sudo .venvs/dev_workspace/bin/py-spy dump --pid "$PID"

# a 90-second flame graph
sudo .venvs/dev_workspace/bin/py-spy record --pid "$PID" --duration 90 --output wm-profile.svg
```

`--subprocesses` is deliberately left off: it would fold every Extension Runner into
the WM's flame graph. Profile an ER separately by pointing `--pid` at it.

## Optional private internal docs mount

The workspace service supports an optional bind mount for private internal docs.

- Container target path: `/workspaces/internal-docs`
- Host source path: `${FINECODE_INTERNAL_DOCS_PATH}`
- Fallback when unset: `./.devcontainer/empty-internal-docs`

This means developers without private docs access can still start the devcontainer successfully.

The mount is writable from inside the container so you can edit docs directly.

If you have private docs locally, set `FINECODE_INTERNAL_DOCS_PATH` in your shell or a local `.env` file before opening the devcontainer.
