# FineCode WM-ER Protocol

This document describes the communication protocol between the FineCode Workspace
Manager (WM) and Extension Runners (ER). WM is the JSON-RPC client; each ER is a
JSON-RPC server implemented via the LSP stack (`finecode_extension_runner/lsp_server.py`).
The protocol is LSP-shaped with a small set of custom commands.

## Transport

- JSON-RPC 2.0
- LSP-style framing over stdio: `Content-Length: N\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n{json}`
- WM spawns ER processes with:
  - `python -m finecode_extension_runner.cli start --project-path=... --env-name=...`
  - `--debug` enables a debugpy attach flow before WM connects
- Field names are camelCase for standard LSP params, but command arguments are
  passed verbatim (snake_case is common in FineCode payloads).

## Lifecycle

1. WM starts the ER process (per project + env).
2. WM sends `initialize` and waits for the ER response.
3. WM sends `initialized`.
4. WM sends `finecodeRunner/updateConfig` to bootstrap handlers and services.
5. On shutdown: WM sends `shutdown` then `exit`.

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

- `workspace/executeCommand`
  - Used for all FineCode WM → ER commands. The `arguments` array is passed to
    the handler verbatim.

  **Commands**

  - `finecodeRunner/updateConfig`
    - Arguments:
      1. `working_dir` (string path)
      2. `project_name` (string)
      3. `project_def_path` (string path)
      4. `config` (object)
    - Config shape (top-level):
      - `actions`: list of action objects (`name`, `handlers`, `source`, `config`)
      - `action_handler_configs`: map of handler source → config
      - `services`: list of service declarations (optional)
      - `handlers_to_initialize`: map of action name → handler names (optional)
    - Result: `{}` (empty object)

  - `finecodeRunner/getInfo`
    - Arguments: none
    - Result: `{ "logFilePath": "/abs/path/to/runner.log" | null }`
    - Returns runtime information about the runner. Currently reports the path
      to the runner's log file, or `null` if logging to a file is not configured.

  - `actions/run`
    - Arguments:
      1. `action_name` (string)
      2. `params` (object)
      3. `options` (object, optional)
    - Options (snake_case keys are expected):
      - `meta`: `{ "trigger": "user|system|unknown", "dev_env": "ide|cli|ai|precommit|ci" }`
      - `partial_result_token`: `int | string` (used to correlate `$/progress`)
      - `result_formats`: `["json", "string"]` (defaults to `["json"]`)
    - Result (success):
      ```json
      {
        "status": "success",
        "result_by_format": "{\"json\": {\"...\": \"...\"}}",
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
    - Note: `result_by_format` is a JSON string (not a JSON object) due to
      LSP serialization constraints in the runner.

  - `actions/getPayloadSchemas`
    - Arguments: none
    - Result: `{ action_name: JSON Schema fragment | null }`
    - Returns a payload schema for every action currently known to the runner.
      Each schema has `properties` (field name → JSON Schema type object) and
      `required` (list of field names without defaults).
      `null` means the action class could not be imported.

  - `actions/mergeResults`
    - Arguments: `[action_name, results]`
    - Result: `{ "merged": ... }` or `{ "error": "..." }`

  - `actions/reload`
    - Arguments: `[action_name]`
    - Result: `{}`

  - `packages/resolvePath`
    - Arguments: `[package_name]`
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

**Notifications**

- `$/progress`
  - Params: `{ "token": <token>, "value": "<stringified JSON partial result>" }`
  - The `token` must match `partial_result_token` from `actions/run`.
  - `value` is a JSON string produced by the ER from a partial run result.

## Error Handling and Cancellation

- JSON-RPC errors are used for protocol-level failures.
- Command-level errors are returned via `{ "error": "..." }` in command results.
- WM cancels in-flight requests by sending `$/cancelRequest` with the request id.

## Document Sync Notes

WM forwards open-file events to ER so actions can operate on in-memory document
state. ER may send `workspace/applyEdit` when handlers modify files; WM applies
these edits via its active client when possible.
