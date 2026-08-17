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
this is a third, independent concurrency layer alongside `prepare-envs`' two
([Bounding concurrency](../guides/preparing-environments.md#bounding-concurrency), ADR-0055).

The cap is sized once, when `WorkspaceContext` is constructed (`context.resolve_er_startup_concurrency`):
`FINECODE_WM_MAX_CONCURRENT_ER_STARTS` env var if set, otherwise
`finecode_extension_runner.concurrency.machine_subprocess_budget()` (not the sqrt-split used by
`prepare-envs`' layers — this guards a single flat axis, not two composing ones). There is no CLI
flag, since this cap isn't scoped to one command's request — it protects the WM server's whole
lifetime and every client that triggers ER starts against it. The resolved cap and its source are
logged at INFO once, at construction.

### Run fan-out concurrency

ER *startup* is bounded above, and subprocesses *inside* one ER are bounded by that ER's
`ICommandRunner` cap (ADR-0056) — but neither bounds how many projects are made to do work at
once. A `run` fanning out across N projects, each ER free to spawn up to its own subprocess cap,
composes multiplicatively exactly as `prepare-envs`' two layers do (ADR-0055). Both `run` fan-out
sites therefore acquire a per-project semaphore:

- `_api_handlers/_streaming.py` — the streaming path every external client uses (CLI, LSP, MCP).
- `run_service/proxy_utils.run_actions_in_projects` — the path `WorkspaceExecutor` uses for
  workspace-scoped actions and ER-originated fan-out.

The cap is resolved by `run_service.run_concurrency.resolve_run_project_concurrency()`:
`FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS` env var if set, otherwise
`default_layered_concurrency()` — the sqrt-split, *unlike* the ER-startup cap above, because this
layer composes with the per-ER subprocess cap beneath it. Machine-bound, so no
`finecode-workspace.toml` equivalent.

The semaphore is built **per fan-out call**, never shared process-wide. Fan-out is re-entrant (a
workspace-scoped action's handler can call back into the WM to fan out again — that is what
`OrchestrationPolicy.max_recursion_depth` bounds), and a shared semaphore would let an outer
fan-out hold every permit while waiting on an inner one that can never acquire any.

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
