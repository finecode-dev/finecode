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

## Signal coverage

With telemetry on, each process records the same five evidence kinds. Every
record carries the process identity as resource attributes — `service.name`,
`project.path` / `workspace.path`, and `finecode.process.incarnation` (a fresh
UUID hex per process start, shared by spans and health summaries).

**Hop spans** — one caller span (`jsonrpc.client/{method}`) and one receiver
span (`jsonrpc.server/{method}`) per WM↔ER request, in both directions. Stage
events named `rpc.stage.<stage>` mark the real transport/dispatch/handler
boundaries (submitted, enqueued, dequeued, write_returned, drain_started,
drain_returned, frame_read, dispatched, handler_started, handler_ended,
response_resolved, caller_completed); each event carries the wall-clock
timestamp plus `finecode.mono_ns` and `finecode.stage.status`. Span attributes
name the connection (`finecode.rpc.connection_id`, fresh per connection
incarnation), the direction (`finecode.rpc.role`: `caller` or `receiver`), the
typed request id (`finecode.rpc.request_id` plus `request_id_type`), the
generation (distinguishes a replacement request reusing an id), the receiver's
causal link (`finecode.rpc.causal_link`: `propagated` or `unavailable`), and
the separate outcomes (`finecode.rpc.caller_outcome`,
`finecode.rpc.handler_outcome`, `finecode.rpc.close_reason`). Absent stages
mean that step never ran for the hop; a stage with status `unsupported` ran in
a transport that cannot observe it (stdio has no drain step).

**Late-stage logs** — stages or responses observed after their hop closed are
never edited into the span. They become bounded `INFO` logs under the group
`finecode_extension_runner.shared_telemetry.rpc_hops`, correlated by
connection, role, typed request id and generation, with `rpc_correlation`
`matched` (the trace id is restored for that emission only) or `unavailable`.

**Lag logs** — the WM's event-loop monitor logs under
`finecode.wm_server.services.event_loop_lag_monitor`, each endpoint-enabled
ER's monitor under `finecode_extension_runner.er_lag_monitor`. One `WARNING`
per episode per 30 s cooldown, one `INFO` recovery summary when the episode
ends (or an interruption summary if shutdown lands mid-episode). Every value
is in the message text and in the extras: lag and threshold, the process CPU
window (process-wide, all threads), host load, host pressure, pid, project
and env.

**Phase spans** — one span per synchronous phase, named `phase/<label>`, with
elapsed and thread-CPU time as separate attributes
(`finecode.phase.elapsed_ns`, `finecode.phase.thread_cpu_ns` with an
unavailable reason when the clock cannot answer), the outcome, the completion
counts availability, and one `finecode.phase.count.<name>` attribute per
count. Thread CPU covers only the calling thread; worker threads and
subprocesses are unattributed elapsed time.

**Health summaries** — see below.

## Diagnostic profile

The supported way to collect all of the above is the diagnostic profile:

- the OTLP endpoint is set (at WM start; ERs inherit it through `initialize`),
- `OTEL_TRACES_SAMPLER` is unset (`parentbased_always_on`),
- the effective log level is `INFO`,
- the collector runs all three pipelines: traces, metrics and logs.

There is no FineCode sampler or API key: sampling is the SDK's, authentication
is the endpoint's. The profile does not promise lossless delivery — a sampled
span is dropped silently (and counted as S3), a full queue or a failed export
drops records (counted as S5/S6), and anything the collector drops after a
successful export is invisible to FineCode. What the profile does promise is
that every loss before the export is counted and reported in the health
summary.

## Telemetry health summary

One loguru record per process, under the log group
`finecode_extension_runner.shared_telemetry.health`, with the message
`telemetry health summary` plus a human-readable digest. The level is `INFO`
when no loss was recorded and `WARNING` when any loss count or admission flag
is set; the as-written level is whatever the group's configured level lets
through. The first summary is written when the setup attempt completes (even
when every signal is unavailable); afterwards a summary is written only when
the counts, flags, availability or ER-setup categories changed, at most one
per 60 seconds. A level change alone never triggers a summary.

The structured extras are flat scalars (`health_<signal>_<field>`,
`health_sampler`, `health_er_setup_<category>`, `health_late_contention`,
`health_late_clock_failure`, `health_log_levels`), because nested objects are
not OTel attribute values. A metric mirror (`finecode.telemetry.evidence_loss`
per signal and cause, `finecode.telemetry.signal_available` per signal) reports
the same counts where metric export works.

How to read a missing summary: a process with no summary has reported nothing,
which is not the same as reporting no loss. A process killed mid-window keeps
only its earlier summaries, so the last summary is a lower bound on the loss,
never an overstatement. Loss on the collector side (after a successful export)
is never reported — only the health summary and the metric mirror speak, and
both stop at the export boundary.

## Locating a slow request

1. Find the caller span (`jsonrpc.client/{method}`) for the slow request and
   read its stage gaps: the gap bracketing the delay names the slow step.
2. Open the receiver child (`jsonrpc.server/{method}`) and read its stage gaps
   the same way. Compare the two spans' per-process gaps only; never subtract
   timestamps across processes — each process has its own monotonic clock, and
   only the wall timestamps are shared, for ordering, not arithmetic.
3. Check the ER and WM lag warnings in the same window: a warning on either
   side attributes unattributed elapsed time to loop starvation there.
4. Read the phase spans (`phase/<label>`) inside the slow handler: thread CPU
   near elapsed means the phase burnt the CPU itself; CPU far below elapsed
   with no lag warning means the time went to workers, subprocesses, or
   waiting that no span covers.
5. Decide: WM delay shows as gaps in the WM-side spans (caller stages for
   WM→ER, receiver stages for ER→WM) with no ER-side counterpart; ER delay is
   the mirror image; transport/scheduling delay shows as `submitted`→`enqueued`
   or `enqueued`→`dequeued` gaps with both endpoints idle. Absent stages and
   `unsupported` statuses narrow the field: an `unsupported` drain stage on
   stdio is normal, a missing `handler_started` with outcome `not_executed`
   means the request was rejected before the handler ran.

## Trace IDs and the WAL

Trace ids and WAL run ids are different namespaces with no navigation between
them yet: a trace shows how long each hop and phase took, the WAL shows what
the run did. Correlating the two (root trace ↔ WAL UUID) needs its own
decision and does not exist in this delivery.

## Timing coverage

Timing starts at the `initialize` exchange. Work before the ER's `initialize`
response -- process start, interpreter startup, config collection -- has no
hop or phase records; the first traced request is `initialize` itself.
