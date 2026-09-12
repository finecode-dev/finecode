# WM Server Internals

This guide describes the internal architecture of the Workspace Manager (WM) server — the
`src/finecode/wm_server/` package.  It is aimed at developers working on the WM itself.

For user-facing concepts (Action, Handler, Preset, ER) see [Concepts](../concepts.md).

---

## Overview

The WM server is a long-lived TCP JSON-RPC server.  A single instance is shared by all
clients (LSP server, MCP server, CLI) in a workspace session.  It:

- maintains a `WorkspaceContext` holding the full runtime state
- discovers projects and resolves their configuration
- manages Extension Runner (ER) subprocesses
- dispatches action requests to the appropriate ERs
- broadcasts notifications back to all connected clients

```text
┌─────────────────────────────────────────────────────────────┐
│  Clients  (LSP server │ MCP server │ CLI)                    │
└────────────────────────┬────────────────────────────────────┘
                         │  TCP JSON-RPC
┌────────────────────────▼────────────────────────────────────┐
│  wm_server.py  — dispatch, connection tracking, auto-stop    │
│  _jsonrpc.py   — framing (length-prefixed JSON)              │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  _api_handlers/  — per-method handlers                       │
│  (_workspace, _actions, _runners, _streaming, _helpers)      │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  services/  — business logic                                 │
│  (runner_start_service, run_service/, action_tree, …)        │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  runner/  — ER process lifecycle + JSON-RPC client           │
│  (runner_manager, runner_client, _internal_client_*)         │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  config/  — config reading and domain object construction    │
│  (read_configs, collect_actions, config_models)              │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  domain.py / errors.py / context.py  — pure data models      │
└─────────────────────────────────────────────────────────────┘
```

Dependencies flow strictly downward.  Upper layers may import from lower ones, never
the reverse.

---

## Layers

### `wm_server.py` — TCP server and dispatch

The top-level module.  Responsibilities:

- Starts the asyncio TCP server on a free port and writes the port to the discovery file
  (`.venvs/dev_workspace/cache/finecode/wm_port`) so clients can find it.
- Tracks connected clients in `_connected_clients`.  Auto-shuts down 30 s after the last
  client disconnects, or 30 s after startup if no client ever connects.
- Reads incoming JSON-RPC messages in `_handle_client` and dispatches to:
  - **`_METHODS`** — request handlers (have an `id`, expect a response)
  - **`_NOTIFICATIONS`** — notification handlers (no `id`, no response)
- Runs each request as an `asyncio.Task` so concurrent requests from the same client are
  served in parallel.
- Handles streaming variants of `actions/run` and `actions/runBatch` specially: when a
  `partialResultToken` or `progressToken` is present, routes to dedicated streaming
  handlers that hold a reference to the `asyncio.StreamWriter` to push mid-request
  notifications.
- Catches known error types at the dispatch boundary (`ValueError` → invalid params
  error code, `ActionRunFailed` / `StartingEnvironmentsFailed` → internal error code)
  so unhandled exceptions produce a well-formed error response rather than crashing the
  connection.
- Runs a periodic event-loop lag monitor for the server's lifetime, so the WM records its
  own starvation instead of leaving it to be inferred from an ER-side timeout — see
  [Event-loop lag monitor](#event-loop-lag-monitor-servicesevent_loop_lag_monitorpy).

**Key globals** (module-level, single instance per process):

| Name | Purpose |
|---|---|
| `_connected_clients` | Set of active `StreamWriter`s |
| `_running_partial_result_tasks` | Per-client set of in-flight streaming tasks |
| `_server` | The `asyncio.Server` instance |
| `_discovery_file` | Path to the port file; deleted on shutdown |

### `_api_handlers/` — per-method handlers

Thin layer that validates incoming `params`, calls the appropriate service function, and
shapes the return value.  Should contain no business logic.

| Module | Methods covered |
|---|---|
| `_workspace.py` | `workspace/addDir`, `removeDir`, `listProjects`, `findProjectForFile`, `setConfigOverrides`, `getProjectRawConfig`, `getWorkspaceEditablePackages`, `startRunners`, `prepareEnvs`, `reloadConfig` |
| `_actions.py` | `actions/list`, `getTree`, `getPayloadSchemas`, `run` (non-streaming), `runBatch` (non-streaming), `reload` |
| `_runners.py` | `runners/list`, `restart`, `checkEnv`, `removeEnv` |
| `_streaming.py` | Streaming variants of `actions/run` and `actions/runBatch` (with `partialResultToken` / `progressToken`) |
| `_helpers.py` | Shared utilities: project resolution, payload coercion, result formatting |
| `document_sync.py` (service) | `documents/opened`, `closed`, `changed` notifications |

### `services/` — business logic

Each service module is a collection of related functions (not classes).  They receive
`WorkspaceContext` and perform one cohesive task.

| Module | Responsibility |
|---|---|
| `runner_start_service.py` | Public entry point for starting ERs.  Wraps `runner_manager` with auto-prepare-envs on failure. |
| `run_service/` | Action execution — see [Run service](#run-service) below. |
| `action_tree.py` | Builds the hierarchical action tree returned by `actions/getTree`. |
| `document_sync.py` | Syncs open-document state to ERs on restart. |
| `partial_results_service.py` | Manages partial-result token subscriptions for streaming responses. |
| `prepare_envs_service.py` | Creates and installs execution environments. Provides `install_env_for_project` for targeted auto-repair of a single env. |
| `in_flight_runs.py` | Register of runs dispatched and not yet finished, per project.  Maintained in memory regardless of WAL configuration, because recovery consults it to refuse replacing runners that are executing something (ADR-0079). |
| `config_reload_service.py` | Configuration recovery (`workspace/reloadConfig`): re-reads a project's config from disk, resolves its presets, then replaces its runners.  The order is load-bearing — preset resolution needs a running `dev_workspace` ER (ADR-0073). |
| `shutdown_service.py` | Cleans up runners and resources on server shutdown. |
| `event_loop_lag_monitor.py` | Samples the WM's own event-loop lag; logs a rate-limited warning carrying runner, process-budget and in-flight-run context when the loop is starved.  Diagnostic only. |

#### Run service

`run_service/` handles action dispatch end-to-end:

```text
execution_scopes.py   — route to project or workspace executor based on Action.scope
        │
        ├── project_executor.py   — dispatch to a single project's ER
        │
        └── workspace_executor.py — fan out across all projects, collect results
                │
                └── proxy_utils.py — payload/result conversion, project lookup,
                                     partial-result and progress forwarding
```

`proxy_utils.py` is the largest module in the services layer.  It handles:
- converting external payloads to ER-internal formats
- routing file-based requests to the correct project via `find_project.py`
- forwarding `PartialResult` and `ProgressNotification` streams from ERs to clients

When an action is *matrixed* (its handlers bind to concrete per-interpreter
envs produced by interpreter-matrix config expansion, ADR-0047), it fans out
per interpreter and returns ONE variant-keyed result (results nested by
interpreter, return codes OR'd) instead of a single un-keyed run — on BOTH
dispatch paths:
- non-streaming: `run_action` fans out via `run_service/matrix_runner.py`,
  running each variant through `proxy_utils._execute_action` and combining
  the serialized `RunActionResponse`s with `matrix_runner._combine_variant_responses`.
- streaming (`finecode run` / `actions/runBatch` with a `partialResultToken`):
  `run_service/matrix_streaming.py` fans out one interpreter-scoped call to
  `proxy_utils.run_with_partial_results` per variant, forwarding each
  variant's live partials tagged with that interpreter (the CLI renders them
  under a per-interpreter sub-heading), and combines the final per-variant
  responses with the same `matrix_runner._combine_variant_responses`.
Non-matrix actions use the existing single/multi-env dispatch path unchanged
on both paths.

A matrixed action's fan-out can be restricted to a subset of its declared
interpreter axis (PRD-0003 AC8): `run`'s `--env`/`--interpreter` selectors
(and, absent those, a matrix env's config-declared `default_interpreters`
policy) are resolved ONCE per project by
`run_service/run_selection.selected_interpreters_for_project` (a thin
WM-side wrapper around the pure `config/env_selection.resolve_selected_interpreters`
resolver — the same resolver `prepare_envs_service` uses for `prepare-envs`)
at each run entry point (`actions/run`, `actions/runBatch`, both with and
without a `partialResultToken`), then threaded down as a
`selected_interpreters: set[str] | None` argument to whichever fan-out site
handles the request — `matrix_runner.run_matrix_action` or
`matrix_streaming.run_matrix_with_partial_results`. `None` (no selectors, no
narrowing config default) runs the full axis, unchanged; an interpreter
outside the resolved axis raises `ActionRunFailed`.

### `runner/` — ER process lifecycle and JSON-RPC client

| Module | Responsibility |
|---|---|
| `runner_manager.py` | Start, stop, restart ER processes.  Registers the callbacks an ER can invoke, initializes handlers.  The authoritative place for ER lifecycle state changes. |
| `runner_client.py` | High-level JSON-RPC client API used by services.  Wraps `_internal_client_api` with typed methods (`run_action`, `reload_action`, etc.).  Also defines `ExtensionRunnerInfo` (extends `domain.ExtensionRunner` with runtime fields). |
| `preset_resolution.py` | Resolves a project's py-preset contributions by asking a running `dev_workspace` ER where each preset package lives (`finecode/resolvePackagePath`), then hands the result to `config/read_configs` to merge.  The runner-dependent half of reading a project config. |
| `run_dispatch_bridge.py` | Slot filled by `services/run_service` — see [ER→WM callback slots](#erwm-callback-slots). |
| `knowledge_bridge.py` | Slot filled by `services/knowledge_service`. |
| `wm_bridge.py` | Slot filled by `wm_server.py`, for broadcasting to clients and ER log forwarding. |
| `elicitation_bridge.py` | Slot filled by `wm_server.py`, for putting a question to the client that started the run (ADR-0082).  Also owns the registry mapping a streaming run to its originating connection. |
| `_internal_client_api.py` | Low-level send/receive over the `AsyncIOThread`. |
| `_internal_client_types.py` | Protocol types for WM↔ER communication (request/response/notification dataclasses, method name constants). |
| `finecode_cmd.py` | Builds the command line to launch an ER subprocess. |

#### ER→WM callback slots

An ER originates requests as well as receiving them: it asks the WM to run an
action, to enumerate subactions, to answer a knowledge query, and it pushes log
records and user messages up for display.  Those callbacks are registered on the
runner's JSON-RPC client, so they are received in the `runner` layer — but the
code that answers them lives in `services/` or in `wm_server.py`, above it.

Rather than import upward, `runner` declares a **slot**: a module holding a
`Protocol` of what it needs plus `install()` / `handlers()` / `reset()`.  The
owning layer fills the slot as an import-time side effect
(`run_service/__init__.py` imports `er_dispatch`, `knowledge_service` and
`wm_server` install themselves directly).  Dependencies keep pointing downward
and `wm-layered` stays green with no ignore entry for this surface.

An unfilled slot behaves according to whether its caller needs an answer:
`run_dispatch_bridge`, `knowledge_bridge` and `elicitation_bridge` return `None`
and their callbacks raise, while `wm_bridge` defaults to a null implementation
that drops the notification.  See ADR-0072.

`elicitation_bridge` is the one slot whose traffic goes *back out* to a client
rather than being answered inside the WM, and it is the only place the WM sends
a client a request rather than a notification.  Two consequences worth knowing
before touching it: the question is addressed to the connection that started the
run — never broadcast, which is why the slot also owns the run→connection
registry that `_api_handlers/_streaming.py` writes — and a client that did not
declare the capability at `client/initialize` is never sent one, so the common
non-interactive case costs a typed "nobody could be asked" rather than a
timeout.  See ADR-0082.

### `config/` — config reading and domain object construction

| Module | Responsibility |
|---|---|
| `read_configs.py` | Reads and merges `pyproject.toml`, `preset.toml`, and `finecode-workspace.toml`.  Returns raw dicts and `EnvConfig` instances.  Pure — resolving *where* a py-preset package lives needs a running ER, so that step lives in `runner/preset_resolution.py` and calls back into `read_project_config_sources` / `finish_project_config` here.  `read_project_config` is the no-presets path for callers with no runner available. |
| `collect_actions.py` | Builds `Action`, `ActionHandler`, `ServiceDeclaration` domain objects from the raw config dict. |
| `config_models.py` | Dataclasses used for structured config validation via `cattrs`. |

### `domain.py` / `errors.py` / `context.py`

Already documented in detail:

- [`domain.py`](../../src/finecode/wm_server/domain.py) — pure data models; no I/O
- [`errors.py`](../../src/finecode/wm_server/errors.py) — unified `WmError` hierarchy
- [`context.py`](../../src/finecode/wm_server/context.py) — `WorkspaceContext` with full
  lifecycle and concurrency documentation

---

## Key flows

### Workspace initialization (`workspace/addDir`)

```text
workspace/addDir
  → _handle_add_dir  (_api_handlers/_workspace.py)
  → runner_manager.add_workspace_dir
      → scan filesystem for projects (find_project.is_project)
      → read_configs.read_project_config  (raw config → ws_projects_raw_configs)
      → Project added to ws_projects with CONFIG_VALID / NO_FINECODE / CONFIG_INVALID
      → [inside workspace_state_lock] project_init_locks entries created
      → [per project, under project_init_lock]
          → collect_actions.collect_project  (raw config → CollectedProject)
          → start dev_workspace ER via runner_start_service
          → ER resolves presets → runner_manager updates CollectedProject with
            preset contributions and canonical action metadata
          → ws_projects entry upgraded to ResolvedProject
```

### Action execution (`actions/run`)

```text
actions/run
  → _handle_run_action  or  _handle_run_action_with_partial_results_task
  → run_service/execution_scopes.py  (route by Action.scope)
      → project scope:  project_executor.run_action_in_project
      → workspace scope: workspace_executor.run_action_in_workspace
                           → fans out to project_executor per project
  → project_executor
      → proxy_utils: find project, convert payload
      → runner_client.run_action_in_project  (JSON-RPC → ER)
      → ER executes handlers and returns result
      → proxy_utils: convert result back
  → (streaming) partial results / progress forwarded via partial_results_service
```

### Configuration recovery (`workspace/reloadConfig`)

```text
workspace/reloadConfig
  → _handle_reload_config  (_api_handlers/_workspace.py)
  → config_reload_service.reload_config
      → [optional] read_configs.read_projects_in_dir over ws_dirs_paths  (rescan)
      → [per project, under project_init_lock]
          → refuse if a run is in flight in this project (ADR-0079)
          → ws_projects_raw_configs.pop(project_dir)   ← every re-read path
                                                          short-circuits on this
          → runner_start_service.start_runners_with_auto_prepare
              → re-read config with presets, collect actions, upgrade to
                ResolvedProject
          → runner_manager.restart_extension_runners   ← replace, per ADR-0073
          → invalidate ws_action_schemas / cached_actions_by_id /
            project_path_by_dir_and_action for this project
```

**The order of the last two steps is load-bearing.** Preset resolution asks the
project's running `dev_workspace` ER where preset packages live, so configuration must
be re-read while the old runners are still up. Stopping them first does not fail
loudly: `preset_resolution` leaves `py_presets_config = None` when no runner answers,
and the project silently loses every preset contribution. See ADR-0073.

If the re-read fails, the configuration that was already in effect is put back — a
failed recovery leaves the project with the old configuration, never with none.

### ER restart recovery

When an ER crashes or is restarted:

1. `runner_manager` detects the process exit and sets `ExtensionRunnerInfo.status = EXITED`.
2. On the next request that needs that ER, `runner_start_service.get_or_start_runners_with_presets`
   detects the non-RUNNING status and restarts it.
3. After restart, `document_sync.py` re-sends open-document state from
   `WorkspaceContext.opened_documents` so the ER has the current file contents.

### ER process termination

An ER OS process is stopped one of two ways, depending on whether it was ever
sent an exit request:

- **Graceful** (`runner_manager.stop_extension_runner`, called for
  `RUNNING`/`REPAIRING` runners): sends `shutdown` then `exit` over the RPC
  channel and waits up to `_STOP_TIMEOUT_SEC` (10s) for
  `JsonRpcClient.server_process_stopped` to fire. If it doesn't fire in time,
  this path only **logs a warning — it does not force-kill**. The ER already
  has the exit request and may legitimately still be tearing down its own
  spawned subprocesses (e.g. a package-manager invocation); killing it mid
  cleanup risks orphaning exactly the children a slower-but-graceful exit
  would have reaped itself. `shutdown_service.on_shutdown` stops every such
  runner **concurrently** (`asyncio.gather`), not one at a time — it runs
  synchronously inside the WM's own event loop, so a sequential sweep would
  block the whole server for `N × _STOP_TIMEOUT_SEC` in a workspace with many
  runners, which is indistinguishable from a hang to anyone watching.
- **Force-kill** (`JsonRpcClient.force_kill()` — POSIX: `os.killpg` on the
  process group, since ERs are spawned with `start_new_session=True`;
  Windows: `taskkill /F /T`): used only where no exit RPC was ever sent, so
  there is no in-progress graceful cleanup to interrupt:
  - `_start_extension_runner_process` calls it on any start-attempt failure
    (port handshake timeout, debug-port timeout, connect failure) — the OS
    process may already be spawned even though the ER never became reachable.
  - `shutdown_service.on_shutdown` calls it on any runner still
    `INITIALIZING` when the WM itself shuts down — this covers a shutdown
    racing with an in-flight start attempt that hasn't hit its own timeout
    yet. `runner.client` is attached immediately after construction (before
    `start()` is even called) specifically so this sweep always has a handle,
    regardless of how far the start attempt got.

### Combined subprocess-concurrency budget

ER startup and subprocess work are two phases of the same fleet, and both are CPU/IO-heavy, so the
WM sizes them from **one** machine-bound total instead of letting each claim a full
`machine_subprocess_budget()`. `WorkspaceContext.__post_init__` resolves `SubprocessBudgets` once
(`services/process_budget.py:resolve_subprocess_budgets`) and splits it in half — `startup_cap =
total // 2` for the ER-startup semaphore, the remainder for the process budget, both floored at
`1`. The "-1 core for the WM" headroom therefore holds in aggregate:
`startup_cap + work_cap ≤ machine_subprocess_budget()`. The two budgets stay **separate objects** —
a run holding work slots must never block on the startup cap to start the ER it fans into
(ADR-0090).

The total is `FINECODE_MAX_CONCURRENT_PROCESSES` if set, otherwise `machine_subprocess_budget()`
(CPU-affinity-aware core count minus one). There is no CLI flag and no `finecode-workspace.toml`
entry: it is a property of the machine, and it protects the WM server's whole lifetime. The
resolved total and both derived caps are logged at INFO once, at construction. See ADR-0093.

### ER startup concurrency

`_start_extension_runner_process` holds `WorkspaceContext.er_startup_semaphore` from just before
spawning the ER process until its RPC channel is confirmed connected, then releases it — bounding
how many ERs may be mid-startup at once, across every trigger (workspace init fanning out across
projects, a matrixed `run` fanning out across interpreter children, and `prepare-envs`' own
runner-start step), since they all call through this one function. It does **not** bound the
triggering action's execution afterward — that runs in the ER's own process, a separate and far
more variable resource cost than the CPU/memory-bursty spawn+import window this cap targets.

This is what fixes `Didn't get port in 30 seconds` failures on constrained machines: without it, a
workspace-wide `run` can attempt far more concurrent ER spawns than the machine can schedule
promptly, delaying some ERs' port handshake past the hardcoded 30s window in
`finecode_jsonrpc.client._connect_to_server_io`. See ADR-0063 for the full rationale, including why
this stays a *separate* budget from the process budget below: the WM starts runners lazily *inside*
a fan-out, so a run holding process-budget slots must never have to wait on the same budget to
start the very ER it is fanning out into — that would deadlock (ADR-0090).

The cap is sized once, when `WorkspaceContext` is constructed, as half of the combined budget
above — there is no dedicated env var for it. There is no CLI flag either, since this cap isn't
scoped to one command's request — it protects the WM server's whole lifetime and every client that
triggers ER starts against it. The resolved cap is logged at INFO once, at construction.

### Process budget

The WM owns a single machine-wide budget of subprocess *work slots*,
`services/process_budget.py:ProcessBudget`, sized as the work half of the combined budget above
(`total − startup_cap`). ERs lease
slots from it once per action run (over `finecode/leaseProcessBudget` / `finecode/releaseProcessBudget`)
and the WM pushes each ER's granted quota down as a gate target
(`finecodeRunner/updateProcessBudget`). Inside an ER, `CommandRunner` and `ProcessExecutor` draw
from that one gate (`finecode_extension_runner/process_slots.py`), so project fan-out, the
interpreter matrix, and `prepare-envs` all lead to the same three leaves and the same one bound.

The ER flags a lease *nested* when its run arrived at orchestration depth > 0. Every
non-streaming dispatch arrives at depth ≥ 1, so almost every ER lease is nested. Only streaming
client runs, the WM's own env checks, and runs whose dispatch declares `RunBudget(waits=True)`
wait for a slot.

Leases that wait are held to the budget plus at most one stall-escape slot; leases that do not
wait take at least one slot each, so their total is the budget or the number of active
non-waiting runs, whichever is greater. Recursion depth bounds a chain's *height*, not a
fan-out's *width*. See ADR-0090 and ADR-0094.

That non-waiting escape is load-bearing: auto-prepare (`install_env_for_project`) runs *inside*
an already-dispatched run, as a root, and would deadlock waiting on slots its own ancestor holds
if its leases were classified as nested.

A waiting lease that has seen no budget movement for `STALL_ESCAPE_SEC` (30 s) is granted one
slot over the budget, with a WARNING naming the waiting runner, how long it waited and the
current holders. At most one stall escape is outstanding at a time, so a stalled budget drifts
by at most one slot; nested leases never reach the wait path.

A dispatch may declare a `RunBudget` (`domain.py`): `waits` overrides the ER's nesting flag and
`max_slots` caps the requested width. It is recorded on the run's in-flight entry, which the
lease handler looks up by the ER's run id, and declared at dispatch — the same action run from
elsewhere keeps the ER's own request. `prepare-envs` steps 5 and 6 use it: each project's
`create_envs` / `install_envs` run waits for `max(1, work_cap // project_count)` slots, so about
`work_cap` projects build envs at once while a single project still gets the whole work cap.
Back-channel project dispatches declare `RunBudget(waits=False)`, because a streaming child
arrives at depth 0 and would otherwise wait while its parent holds slots.

The WM leases from the same budget for subprocess work of its own: each env version check
(`runner_manager.check_runner_within_budget`, used by `prepare-envs` and `runners/checkEnv`)
holds one non-nested slot under the owner id `wm:env-version-check` while its interpreter
runs. `prepare-envs` checks every project's env at once, and unbounded, the interpreters
overload the machine until healthy envs miss the check's deadline — and a failed check gets
the env deleted and recreated. A timed-out check is also retried with a longer deadline
(`VERSION_CHECK_TIMEOUTS_SEC`) before the env is declared invalid, and every invalid verdict
carries its reason into the `prepare-envs` warning.

`OrchestrationPolicy.max_project_fanout` (64) is a *separate* mechanism and is not a capacity
limit: it refuses, and it applies only when `orchestration_depth > 0`. It guards against runaway
recursive orchestration, not against a large workspace. A request arriving from a person at depth
0 is never refused for width — see ADR-0067, which amends ADR-0016 on this point.

### Auto-repair (`install_env_for_project`)

Triggered when `runner_manager.update_runner_config` receives error `-32001` from the ER
(missing package or stale entry points).  Runs the same `CreateEnvsAction` + `InstallEnvsAction`
sequence as `prepare-envs` — scoped to the affected env — then restarts the ER via
`restart_extension_runner`.  The only difference from a manual run is which runner executes
those actions; see [Automatic env repair](../guides/preparing-environments.md#automatic-env-repair)
for the routing rules.

---

## Infrastructure

### Discovery and startup serialization (`wm_lifecycle.py`)

Clients find the WM server by reading the port from the discovery file.
`wm_lifecycle.py` provides:
- `discovery_file_path()` — canonical path for the shared server
- `running_port()` — reads the file and verifies the port actually accepts connections
- A `FileLock` to serialize concurrent startup attempts across processes


### Reconnection and server replacement (ADR-0074)

`ApiClient` reconnects when its connection drops: bounded exponential backoff with
jitter, re-reading the discovery file each attempt because a restarted WM listens on a
new port. Requests that were in flight fail and are never retried — an action may have
published an artifact before the drop, and the client cannot tell how far it got.

Restoring the socket is not the success condition. `configure_reconnect(policy,
on_reattach=...)` takes the session-setup routine that `connect()` also calls, so
first-connect and reconnect cannot drift; a client whose re-attach fails reports itself
disconnected rather than connected. Each surface supplies its own: the MCP server
re-adds its workspace dir and invalidates the tool list, the LSP server re-adds its
folders and re-supplies open documents (which it mirrors locally for exactly this
reason), the CLI in shared-server mode re-adds its dir and re-subscribes to logs.

`wm_lifecycle.replace_running_server()` is the WM-replacement sequence: shutdown, poll
until the port is free, start, wait ready, wait for the client's *next* connection.
It lives here rather than in a client surface because a server cannot define its own
replacement — the one recovery operation that is not a WM method.

### Write-Ahead Log (`wal.py`)

Optional feature.  When enabled, `WalWriter` appends action results to a log file for
replay or inspection.  `WorkspaceContext.wal_writer` is `None` when WAL is disabled.

### Event-loop lag monitor (`services/event_loop_lag_monitor.py`)

A single `asyncio` task samples the delay between when it was due to run and when the loop
actually ran it — `lag = loop.time() - next_due` — every 0.5 s.  It is the only place from
which the WM can see its own starvation: when the loop is busy with a long synchronous
callback, with ER or subprocess fan-out, or with external CPU pressure, the WM's own RPC
replies are late but nothing in the WM otherwise records why.

When lag exceeds 1 s the monitor logs one `WARNING` per 30 s cooldown carrying the lag and
the evidence a reader needs to attribute it:

- **What the WM consumed over the late window** (previous sample → this one):
  `wm_cpu_ms` / `wm_cpu_pct` (process CPU time, all WM threads) and the
  `voluntary_switches` / `involuntary_switches` deltas from `getrusage`.  CPU close to the
  window means the WM's own code held the loop (a long synchronous callback, or a WM
  thread holding the GIL); CPU near zero with a jump in involuntary switches means the WM
  was runnable but the OS gave the CPU to other processes; CPU near zero with many
  voluntary switches means the loop was blocked waiting, typically on synchronous I/O.
- **How contended the host was**: `load_1m` against `cpu_count` (affinity-aware).
- **How much work was queued on the loop**: `ready_callbacks`, the stdlib loop's ready
  queue length — a flood of short callbacks lags the loop without any single long one.
- **What the WM was doing**: `runners_starting` (ERs `INITIALIZING` or `REPAIRING`),
  `runners_running`, `budget_granted` / `budget_total` from the process budget, and
  `in_flight_runs`.

Every value is written into the message text, because the file log and the client log
stream render only the message; the same values are also bound as loguru `extra` fields
for structured consumers such as the OTel sink.  Values the platform cannot provide
(`getrusage` and the load average on Windows, the ready queue on non-stdlib loops) read
`n/a` / `None`.  When lag drops back, a single `INFO` record closes the episode: how long
the stall lasted, how many samples lagged and the worst lag among them, how many warnings
the cooldown suppressed, and the WM CPU and context-switch totals over the whole episode
(`max_lag_ms`, `lagging_samples`, `episode_*`).  Read the recovery line, not the warning,
to judge a stall's size — the cooldown hides the later samples, which are often the
worst.  A healthy loop produces nothing at all.

It is diagnostic: it leases nothing, takes no lock, and nothing depends on it running.  The
sample interval, threshold and cooldown are module constants (`SAMPLE_INTERVAL_SEC`,
`WARN_THRESHOLD_SEC`, `WARN_COOLDOWN_SEC`).  `wm_server.start` launches it and
`wm_server.stop` cancels it.

---

## Error handling conventions

- All WM domain errors are defined in `errors.py` and rooted at `WmError(Exception)`.
- The dispatch boundary in `wm_server.py` (`_handle_request_task`) catches known error
  types and maps them to JSON-RPC error codes.  Unknown `Exception` subtypes fall through
  to a generic `-32603` response with `logger.exception`.
- Services raise typed errors; API handlers let them propagate to the dispatch boundary
  rather than catching and re-wrapping.
- `ConfigurationError` and `PresetPackageNotInstalledError` re-exported from
  `config_models.py` for backward compatibility — new code should import from `errors.py`.
