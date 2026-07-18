# Observability & Telemetry

FineCode can export **traces, metrics, and logs** over [OpenTelemetry](https://opentelemetry.io/)
(OTLP) so you can see how actions run, how long they take, and where they fail — in your
own monitoring system. This is entirely optional: FineCode works fully without it.

Telemetry is most useful **occasionally** — when investigating a slow or failing action,
or when verifying that a new setup works — rather than as an always-on cost. Traces in
particular are the resource-intensive signal, so it is normal to enable a backend only
while you need it.

## Enabling telemetry

Point FineCode at an OTLP endpoint. Either set it in your workspace configuration:

```toml
[workspace.wm.telemetry]
otlp_endpoint = "http://localhost:4317"
```

…or via the `FINECODE_OTLP_ENDPOINT` environment variable, which takes precedence.

Notes on behavior:

- The endpoint is read **once when FineCode starts** — setting it is what turns telemetry
  on, and changing it takes effect on the next restart.
- The endpoint must include an explicit **host and port**; a malformed value fails fast
  at startup.
- Once set, it does **not** need to be reachable when FineCode starts. Exporters buffer
  and retry, so a backend you start later is picked up automatically without a restart.
- An unreachable endpoint produces a single startup heads-up, not a stream of errors.

## Running a local backend

You need something that speaks OTLP on the other end. Any OTLP-compatible backend works;
below are the common options, simplest first.

### Single container (recommended for a quick local setup)

Grafana's [`otel-lgtm`](https://github.com/grafana/docker-otel-lgtm) image bundles an
OTel Collector, Prometheus (metrics), Tempo (traces), Loki (logs), and a pre-wired
Grafana UI in one container:

```yaml
# docker-compose.observability.yml
services:
  otel-lgtm:
    image: grafana/otel-lgtm:latest
    ports:
      - "3000:3000"   # Grafana UI
      - "4317:4317"   # OTLP gRPC
      - "4318:4318"   # OTLP HTTP
```

```sh
docker compose -f docker-compose.observability.yml up -d
```

Then set `otlp_endpoint = "http://localhost:4317"` and open Grafana at
`http://localhost:3000`.

### Managed / hosted backends

Any OTLP endpoint works — Grafana Cloud, Honeycomb, Jaeger, Datadog, an existing
company collector, etc. Use the vendor's OTLP endpoint (and, for `https://` endpoints,
FineCode connects securely automatically). Consult the vendor for authentication
headers if required.

If traces are your main interest, [Jaeger](https://www.jaegertracing.io/) is worth
adding — its trace-focused UI presents FineCode's action/run spans more clearly than a
general-purpose dashboard. It accepts OTLP directly, so you can point `otlp_endpoint` at
it or fan traces out to it via a collector.

### Full self-hosted stack with WAL correlation

If you want a working example that also ships a customized collector config (WAL log
shipping via the `filelog` receiver, plus Tempo↔Loki correlation wiring), FineCode's own
repository contains one at
[`docker-compose.otel.yml`](https://github.com/finecode/finecode/blob/main/docker-compose.otel.yml)
that you can adapt. It runs `grafana/otel-lgtm` (with our own collector config mounted
over its default) plus a standalone Jaeger, rather than one container per component.

## Troubleshooting: stack is up, but no data appears anywhere

If traces/metrics/logs are missing everywhere (not just in one UI), and your backend's
own logs show no export errors at all, first re-check the two things "Enabling
telemetry" above calls out: is `otlp_endpoint`/`FINECODE_OTLP_ENDPOINT` actually set to a
reachable value, and has the WM been restarted since it was set (it's read once at
startup).

If both of those check out and there's still nothing, and you run your backend in a
container: **container runtimes only resolve `.env` files / `environment:` values when a
container is *created*, not on every restart.** Restarting an existing container (however
you normally do that — `docker restart`, your orchestrator's restart command, a devcontainer
"restart", etc.) reuses it as-is, including whatever environment it was created with. If
you *just* changed an env var and only restarted (rather than recreated) the container
holding it, the change is silently ignored — with no error anywhere to point at it. This
applies equally to wherever `FINECODE_OTLP_ENDPOINT`/`otlp_endpoint` is set (e.g. a
devcontainer) and to any env var your OTLP backend itself reads.

To confirm this is what happened, inspect the *running container's* actual environment
(not the image, and not the compose/config file — the live container):

```sh
docker ps --filter "name=<container-name>"
docker inspect <container-id> --format '{{json .Config.Env}}'
```

If the variable you just set is missing from that list, recreate the container rather
than restarting it — e.g. `docker compose up -d --force-recreate <service>`, or the
equivalent for however you manage that container/devcontainer.

If you're using FineCode's own `docker-compose.otel.yml` and devcontainer, see
[Local observability stack](developing-finecode.md#local-observability-stack) for the
exact recreate commands and an additional `grafana/otel-lgtm`-specific logging gotcha.

## Using a devcontainer for your own project

If you develop your project inside a **devcontainer**, that is a good place to make the
observability backend reproducible for your team. Rather than requiring everyone to run
extra commands, add the backend as an **opt-in** service so it stays off by default and
does not slow down container startup.

FineCode's repository does exactly this and is a useful reference:

- The observability services are gated behind a Docker Compose **profile**, so they stay
  down unless explicitly enabled.
- A single `.env` toggle (which Compose reads automatically) brings them up alongside the
  container — no manual pre-start step, which keeps the "open the folder and go" IDE
  experience intact.

See FineCode's [`docker-compose.otel.yml`](https://github.com/finecode/finecode/blob/main/docker-compose.otel.yml)
and `.devcontainer/` for the full pattern.
