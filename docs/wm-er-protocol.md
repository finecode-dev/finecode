# FineCode WM-ER Protocol

This document describes the communication protocol between the FineCode Workspace
Manager (WM) and Extension Runners (ER). WM is the JSON-RPC client; each ER is a
JSON-RPC server.

The WM-ER protocol uses JSON-RPC 2.0 with LSP-style wire framing. Lifecycle method
names (`initialize`, `initialized`, `shutdown`, `exit`) and text-document notification
names follow LSP conventions; all FineCode-specific commands use direct JSON-RPC
method names.

## Transport

- JSON-RPC 2.0
- LSP-style framing (`Content-Length: N\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n{json}`) over a **loopback TCP socket**. On startup the ER binds a free port on `127.0.0.1` and announces it on stdout as `Serving on ('127.0.0.1', <port>)`; the WM reads that line from the spawned process's stdout and connects to the advertised port. (A stdio transport also exists in the ER but is not the default path.)
- WM spawns ER processes with:
  - `python -m finecode_extension_runner.cli start --project-path=... --env-name=... --log-level=INFO`
  - `--log-level` sets the global default log level for the ER process (default: `INFO`); fine-grained per-group overrides are delivered later via `finecodeRunner/updateConfig`
  - `--debug` enables a debugpy attach flow before WM connects (the debug port is announced on stdout as `Debug session: 127.0.0.1:<port>`)
- All parameter object keys use camelCase.

### Startup-failure diagnostics (requirement / edge case)

Until the ER publishes its port, there is **no RPC channel**, so `er/logRecords`
forwarding (see ER→WM notifications) cannot run — a runner that fails to start or exits
before announcing its port can forward nothing. Because the ER writes its own loguru
diagnostics to **stdout**, the WM-side client retains the **startup-window stdout**
(bounded) in addition to the subprocess **stderr**, and surfaces both — as a
`Server stdout output:` / `Server stderr output:` tail — in the `ServerFailedToStart`
error. That error is logged by the WM (reaching subscribed clients as `server/logRecords`
with `source: "wm"`, per ADR-0049) and returned in the triggering operation's error
response. Capture stops once the port is published; after that the live channel and
`er/logRecords` take over. This keeps a "runner failed to start" failure legible over the
protocol even though the runner never became reachable. (Regression-tested in
`finecode_jsonrpc/tests/test_startup_output_capture.py`.)

## Lifecycle

1. WM starts the ER process (per project + env).
2. WM sends `initialize` and waits for the ER response.
3. WM sends `initialized`.
4. WM sends `finecodeRunner/updateConfig` to bootstrap handlers and services.
   - ER processes it and returns `{}`.
5. WM sends `finecodeRunner/resolveActionMeta` to get action meta info.
   - ER returns a complete map of `configSource → { canonical_source, runs_concurrently, scope, parentActionSource, language }` for every action
     whose class can be imported in this env.  Actions that fail to import are omitted.
   - WM stores these on its `Action` domain objects before the runner is considered ready.
6. On shutdown: WM sends `shutdown` then `exit`.

> **Multi-env runs:** when an action's handlers span more than one env, the WM
> orchestrates execution segment-by-segment using `actions/runHandlers` so that
> the serialized run context (`previousResult`) crosses the wire only at actual
> env boundaries. See [Multi-Env Action Orchestration](#multi-env-action-orchestration).

## Message Catalog

### WM -> ER

**Requests**

- `initialize`
  - Standard LSP initialize request, carrying the telemetry settings in
    `initializationOptions.telemetry`: `{ "mode": "disabled" }`, or
    `{ "mode": "otlp", "otlpEndpoint": "<host>:<port>" }` (`otlpEndpoint`
    is present if and only if mode is `"otlp"`). Any other object counts as
    non-disabled: the ER attempts activation and reports per-signal status.
  - Example params (trimmed):
    ```json
    {
      "processId": 12345,
      "clientInfo": {"name": "FineCode_WorkspaceManager", "version": "0.1.0"},
      "capabilities": {},
      "workspaceFolders": [{"uri": "file:///path/to/project", "name": "project"}],
      "trace": "verbose",
      "initializationOptions": {"telemetry": {"mode": "otlp", "otlpEndpoint": "127.0.0.1:4317"}}
    }
    ```
  - Result: the standard server capabilities and info, plus
    `telemetry: { "traces": ..., "metrics": ..., "logs": ... }` with each
    signal `"disabled"`, `"active"` or `"unavailable"`. The ER makes one
    setup attempt before responding: it activates the OTLP providers, arms
    and starts its event-loop lag monitor, installs the synchronous-phase
    timing binding, and writes the first telemetry health summary. A second
    `initialize` replays the stored result without attempting anything again.
    Telemetry settings are never carried by or applied from
    `finecodeRunner/updateConfig`.

- `shutdown`
  - Standard LSP shutdown request.

- `finecodeRunner/updateConfig`
  - Params: `{ "workingDir": string, "projectName": string, "projectDefPath": string, "config": object }`
  - Config shape (top-level):
    - `actions`: list of action objects (`name`, `handlers`, `source`, `config`)
    - `action_handler_configs`: map of handler source → config
    - `services`: list of service declarations (optional)
      - `interface` (string): fully-qualified path of the service protocol/interface (e.g. `"finecode_extension_api.interfaces.icommandrunner.ICommandRunner"`)
      - `source` (string): fully-qualified path of the implementation class
      - `env` (string): execution environment name the service implementation runs in
      - `dependencies` (array of strings): dependencies to install into `env` for this service
      - `config` (object, optional): service-specific configuration, structured into a `config`-annotated constructor parameter on the implementation the same way action handler `config` is (ADR-0056). `null`/absent when the service takes no config.
    - `handlers_to_initialize`: map of action name → handler names (optional). When present, the ER eagerly initializes the listed handlers so they are ready before the first `actions/run` request. When absent or null, no eager initialization is performed and handlers are initialized on first use.
    - `logging`: logging configuration for this ER instance (optional)
      - `defaultLevel` (string): global log level, e.g. `"INFO"`, `"DEBUG"`, `"TRACE"`. Overrides the `--log-level` startup value when present.
      - `logGroups` (object): map of logger-name prefix → level string. Prefix matching applies — a key of `"fine_python_ruff"` covers `fine_python_ruff`, `fine_python_ruff.linter`, etc. The longest matching prefix wins. Example: `{ "fine_python_ruff": "TRACE", "finecode_extension_runner": "WARNING" }`
  - Result: `{}` (empty object)

- `finecodeRunner/updateLogging` (ADR-0049)
  - Params: `{ "forward": boolean, "forwardLevel": string }`
  - Result: `{}` (empty object)
  - Toggles ER -> WM log forwarding (see `er/logRecords` below). This is a
    **dedicated, process-level** control message — unlike
    `finecodeRunner/updateConfig`, applying it never rebuilds the ER's
    `RunnerContext` (no handler/service re-initialization).
  - The WM sends this only while at least one client is subscribed to WM logs
    (`server/subscribeLogs`); it disables forwarding again (`forward: false`)
    once the last subscriber unsubscribes or disconnects. A newly started ER
    is brought up to date immediately if a client is already subscribed.
    Forwarding is therefore zero-cost on the ER when nobody is observing.
  - `forwardLevel` sets the minimum level forwarded to the WM
    (`"TRACE"|"DEBUG"|"INFO"|"SUCCESS"|"WARNING"|"ERROR"|"CRITICAL"`) and is
    **independent** of the ER's own file/stdout log level configured via
    `finecodeRunner/updateConfig`'s `logging` block — enabling forwarding
    never changes what the ER writes to its own log file.

- `finecodeRunner/getInfo`
  - Params: `{}`
  - Result: `{ "logFilePath": "/abs/path/to/runner.log" | null }`
  - Returns runtime information about the runner. Currently reports the path
    to the runner's log file, or `null` if logging to a file is not configured.

- `actions/run`
  - Params: `{ "actionName": string, "params": object, "options": object | null }`
  - Executes only the action's handlers whose `env` equals this ER's env;
    handlers without an `env` always run. The WM relies on this to address one
    interpreter variant of a matrixed action by sending `actions/run` to that
    variant's env.
  - Options keys (camelCase):
    - `meta`: `{ "trigger": "user|system|unknown", "devEnv": "ide|cli|ai|git_hook|ci", "orchestrationDepth": int }`
      - `orchestrationDepth`: cross-boundary hop counter, defaults to `0`. The ER propagates it unchanged via `RunActionMeta.orchestration_depth`.
    - `partialResultToken`: `int | string` (used to correlate `$/progress`)
    - `resultFormats`: `["json", "string"]` (defaults to `["json"]`)
    - `callerKwargs` (object | null): serialized `CallerRunContextKwargs`, or `null` when none. The ER deserializes it into the action's run context `caller_kwargs` parameter.
  - Result (success):
    ```json
    {
      "status": "success",
      "resultByFormat": "{\"json\": {\"...\": \"...\"}}",
      "returnCode": 0
    }
    ```
  - Result (streamed): used when `partialResultToken` was provided and all
    results were delivered via `$/progress` notifications. The final response
    is an explicit completion signal — `resultByFormat` is intentionally empty.
    The WM treats this as a valid completion; an empty `resultByFormat` with
    any other status is a protocol error.
    ```json
    {
      "status": "streamed",
      "resultByFormat": "{}",
      "returnCode": 0
    }
    ```
  - Result (stopped):
    ```json
    {
      "status": "stopped",
      "resultByFormat": "{\"json\": {\"...\": \"...\"}}",
      "returnCode": 1
    }
    ```
  - Result (error):
    ```json
    {"error": "message"}
    ```
  - Note: `resultByFormat` is a JSON-encoded string (not a nested object) —
    the WM decodes it with `json.loads` after receiving the response.

- `actions/runHandlers`
  - Runs a named subset of an action's handlers sequentially within this ER,
    seeding `context.current_result` from a prior segment's serialized result.
    Used by the WM to orchestrate multi-env action runs; not used for single-env
    actions (those still use `actions/run`).
  - Params:
    - `actionName` (string): action name as registered via `finecodeRunner/updateConfig`
    - `handlerNames` (list of string): ordered list of handler names to execute;
      all must belong to this ER's env
    - `previousResult` (object | null): serialized `RunActionResult`
      (`dataclasses.asdict`) from the last handler of the preceding segment, or
      `null` for the first segment. Reconstructed as `context.current_result`
      before the first handler in `handlerNames` is invoked.
    - `previousContext` (object | null): serialized `STATE_TYPE` dataclass from
      the preceding segment's response, or `null` for the first segment or when
      the context has no `STATE_TYPE`. Restored into `context.state` before
      `context.init()` is called, so restored state is visible during
      initialization.
    - `callerKwargs` (object | null): serialized `CallerRunContextKwargs`, forwarded
      unchanged to all segments. Each segment deserializes it independently into the
      run context `caller_kwargs` parameter. Unlike `previousContext`, it does not
      chain across segments — the same dict is passed to every segment.
    - `options` (object | null): same keys as `actions/run`. `resultFormats`
      should be omitted (or `[]`) for intermediate segments and non-empty only
      for the final segment of a run.
  - Result (success):
    ```json
    {
      "status": "success",
      "result": {"<resultField>": "..."},
      "resultByFormat": {"json": {"...": "..."}, "string": "..."},
      "returnCode": 0,
      "context": {"<stateField>": "..."}
    }
    ```
    - `result`: serialized `RunActionResult` after all specified handlers ran
      (`dataclasses.asdict`). Pass as `previousResult` to the next segment's
      `actions/runHandlers` call.
    - `resultByFormat`: formatted results in the requested formats; `{}` when
      `resultFormats` was empty in options.
    - `context` (object | null): serialized `STATE_TYPE` after handlers ran;
      `null` when the context has no `STATE_TYPE` or `serialize_context()`
      returns `null`. Pass as `previousContext` to the next segment's
      `actions/runHandlers` call.
  - Result (streamed): used when `partialResultToken` was provided and all
    results were delivered via `$/progress`. `result` and `context` are still
    populated for chaining.
    ```json
    {
      "status": "streamed",
      "result": {"<resultField>": "..."},
      "resultByFormat": {},
      "returnCode": 0,
      "context": {"<stateField>": "..."}
    }
    ```
  - Result (stopped):
    ```json
    {
      "status": "stopped",
      "result": {"<resultField>": "..."},
      "resultByFormat": {"json": {"...": "..."}},
      "returnCode": 1,
      "context": {"<stateField>": "..."}
    }
    ```
  - Result (error): `{"error": "message"}`

- `actions/getPayloadSchemas`
  - Params: `{}`
  - Result: `{ action_name: JSON Schema fragment | null }`
  - Returns a payload schema for every action currently known to the runner.
    Each schema has `properties` (field name → JSON Schema type object) and
    `required` (list of field names without defaults).
    `null` means the action class could not be imported.

- `actions/mergeResults`
  - Params: `{ "actionName": string, "results": list, "resultFormats": ["json" | "string"] }`
    `resultFormats` defaults to `["json"]`.
  - `results`: list of serialized `RunActionResult` objects (`dataclasses.asdict`),
    one per concurrent segment or handler. Used by the WM after a concurrent
    multi-env run to merge the per-env results into a single final result.
  - Result: `{ "resultByFormat": { ... } }` or `{ "error": "..." }`. The `json`
    key is the merged result serialized with `dataclasses.asdict`. For `string`,
    the ER returns `string` or `styled_text_json`, depending on the result's text
    renderer. The `merged` key is not part of this request or response.

- `actions/reload`
  - Params: `{ "actionName": string }`
  - Result: `{}`

- `finecodeRunner/resolveActionMeta`
  - Params: `{}` (no params)
  - Result: `{ "actions": { "<configSource>": { "canonical_source": string, "runs_concurrently": bool, "scope": string, "parentActionSource": string | null, "language": string | null, "fileLoc": string | null }, ... }, "handlers": { "<configSource>": { "canonicalSource": string, "fileLoc": string | null }, ... } }`.
    `actions` covers every action whose class can be imported in this env; `handlers`
    covers every handler registered in this env.  Actions and handlers that fail to
    import are omitted entirely from their respective maps.  `fileLoc` is
    `"<path>:<lineno>"` of the class's source (relative to the project dir when inside
    it, else absolute), or `null` when it could not be resolved (e.g. a dynamically
    built class).
    Example: `{ "actions": { "myext.LintAction": { "canonical_source": "myext.actions.lint.LintAction", "runs_concurrently": true, "scope": "project", "parentActionSource": null, "language": null, "fileLoc": "myext/actions/lint.py:10" } }, "handlers": { "myext.LintHandler": { "canonicalSource": "myext.lint_handler.LintHandler", "fileLoc": "myext/lint_handler.py:20" } } }`
  - Called by the WM after `finecodeRunner/updateConfig` completes to store all action
    and handler metadata on its `Action`/`ActionHandler` domain objects before the
    runner is considered ready.  The WM uses `canonical_source` as the primary
    identifier in all subsequent action lookups.  `parentActionSource` and `language`
    are used to serve `finecode/getActionsForParent` requests (see ER→WM section).
    Fields absent from the response (import failure) remain `None` until another
    runner for the same project resolves them; if still unresolved when requested,
    resolution is retried on demand (see `finecode/getActionsForParent` below).
  - The key of both maps is the **config source** — the string written in the
    definition file, which for handlers is almost always a package-level re-export
    (`myext.LintHandler`) rather than the module the class is defined in
    (`myext.lint_handler.LintHandler`).  A handler's `canonicalSource` bridges the two
    (ADR-0054).  Unlike an action's, it is **identity metadata, not a dispatch key**:
    the WM still reaches a handler by traversing its action's handler list, so nothing
    in dispatch matches on it.  Consumers that need to recognize the same handler
    across projects — anything keying handlers, such as the knowledge extractor —
    must key on `canonicalSource`, because two different aliases can name one handler.

- One-shot action-metadata dump (CLI)
  - Argv: `python -m finecode_extension_runner.cli dump-action-meta --project-path=<dir>`,
    with `cwd` set to the project dir and `VIRTUAL_ENV` removed from the environment
    (the same launch as an ER start). Stdin: `{"sources": ["<configSource>", ...]}`.
  - Stdout carries only the format-1 document: `{"format": 1, "startedNs": int,
    "actionMetaFile": <fingerprint of action_meta.py>, "header": {"sysPath",
    "pythonpath", "sitePackages": {"path", "dir_mtime_ns", "pth": [...]},
    "headerStable": bool}, "entries": {"<source>": {"meta", "files", "dirs"}},
    "failures": {"<source>": {"error", "files"}}}`. fd 1 is redirected to stderr
    at the fd level before the imports, so import-time prints cannot corrupt it.
  - Exit codes: 0 answered (even with per-source `failures`); 2 is click's usage
    error, which is how the WM detects an older ER without the subcommand; any other
    non-zero exit, or unparseable stdout, means the env cannot run the dump.
  - It shares `action_meta.action_meta` with `finecodeRunner/resolveActionMeta`, so
    `meta` equals that command's per-action entry for the same class.

- `actions/resolveSource`
  - Params: `{ "source": string }` — an arbitrary import-path alias to resolve.
  - Result: `{ "canonicalSource": string }` — the fully qualified class path
    (`cls.__module__ + "." + cls.__qualname__`).
  - Raises a JSON-RPC error if the alias cannot be imported or resolved.
  - Used during action lookup when a caller provides an alias not already known
    from `finecodeRunner/resolveActionMeta` (full ADR-0019 support).

- `packages/resolvePath`
  - Params: `{ "packageName": string }`
  - Result: `{ "packagePath": "/abs/path/to/package" }`

**Notifications**

- `initialized` (standard LSP)
- `textDocument/didOpen`
  - Params: standard LSP `DidOpenTextDocumentParams` — `{ "textDocument": { "uri": string, "languageId": string, "version": int, "text": string } }`.
  - The ER seeds the file's tracked content directly from `text_document.text` —
    it never re-reads the file from disk for this notification. `text` is the
    client's buffer and is the source of truth regardless of what's on disk;
    the file may even no longer exist on disk (e.g. a stale IDE tab left open
    after the file was deleted), which would otherwise crash a disk read.
- `textDocument/didChange`
- `textDocument/didClose`
- `$/cancelRequest`
  - Sent by WM when an in-flight request should be cancelled. The ER sends it too, in the other direction, for `finecode/elicit` and the two run methods (see ER -> WM below).

### ER -> WM

**Requests**

ER → WM requests carry the caller's trace context in the existing envelope
`_meta.traceparent` field, so receiver spans parent to the matching caller
span across processes.


- `workspace/applyEdit`
  - Standard LSP request for applying workspace edits.
  - WM forwards this to its active client (IDE) if available.

- `projects/getRawConfig`
  - Params: `{ "projectDefPath": "/abs/path/to/project/finecode.toml" }`
  - Result: `{ "config": "<stringified JSON config>" }`
  - Used by ER during `finecodeRunner/updateConfig` to resolve project config.
  - The config is the WM's **resolved** config, served from its per-project cache —
    not the definition file's contents. Presets and `finecode-user.toml` are merged
    in, handler/service dependencies are merged into `[dependency-groups]`, and
    interpreter matrices are already expanded (ADR-0047): an env declaring
    `interpreters` no longer appears in `[tool.finecode.env]`, having been replaced
    by one `<base>-<impl>-<version>` child per interpreter, each with a singular
    `interpreter` and a `matrix_env` key naming its base. Only entries with
    `matrix_env` are matrix children; an `interpreter` without it is a plain pin
    that selects the venv's interpreter. Extensions reading the env table can rely on this ordering; it is
    pinned by `test_config_served_to_extensions_has_matrices_already_expanded`.

- `workspace/getWorkspacePackages`
  - Params: `{}`
  - Result: `{ "packages": { "<pkg_name>": {"dir": "/abs/posix/path", "wheel": "/abs/wheel.whl" | null, "editable": bool}, ... } }`
  - Returns the workspace-level package map resolved from
    `finecode-workspace.toml` (see ADR-0029). Each entry carries the package's
    source directory and the resolved install decision (`editable`, and the
    wheel built for it in wheel mode). The WM resolves this map once during
    `workspace/addDir` and caches it for the lifetime of the workspace context.
    The install mode and `exclude` set are resolved by `prepare-envs`, so the
    decision reflects the most recent run.

- `workspace/getProjectPaths`
  - Params: `{}`
  - Result: `{ "projects": [{ "path": "/abs/path/to/project", "configStatus": "valid" }, ...] }`
  - `configStatus` is one of `"valid"` (has FineCode config), `"no_config"` (no
    FineCode config, expected), or `"invalid"` (config present but invalid).
  - Returns all projects currently known to the WM with their config status.
    Handlers that need to run actions should filter to `"valid"` projects only.

- `finecode/runActionInProject`
  - Params:
    - `actionSource` (string): **fully qualified** import path of the action class —
      `f"{cls.__module__}.{cls.__qualname__}"` (e.g. `"myext.actions.lint.LintAction"`).
      Must not be a re-exported alias such as `"myext.LintAction"`. The WM resolves
      the action name by matching against the canonical source reported by
      `finecodeRunner/resolveActionMeta`; a re-exported path will not match and the
      request will fail.
    - `payload` (object): serialized action payload (`dataclasses.asdict`)
    - `meta` (object): `{ "trigger": string, "devEnv": string, "orchestrationDepth": int }`
    - `callerKwargs` (object | null): serialized `CallerRunContextKwargs`, or `null` when none. The WM passes it through opaquely to the target ER.
    - `partialResultToken` (int | string, optional): selects streamed partial-result
      delivery. Omission selects the existing non-streamed behavior. Tokens are
      scoped to the parent connection that sent them.
    - `progressToken` (int | string, optional): selects live child-progress
      relay. Omission keeps today's behavior: the child's progress is invisible
      to the parent. Scoped to the parent connection like `partialResultToken`;
      only honored together with `partialResultToken`. Client paths
      (`actions/run` token passthrough, `actions/runBatch`) are unchanged.
  - Result: `{ "result": <json result object>, "returnCode": 0|1, "subtreeStopped": boolean }` — `subtreeStopped` is true iff every delegated call of the run ended stopped, including the calls of same-env child runs at any depth. A `delegated call` here and below is a WM-path call only.
  - Runs the action at project scope (all env-runners of the ER's own project). WM enforces `OrchestrationPolicy.max_recursion_depth` before dispatching.
  - Streamed delivery: when `partialResultToken` is present, the WM allocates a
    fresh child token for the child runner and translates each matching
    `$/progress` notification back to the parent token. `value` is the child's
    stringified `json` result object: the WM strips the child's per-format
    wrapper (`{"json": {...}}`), so a streamed partial and the direct-final
    fallback have the same shape, which the parent ER structures into the child's
    result type. Other result formats are not forwarded. The streamed reply carries
    completion metadata (`returnCode`, with no result data). When no partial
    arrived, the WM emits the direct-final fallback notification on the parent
    token before completion. Per-call forwarding state (the parent-token map
    entry, forwarding pumps, the per-call progress buffer and the direct-final
    fallback) is released when the parent closes early (see
    `finecode/releaseDelegatedCall` below); a close or cancel sends
    `$/cancelRequest`, which cancels the child. The close completes when both
    the release and the child's stop have ended, or at the 5.0 s stop bound,
    whichever is first.
  - Live child-progress relay: when both `partialResultToken` and `progressToken`
    are present, the WM allocates a second fresh child token — the child progress
    token, a uuid distinct from the child partial token and never derived from
    caller tokens — passes it as the child
    run's progress token, and forwards the child's `$/progress` values to the
    parent ER on the parent's `progressToken`, translated into plain
    `report(message=...)` calls (percentages are never forwarded). Partials and
    progress keep separate tokens end to end. A `begin`
    carrying a string `message` relays that message, else its string `title`;
    a `report` relays a string `message` and drops a missing one silently; an
    `end` relays a string `message` and otherwise the fixed text
    `Delegated progress ended`. Malformed or unknown child values are dropped
    with a warning.
  - Progress bound: at most 100 delegated-progress notifications are held per
    call, summed over every WM stage and every child runner of the call; at the
    limit the oldest pending notification is discarded first, except that the
    newest pending notification that would produce a report is always kept.
    A failing progress send to the parent stops progress forwarding for that
    call only (logged once as a warning); partial results and the reply
    continue, and the call's progress is still consumed to its end so the
    child run's drain completes.
  - Progress drain: every relayed report reaches the parent before the delegated
    iterator ends on a received `end`, and none is delivered after it ends;
    reports arrive in child order. A parent that stops pulling still receives
    the bounded tail ending with the end.

- `finecode/releaseDelegatedCall`
  - Params: `{ "partialResultToken": <token> }` — the parent partial-result
    token of a delegated call the parent closed early.
  - Result: `{}`.
  - The parent ER sends this once when its delegated iterator ends by `aclose()`,
    finalization or cancellation while the `finecode/runActionInProject` request
    still has no reply, and sends `$/cancelRequest` for that request at the same
    moment. The close completes when both the release reply and the child's stop
    have ended, or 5.0 s after the close began, whichever is first; the 2.0 s
    release wait runs inside, and concurrently with, the 5.0 s stop bound, and a
    late or missing release reply only logs a warning. A repeated
    cancellation while waiting does not shorten the wait. No release is sent for
    a call whose reply was received, or on the same-env path. A call released
    before dispatch never acquires forwarding state. Notifications already handed
    to the connection before the release was processed may still arrive; none
    are forwarded after. On release the WM
    drops that call's forwarding state — no further `$/progress` and no
    direct-final fallback is sent to the parent for it — while the cancelled
    child stops and its own output drains to a discarding sink until it ends.
    Releasing an unknown, finished or already-released token (including another
    connection's value) replies `{}` with no effect.

- Version compatibility (delegated progress)
  - The delegated-progress fields — `progressToken` on
    `finecode/runActionInProject` and `finecode/releaseDelegatedCall` — require
    same-version WM and ERs, with no capability flag, handshake or fallback:
    sibling runner environments get the exact `finecode_extension_runner` pin
    the WM injects (repaired by `prepare-envs`); `dev_workspace` equality is a
    setup precondition that is not enforced. Behavior with a peer at another
    version is unspecified and untested (a stale parent ER simply never sends
    `progressToken`, so no child progress arrives); repair with `prepare-envs`
    and a runner restart.

- `finecode/getActionsForParent` (ADR-0045)
  - Params: `{ "parentActionSource": string }` — fully qualified import path of a
    parent action class.
  - Result: `{ "subactions": [{ "source": string, "canonicalSource": string | null,
    "language": string }, ...] }` — every action in this project whose resolved
    `PARENT_ACTION` matches `parentActionSource`, regardless of which env owns its
    handler.
  - Used by an ER's `get_actions_for_parent` to discover subactions that live in a
    different env than the caller — an ER only ever knows the actions its own env
    executes, so cross-env subaction discovery is delegated to the WM, which has
    the full project-wide action topology. Actions not yet resolved by any runner
    are resolved from the per-venv cache or a one-shot dump; never starts a runner.
    An action that still cannot be resolved
    is simply excluded rather than failing the whole request.

- `finecode/listWorkspaceActions`
  - Params: `{}` (no params)
  - Result: `{ "actions": [{ "name": string, "source": string, "canonicalSource": string | null,
    "scope": string | null, "project": string, "language": string | null,
    "parentActionSource": string | null, "fileLoc": string | null,
    "handlers": [{ "name": string, "source": string, "canonicalSource": string | null,
    "env": string, "fileLoc": string | null }, ...] }, ...] }` — the aggregated
    action/handler registry across **every** project and env in the workspace.
  - Backs the `IWorkspaceActionRegistry` service (see
    [Services](reference/services.md)). An ER only ever knows the actions its own env
    executes, so this cross-env picture can only come from the WM.
  - Values come straight from the WM's resolved domain objects, so a field is `null`
    when the runner that would resolve it has not started yet (or could not import the
    class). Callers must tolerate `null` on every optional field rather than assuming
    a fully-populated registry — notably `canonicalSource`, which is populated per env
    by that env's own runner.
  - On both actions and handlers, `source` is the config-facing alias and
    `canonicalSource` is the module the class is actually defined in. Key entities by
    `canonicalSource`: the same action or handler is reachable under several aliases,
    and the WM does not deduplicate the rows (a PROJECT-scope action appears once per
    project that registers it).

- `finecode/runActionInWorkspace`
  - Params:
    - `actionSource` (string): **fully qualified** import path of the action class —
      same constraint as `finecode/runActionInProject` above.
    - `payload` (object): serialized action payload
    - `meta` (object): `{ "trigger": string, "devEnv": string, "orchestrationDepth": int }`
    - `projectPaths` (list[string] | null): explicit POSIX project paths, or `null` for all projects that declare the action
    - `payloadOverridesByProject` (object | null): complete per-project payload overrides (keyed by POSIX path); the WM shallow-merges `{**payload, **overrides[project]}`
    - `concurrently` (boolean, default `true`): run projects concurrently.
  - Result: `{ "resultsByProject": { "<posix path>": <json result>, ... }, "subtreeStopped": boolean }` — same verdict rule as `finecode/runActionInProject` above.
  - Fans out the action across the specified projects (or all projects that declare it). Because this route is always nested orchestration (an ER handler asking the WM to fan out, so `orchestrationDepth > 0`), the WM enforces `OrchestrationPolicy.max_recursion_depth` before dispatching, and never refuses a fan-out for its width — see [ADR-0095](../finecode_internal_docs/adr/0095-workspace-fan-out-is-bounded-by-recursion-depth-not-width.md). The subprocess fan-out a dispatch leads to is bounded by the [process budget](guides/wm-server-internals.md#process-budget) (ADR-0090).

- `finecode/elicit`
  - Params: `{ "message": string, "options": [string], "default": string | [string] | null, "kind": "choice" | "text" | "multiChoice", "multiline": boolean, "initialValue": string | null, "placeholder": string | null, "descriptions": { [option]: string }, "timeoutSec": number, "runId": string | null }` — `kind` is left out for choice; every other new field is left out when unused. A `default` or `descriptions` entry naming an unoffered option is left out.
  - Result: `{ "outcome": "answered" | "declined" | "unavailable", "value": string | null, "values": [string] | null }` (ADR-0082); `value` and `values` are left out when unset.
  - Puts one multiple-choice question to the client that started the named run and waits for its answer. No origin, a gone client, nothing declared, or the deadline all resolve `unavailable` without raising.
  - When the WM stops waiting for the question (deadline, the run ending in any way, or the ER cancelling it), it sends the client `client/elicitWithdrawn` first ([WM protocol](wm-protocol.md#clientelicitwithdrawn)). A run that ends with an ask still pending gets that ask answered `unavailable` at once rather than at its deadline.
  - Cancelling the wait is how an action abandons a question: the ER sends `$/cancelRequest` for this request id (below) and the WM withdraws the question.

- `$/cancelRequest` (notification)
  - Params: `{ "id": number }`
  - Sent by the ER when the task awaiting its request is cancelled, synchronously before the cancellation completes: `finecode/elicit`, `finecode/runActionInProject`, and `finecode/runActionInWorkspace`. For `finecode/elicit` the WM withdraws the question from its client with reason `noLongerNeeded` (or `runCancelled` if the WM itself is cancelling the run) and answers the request `REQUEST_CANCELLED`. For the two run methods the WM cancels the call's WM run and its child requests, waits for the children, and replies `REQUEST_CANCELLED` with `error.data.subtreeStopped` carrying the verdict. A not-started run-method call replies `true`. A cancel that arrives before the request started, after it finished, twice, or for an unknown id withdraws at most once. A timeout is not an abandonment and sends none.
  - Connection loss cancels the lost ER's open delegated calls and nothing else; `finecode/elicit` is never cancelled this way.

- `finecode/elicitAvailability`
  - Params: `{ "runId": string | null }`
  - Result: `{ "canAnswer": boolean, "canAnswerText": boolean, "canAnswerMultiChoice": boolean }` — absent on an older WM reads as `false`. The ER probes availability before sending a text or multi-select question and sends no `finecode/elicit` when the shape is not reported.
  - Says whether a question could be put to a person right now, answered from the same facts as `finecode/elicit`; sends nothing to the client. Nested runs answer for the root client. An older WM answers method-not-found, which the ER reads as `false`. Startup-slot neutral: the handler does a synchronous dict read and waits on no runner, lease or person.

- `knowledge/registerSchema`
  - Params: `{ "snapshot": <object> }`
  - Result: `{ "accepted": boolean }` — `false` means the WM already held this
    exact schema, which is the ordinary case for a second runner hosting the same
    one.
  - Hands the WM the knowledge schema its fact store should be read against. The
    WM owns the store but must not import the package that declares the schema,
    so the schema travels as **data**: entity types, fields, relationships,
    per-provider `SUPPLIES` lists, and derived-predicate bodies. The document is
    produced by `finecode_knowledge.query.snapshot.registry_to_json` and is
    versioned inside itself (`v`); its shape belongs to the knowledge engine, and
    the WM's transport types keep it opaque so one wire format does not acquire
    two owners.
  - No Python crosses. A provider rebuilt from a snapshot is a *declaration of
    what it may supply*, never an extractor — asking one for its source inputs
    raises. Extraction runs where the provider actually lives.
  - Must be sent before the first `knowledge/query`; querying first fails with an
    error naming this method.

- `knowledge/query`
  - Params:
    - `query` (object): a serialized `Query`, produced by
      `finecode_knowledge.query.serialize.query_to_json`. Derived predicates
      appear **by registered name**, never inlined — the query is a reference
      into the schema the WM already holds.
    - `mode` (string, default `"verified"`): `"verified"` blocks until inputs
      verify. `"cached"` returns whatever the WM has memoized for this query
      immediately, carrying a `"cached"` reservation saying it was not
      re-verified; with nothing memoized it blocks and computes, so a caller
      always gets an answer rather than an empty one. The memo is per-WM-process,
      so `"cached"` only pays off against a live WM — a one-shot CLI run starts
      with an empty table.
    - `limit` (int | null): stop the walk after this many rows. Applied by the
      WM, because applying it in the ER would mean receiving every row first.
  - Result: `{ "rows": [[<tagged value>, ...], ...], "freshness": { "revision": string, "reservations": [{ "kind": string, "subject": string, "detail": string }, ...] } }`
  - A row cell is tagged because a row is genuinely a union: `{"k": "scalar", "v": ...}`,
    `{"k": "ref", "v": {"type": ..., "key": [...]}}`, or `{"k": "prov", "v": {...}}`.
  - **Rows and the verdict, always.** An empty `reservations` list means fully
    verified; a non-empty one names which inputs were unconfirmed and why.
  - **One query out, one result back.** The unit of access is a whole query, not
    a read, so a rule joining four relations costs one message regardless of how
    many facts answering it touched.
  - The ER never connects to the storage backend: it has no driver, no
    connection and no credentials, and swapping the WM's backend changes nothing
    it sees. The **footprint** of each query — which fact slots were consulted —
    is recorded in the WM and does not cross; its consumer is the memo table,
    which lives on the same side as the store.
  - Reached from an extension through the `IKnowledgeStore` service (see
    [Services](reference/services.md)) rather than by building these params by
    hand; `finecode_knowledge.query.remote.RemoteBackend` adapts that service into
    an ordinary query backend, so a rule body does not know which side executed it.

- `knowledge/records`
  - Params:
    - `refs` (array): entity references, each `{ "type": string, "key": [string, ...] }`.
  - Result: `{ "v": 1, "records": [{ "fields": { "<qualified field>": { "v": ..., "prov": {...} } }, "conflicts": [...] }, ...] }`
  - **The read a query cannot express.** A query names predicates, so it asks
    about fields it knows the names of. A projection rendering *everything* known
    about an entity cannot, because extensions may declare fields on entity types
    they did not define — enumerating what the caller knew about would silently
    drop exactly those.
  - **The reply is positional and complete.** One record per ref, in order; an
    entity with no facts yields an empty record rather than being omitted, so the
    reply's length never depends on the store's contents.
  - **Batched on purpose.** `refs` is a list so a projection over forty entities
    costs one message rather than forty — the same granularity argument that makes
    `knowledge/query` take a whole query.
  - Like `knowledge/query`, the read is recorded in a footprint on the WM side.
    That is the reason this method exists at all rather than an extension holding
    its own copy of the store: a read the WM never sees contributes nothing to
    what it knows an answer depended on, so nothing resting on it can be
    invalidated when the world moves.

**Notifications**

- `$/progress`
  - Params: `{ "token": <token>, "value": "<stringified JSON partial result>" }`
  - The `token` must match `partialResultToken` from `actions/run` or
    `actions/runHandlers`.
  - `value` is a JSON string produced by the ER from a partial run result.
  - When `$/progress` is used to deliver results, the final `actions/run` or
    `actions/runHandlers` response must have `status: "streamed"` and empty
    `result_by_format`. See result (streamed) entries above.
  - Delegated progress (WM → parent ER): when a delegated call carries a
    `progressToken` (see `finecode/runActionInProject` above), the WM forwards
    the child run's progress values on the parent's token. `value` is the
    child's progress value object (with `type` `begin`/`report`/`end`); the
    parent ER maps it to a report per the table in
    `docs/guides/designing-actions-reference.md` §Progress. Values arriving for
    unknown tokens (late, or after a release) are dropped silently.

- `er/logRecords` (ADR-0049)
  - Params: `{ "records": [{ "timestamp": number, "level": string, "group": string, "message": string }, ...] }`
  - Sent by the ER only while forwarding is enabled (see
    `finecodeRunner/updateLogging` above); otherwise the ER never sends this
    notification. Records are raw/unredacted — **redaction happens at the WM
    boundary**, not in the ER — and are unbatched (one send per emitted
    record); the WM re-batches before relaying to subscribed clients as
    `server/logRecords` (see `docs/wm-protocol.md`), tagging each record's
    `source` as `"runner:<env>@<project>"`.

### WM obligation: bridging both delivery modes to callers

When the WM runs an action on behalf of a caller that supplied a
`partialResultToken` (via `actions/runBatch`), it must forward the result to
the caller as `actions/partialResult` notifications regardless of which ER
delivery mode was used:

- **Streaming mode** (`status: "streamed"`, empty `resultByFormat`): each
  `$/progress` notification is forwarded as an `actions/partialResult`
  notification as it arrives.  The final RPC response carries no result data.

- **Direct result mode** (`status: "success"`, non-empty `resultByFormat`):
  no `$/progress` notifications are sent by the ER.  After the RPC call
  completes the WM must emit a single `actions/partialResult` notification
  containing the `resultByFormat` from the RPC response.

The two modes are mutually exclusive: a handler that uses `$/progress`
produces an empty `resultByFormat`; one that does not produces a non-empty
`resultByFormat`.  The WM detects which mode was used by checking whether any
`$/progress` notifications arrived before the RPC call returned.

## Multi-Env Action Orchestration

When an action's handlers span more than one env, the WM cannot delegate the
whole run to a single ER via `actions/run`. Instead the WM becomes the
orchestrator and drives execution using `actions/runHandlers`.

### Sequential mode (default)

The WM groups the action's handlers into **consecutive same-env segments**:

```text
handlers:  [h1/env1, h2/env1, h3/env1, h4/env2]
segments:  [(env1, [h1, h2, h3]), (env2, [h4])]

handlers:  [h1/env1, h2/env2, h3/env1]
segments:  [(env1, [h1]), (env2, [h2]), (env1, [h3])]
```

Execution:

1. WM calls `actions/runHandlers` for segment 1 with `previousResult: null` and
   `previousContext: null`.
2. For each subsequent segment, WM calls `actions/runHandlers` on that segment's
   ER with `previousResult` set to the `result` returned by the previous call,
   and `previousContext` set to the `context` returned by the previous call.
   The ER reconstructs `previousResult` as `context.current_result` and
   `previousContext` as `context.state` before `context.init()` is called.
3. If any call returns `status: "stopped"`, WM stops the chain and returns that
   result to the caller.
4. `resultFormats` is passed only in the final segment's options — earlier
   segments return `resultByFormat: {}` to avoid unnecessary serialization.
5. WM assembles the final response from the last segment's `result` and
   `resultByFormat`.

### Concurrent mode

The WM groups handlers by env (order within an env does not matter for
concurrent execution):

```text
handlers:  [h1/env1, h2/env2, h3/env1]
groups:    [(env1, [h1, h3]), (env2, [h2])]
```

Execution:

1. WM dispatches `actions/runHandlers` to all env groups in parallel, all with
   `previousResult: null` and `previousContext: null` — parallel groups have no
   linear context chain to thread.
2. WM collects all `result` objects from the parallel calls.
3. WM calls `actions/mergeResults` on any available ER for the action, passing
   the collected `result` objects.
4. The merged result and its formatted representation form the final response.

### Single-env actions

When all handlers are in the same env, the WM uses `actions/run` — a single
delegated call where the ER manages handler sequencing internally.
`actions/runHandlers` is only used when handlers span multiple envs.

### `runId` continuity

The WM generates a single `runId` for the whole logical action run and passes it
in every `actions/run` and `actions/runHandlers` call's options. Each ER emits
WAL events tagged with that ID for the handler(s) it executes, so traces can be
correlated across envs for the same logical run.

It is not only a correlation id. The WM keys its register of in-flight runs by
it (ADR-0079), and an ER names it back in two places:

- `finecode/elicit`, so the WM can put the question to the client that started
  *that run*. The project the asking runner serves cannot identify a run — two
  clients may be running the same project at the same moment — so an elicit
  without a `runId` is answered "unavailable" rather than guessed at (ADR-0082).
- `finecode/elicitAvailability`, where it names the *asking* run. The answer
  comes from the same facts, for the same run, and sends nothing.
- `finecode/runActionInProject` and `finecode/runActionInWorkspace`, where it
  names the *calling* run. The nested run the WM starts gets an id of its own
  and inherits the caller's originating client, so a question asked from inside
  it reaches the same person however many hops down it is.

An ER that omits it from a run request is refused: the id is required, and a
missing one is a protocol error rather than a defaulted value.

## Error Handling and Cancellation

- JSON-RPC errors are used for protocol-level failures.
- Command-level errors are returned via `{ "error": "..." }` in command results.
- WM cancels in-flight requests by sending `$/cancelRequest` with the request id.
- The ER sends `$/cancelRequest` when it stops waiting for `finecode/elicit` or one of the two run methods. For any other ER -> WM request the WM keeps serving the request after the ER stops waiting; a `$/cancelRequest` for one is ignored with a debug log. An older WM that registered no hook for the run methods ignores the cancel too: the child runs to its end, the parent waits at most 5.0 s, and the call reads unconfirmed. `subtreeStopped` and the run-method cancel are same-version rules (ADR-0159).
- Every `actions/run` and `actions/runHandlers` result and cancelled error carries `subtreeStopped`, including the `{"error": ...}` returns. `subtreeStopped` never reaches a client: `actions/run` (streamed and non-streamed) and `actions/runBatch` results returned to a client, LSP or MCP never contain the key.

## Document Sync Notes

WM forwards open-file events to ER so actions can operate on in-memory document
state. ER may send `workspace/applyEdit` when handlers modify files; WM applies
these edits via its active client when possible.

The WM keeps its own copy of every open document's content
(`WorkspaceContext.opened_documents`), populated from the IDE's own
`didOpen`/`didChange` notifications, so it can re-supply the current content
to an ER that (re)starts while the file is already open — see
`send_opened_files` in `runner_manager.py`. That re-supply must forward the
already-known `text` unchanged; reconstructing a fresh `TextDocumentInfo`
without `text` would make the ER seed an empty buffer instead of the real
content on every runner restart.
