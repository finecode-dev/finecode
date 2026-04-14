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
5. WM sends `finecodeRunner/resolveActionSources` to get canonical action source paths.
   - ER returns a sparse map of `configSource → canonicalSource` for actions whose
     declared config path differs from the fully qualified runtime path.
   - WM stores these on its `Action` domain objects before the runner is considered ready.
6. On shutdown: WM sends `shutdown` then `exit`.

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

- `actions/getPayloadSchemas`
  - Params: `{}`
  - Result: `{ action_name: JSON Schema fragment | null }`
  - Returns a payload schema for every action currently known to the runner.
    Each schema has `properties` (field name → JSON Schema type object) and
    `required` (list of field names without defaults).
    `null` means the action class could not be imported.

- `actions/mergeResults`
  - Params: `{ "actionName": string, "results": list }`
  - Result: `{ "merged": ... }` or `{ "error": "..." }`

- `actions/reload`
  - Params: `{ "actionName": string }`
  - Result: `{}`

- `finecodeRunner/resolveActionSources`
  - Params: `{}` (no params)
  - Result: sparse map of `{ "<configSource>": "<canonicalSource>", ... }` for actions
    whose declared config path differs from the fully qualified runtime path.
    Only entries where the two differ are included.
    Example: `{ "myext.LintAction": "myext.actions.lint.LintAction" }`
  - Called by the WM after `finecodeRunner/updateConfig` completes to store canonical
    sources on its `Action` domain objects before the runner is considered ready.
    The WM uses `canonical_source` as the primary identifier in all subsequent action
    lookups; `source` (from config) is the fallback for actions whose class could not
    be imported in this env.
  - Actions where `cls.__module__ + "." + cls.__qualname__ == config source` are
    omitted (no mapping needed — the config source is already canonical).

- `actions/resolveSource`
  - Params: `{ "source": string }` — an arbitrary import-path alias to resolve.
  - Result: `{ "canonicalSource": string }` — the fully qualified class path
    (`cls.__module__ + "." + cls.__qualname__`).
  - Raises a JSON-RPC error if the alias cannot be imported or resolved.
  - Used during action lookup when a caller provides an alias not already known
    from `finecodeRunner/resolveActionSources` (full ADR-0019 support).

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
      `finecodeRunner/resolveActionSources`; a re-exported path will not match and the
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
  - The `token` must match `partial_result_token` from `actions/run`.
  - `value` is a JSON string produced by the ER from a partial run result.
  - When `$/progress` is used to deliver results, the final `actions/run` response
    must have `status: "streamed"` and empty `result_by_format`. See `actions/run`
    result (streamed) above.

## Error Handling and Cancellation

- JSON-RPC errors are used for protocol-level failures.
- Command-level errors are returned via `{ "error": "..." }` in command results.
- WM cancels in-flight requests by sending `$/cancelRequest` with the request id.

## Document Sync Notes

WM forwards open-file events to ER so actions can operate on in-memory document
state. ER may send `workspace/applyEdit` when handlers modify files; WM applies
these edits via its active client when possible.
