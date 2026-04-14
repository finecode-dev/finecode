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
- LSP-style framing over stdio: `Content-Length: N\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n{json}`
- WM spawns ER processes with:
  - `python -m finecode_extension_runner.cli start --project-path=... --env-name=...`
  - `--debug` enables a debugpy attach flow before WM connects
- All parameter object keys use camelCase.

## Lifecycle

1. WM starts the ER process (per project + env).
2. WM sends `initialize` and waits for the ER response.
3. WM sends `initialized`.
4. WM sends `finecodeRunner/updateConfig` to bootstrap handlers and services.
   - ER processes it and returns `{}`.
5. WM sends `finecodeRunner/resolveActionMeta` to get action meta info.
   - ER returns a complete map of `configSource → { canonical_source, runs_concurrently }` for every action
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
  - Standard LSP initialize request.
  - Example params (trimmed):
    ```json
    {
      "processId": 12345,
      "clientInfo": {"name": "FineCode_WorkspaceManager", "version": "0.1.0"},
      "capabilities": {},
      "workspaceFolders": [{"uri": "file:///path/to/project", "name": "project"}],
      "trace": "verbose"
    }
    ```

- `shutdown`
  - Standard LSP shutdown request.

- `finecodeRunner/updateConfig`
  - Params: `{ "workingDir": string, "projectName": string, "projectDefPath": string, "config": object }`
  - Config shape (top-level):
    - `actions`: list of action objects (`name`, `handlers`, `source`, `config`)
    - `action_handler_configs`: map of handler source → config
    - `services`: list of service declarations (optional)
    - `handlers_to_initialize`: map of action name → handler names (optional)
  - Result: `{}` (empty object)

- `finecodeRunner/getInfo`
  - Params: `{}`
  - Result: `{ "logFilePath": "/abs/path/to/runner.log" | null }`
  - Returns runtime information about the runner. Currently reports the path
    to the runner's log file, or `null` if logging to a file is not configured.

- `actions/run`
  - Params: `{ "actionName": string, "params": object, "options": object | null }`
  - Options keys (camelCase):
    - `meta`: `{ "trigger": "user|system|unknown", "devEnv": "ide|cli|ai|git_hook|ci", "orchestrationDepth": int }`
      - `orchestrationDepth`: cross-boundary hop counter, defaults to `0`. The ER propagates it unchanged via `RunActionMeta.orchestration_depth`.
    - `partialResultToken`: `int | string` (used to correlate `$/progress`)
    - `resultFormats`: `["json", "string"]` (defaults to `["json"]`)
  - Result (success):
    ```json
    {
      "status": "success",
      "result_by_format": "{\"json\": {\"...\": \"...\"}}",
      "return_code": 0
    }
    ```
  - Result (streamed): used when `partialResultToken` was provided and all
    results were delivered via `$/progress` notifications. The final response
    is an explicit completion signal — `result_by_format` is intentionally empty.
    The WM treats this as a valid completion; an empty `result_by_format` with
    any other status is a protocol error.
    ```json
    {
      "status": "streamed",
      "result_by_format": "{}",
      "return_code": 0
    }
    ```
  - Result (stopped):
    ```json
    {
      "status": "stopped",
      "result_by_format": "{\"json\": {\"...\": \"...\"}}",
      "return_code": 1
    }
    ```
  - Result (error):
    ```json
    {"error": "message"}
    ```
  - Note: `result_by_format` is a JSON-encoded string (not a nested object) —
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
    - `options` (object | null): same keys as `actions/run`. `resultFormats`
      should be omitted (or `[]`) for intermediate segments and non-empty only
      for the final segment of a run.
  - Result (success):
    ```json
    {
      "status": "success",
      "result": {"<resultField>": "..."},
      "resultByFormat": {"json": {"...": "..."}, "string": "..."},
      "returnCode": 0
    }
    ```
    - `result`: serialized `RunActionResult` after all specified handlers ran
      (`dataclasses.asdict`). Pass as `previousResult` to the next segment's
      `actions/runHandlers` call.
    - `resultByFormat`: formatted results in the requested formats; `{}` when
      `resultFormats` was empty in options.
  - Result (streamed): used when `partialResultToken` was provided and all
    results were delivered via `$/progress`. `result` is still populated for
    context chaining.
    ```json
    {
      "status": "streamed",
      "result": {"<resultField>": "..."},
      "resultByFormat": {},
      "returnCode": 0
    }
    ```
  - Result (stopped):
    ```json
    {
      "status": "stopped",
      "result": {"<resultField>": "..."},
      "resultByFormat": {"json": {"...": "..."}},
      "returnCode": 1
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
  - Params: `{ "actionName": string, "results": list }`
  - `results`: list of serialized `RunActionResult` objects (`dataclasses.asdict`),
    one per concurrent segment or handler. Used by the WM after a concurrent
    multi-env run to merge the per-env results into a single final result.
  - Result: `{ "merged": <serialized RunActionResult> }` or `{ "error": "..." }`

- `actions/reload`
  - Params: `{ "actionName": string }`
  - Result: `{}`

- `finecodeRunner/resolveActionMeta`
  - Params: `{}` (no params)
  - Result: complete map of `{ "<configSource>": { "canonical_source": string, "runs_concurrently": bool }, ... }` for every
    action whose class can be imported in this env.  Actions that fail to import are
    omitted entirely.
    Example: `{ "myext.LintAction": { "canonical_source": "myext.actions.lint.LintAction", "runs_concurrently": true } }`
  - Called by the WM after `finecodeRunner/updateConfig` completes to store canonical
    sources and execution modes on its `Action` domain objects before the runner is
    considered ready.  The WM uses `canonical_source` as the primary identifier in all
    subsequent action lookups.  Actions absent from the response (import failure) keep
    `canonical_source = None` until another runner for the same project resolves them.

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
- `textDocument/didChange`
- `textDocument/didClose`
- `$/cancelRequest`
  - Sent by WM when an in-flight request should be cancelled.

### ER -> WM

**Requests**

- `workspace/applyEdit`
  - Standard LSP request for applying workspace edits.
  - WM forwards this to its active client (IDE) if available.

- `projects/getRawConfig`
  - Params: `{ "projectDefPath": "/abs/path/to/project/finecode.toml" }`
  - Result: `{ "config": "<stringified JSON config>" }`
  - Used by ER during `finecodeRunner/updateConfig` to resolve project config.

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
  - Result: `{ "result": <json result object>, "returnCode": 0|1 }`
  - Runs the action at project scope (all env-runners of the ER's own project). WM enforces `OrchestrationPolicy.max_recursion_depth` before dispatching.

- `finecode/runActionInWorkspace`
  - Params:
    - `actionSource` (string): **fully qualified** import path of the action class —
      same constraint as `finecode/runActionInProject` above.
    - `payload` (object): serialized action payload
    - `meta` (object): `{ "trigger": string, "devEnv": string, "orchestrationDepth": int }`
    - `projectPaths` (list[string] | null): explicit POSIX project paths, or `null` for all projects that declare the action
    - `concurrently` (boolean, default `true`): run projects concurrently.
  - Result: `{ "resultsByProject": { "<posix path>": <json result>, ... } }`
  - Fans out the action across the specified projects (or all projects that declare it). WM enforces `OrchestrationPolicy.max_project_fanout` before dispatching.

**Notifications**

- `$/progress`
  - Params: `{ "token": <token>, "value": "<stringified JSON partial result>" }`
  - The `token` must match `partialResultToken` from `actions/run` or
    `actions/runHandlers`.
  - `value` is a JSON string produced by the ER from a partial run result.
  - When `$/progress` is used to deliver results, the final `actions/run` or
    `actions/runHandlers` response must have `status: "streamed"` and empty
    `result_by_format`. See result (streamed) entries above.

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

1. WM calls `actions/runHandlers` for segment 1 with `previousResult: null`.
2. For each subsequent segment, WM calls `actions/runHandlers` on that segment's
   ER with `previousResult` set to the `result` returned by the previous call.
   The ER reconstructs this as `context.current_result` before the first handler
   in the segment runs.
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
   `previousResult: null`.
2. WM collects all `result` objects from the parallel calls.
3. WM calls `actions/mergeResults` on any available ER for the action, passing
   the collected `result` objects.
4. The merged result and its formatted representation form the final response.

### Single-env actions

When all handlers are in the same env, the WM uses `actions/run` — a single
delegated call where the ER manages handler sequencing internally.
`actions/runHandlers` is only used when handlers span multiple envs.

### `walRunId` continuity

The WM generates a single `walRunId` for the whole logical action run and
passes it in every `actions/runHandlers` call's options. Each ER emits WAL
events tagged with that ID for the handler(s) it executes, so traces can be
correlated across envs for the same logical run.

## Error Handling and Cancellation

- JSON-RPC errors are used for protocol-level failures.
- Command-level errors are returned via `{ "error": "..." }` in command results.
- WM cancels in-flight requests by sending `$/cancelRequest` with the request id.

## Document Sync Notes

WM forwards open-file events to ER so actions can operate on in-memory document
state. ER may send `workspace/applyEdit` when handlers modify files; WM applies
these edits via its active client when possible.
