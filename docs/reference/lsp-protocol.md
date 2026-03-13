# FineCode LSP Client Protocol

This document describes the communication between the FineCode LSP server
(`src/finecode/lsp_server/lsp_server.py`) and LSP clients (currently the
FineCode VSCode extension). It focuses on FineCode-specific behavior and
custom commands; standard LSP features are listed without detailed spec.

## Transport

- JSON-RPC 2.0 over standard LSP framing (`Content-Length: N\r\n\r\n{json}`)
- Transport: stdio (the server starts with `start_io_async()`)

## Lifecycle Notes

- The LSP server waits for the standard `initialized` notification and then
  ensures the WM Server is running and connects to it.
- If WM Server connection fails, FineCode features that depend on WM will fail.
- Normal LSP shutdown closes the WM client connection.
- Use the custom `server/shutdown` request (below) to explicitly stop the WM
  Server when the IDE wants to restart it.

## Standard LSP Features (Implemented)

The server implements the following LSP features (standard behavior; no
FineCode-specific protocol additions):

- `workspace/didChangeWorkspaceFolders`
- `textDocument/didOpen`
- `textDocument/didSave`
- `textDocument/didChange`
- `textDocument/didClose`
- `textDocument/formatting`
- `textDocument/rangeFormatting`
- `textDocument/rangesFormatting`
- `textDocument/codeAction`
- `codeAction/resolve`
- `textDocument/codeLens`
- `codeLens/resolve`
- `textDocument/diagnostic`
- `workspace/diagnostic`
- `textDocument/inlayHint`
- `inlayHint/resolve`
- `shutdown`

## Custom FineCode Commands (`workspace/executeCommand`)

FineCode exposes IDE commands via the standard `workspace/executeCommand` LSP
method. The command names below are advertised by the server and routed to WM
Server APIs. Params are passed as the `params` array of `executeCommand`.

### Action Tree and Projects

- `finecode.getActions`
  - Params: `parent_node_id` (string or `null`)
  - Returns: action tree under the given node

- `finecode.getActionsForPosition`
  - Params: position object (currently ignored)
  - Returns: action tree (currently full tree)

- `finecode.listProjects`
  - Params: none
  - Returns: list of workspace projects

### Action Execution

- `finecode.runAction`
  - Params: `{ "action": str, "project": str, "params"?: object, "options"?: object }`
  - Behavior: forwards to WM `actions/run`

- `finecode.runActionOnFile`
  - Params: `{ "projectPath": "<project>::<action>" }`
  - Behavior:
    - Requests `editor/documentMeta` from the client to get the active file
    - Runs the action with `target=files` for that file

- `finecode.runActionOnProject`
  - Params: `{ "projectPath": "<project>::<action>" }`
  - Behavior: runs the action with `target=project`

### Action/Runner Management

- `finecode.reloadAction`
  - Params: `{ "projectPath": "<project>::<action>" }`
  - Behavior: forwards to WM `actions/reload`

- `finecode.reset`
  - Params: none
  - Behavior: forwards to WM `server/reset`

- `finecode.restartExtensionRunner`
  - Params: `{ "projectPath": "<project>::<env>" }`
  - Behavior: forwards to WM `runners/restart` with `debug=false`

- `finecode.restartAndDebugExtensionRunner`
  - Params: `{ "projectPath": "<project>::<env>" }`
  - Behavior: forwards to WM `runners/restart` with `debug=true`

## Custom LSP Requests

### Client → Server

- `server/shutdown`
  - Params: `{}`
  - Result: `{}`
  - Purpose: explicitly stops the WM Server, then closes the WM client

### Server → Client

- `editor/documentMeta`
  - Params: `{}`
  - Result: document metadata for the active editor
  - Minimum required fields: `uri` with a valid filesystem path

- `ide/startDebugging`
  - Params: debug configuration object (VSCode `debugpy` attach configuration)
  - Result: any value (used for logging only)
  - Purpose: starts a debug session when a runner is restarted with debug
