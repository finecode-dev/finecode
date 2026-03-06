# FineCode API Server Protocol

The FineCode API server is a TCP JSON-RPC 2.0 service that manages the workspace state
(projects, configs, extension runners). Any client — LSP server, MCP server, or CLI — can
connect to it.

## Transport

- TCP on `127.0.0.1`, random free port
- Content-Length framing (same as LSP): `Content-Length: N\r\n\r\n{json_body}`
- Discovery: port written to `.venvs/dev_workspace/cache/finecode/api_port`
- Auto-stops when the last client disconnects (after a 5s grace period) or if no client connects 30 seconds after start of API Server

## JSON-RPC 2.0

**Request** (client -> server, expects response):

```json
{"jsonrpc": "2.0", "id": 1, "method": "workspace/listProjects", "params": {...}}
```

**Response** (success):

```json
{"jsonrpc": "2.0", "id": 1, "result": {...}}
```

**Response** (error):

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "Not yet implemented"}}
```

**Notification** (no `id` field, no response expected):

```json
{"jsonrpc": "2.0", "method": "documents/opened", "params": {...}}
```

Method names use LSP-style domain prefixes: `workspace/`, `actions/`, `documents/`,
`runners/`, `server/`.

---

## Methods

### `workspace/` — Workspace & Project Discovery

#### `workspace/listProjects`

List all projects in the workspace.

- **Type:** request
- **Clients:** LSP, MCP, CLI
- **Status:** implemented

**Params:** `{}`

**Result:**

```json
[
  {"name": "finecode", "path": "/path/to/finecode", "status": "CONFIG_VALID"}
]
```

---

#### `workspace/findProjectForFile`

Determine which project (if any) contains a given file.  The LSP server uses
this helper when a document diagnostic request arrives; it avoids having to
list all projects and perform path comparisons itself.

- **Type:** request
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"file_path": "/abs/path/to/some/file.py"}
```

**Result:**

```json
{"project": "project_name"}   # or {"project": null} if not found
```

The server internally calls
:func:`finecode.api_server.services.run_service.find_action_project` with
``action_name="lint"`` and returns the corresponding project name.

---

#### `workspace/addDir`

Add a workspace directory. Discovers projects, reads configs, collects actions,
and starts extension runners.

> **Design note:** Ideally, workspace directories would be a single shared
> definition independent of which client connects (LSP, MCP, CLI). Currently,
> each client calls `workspace/addDir` with its own working directory, so the
> API server's workspace is the union of what clients have registered. This is a
> known simplification — a future improvement would introduce a workspace
> configuration file or a dedicated workspace management layer so that the set
> of directories is not environment-specific.

- **Type:** request
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"dir_path": "/path/to/workspace"}
```

**Result:**

```json
{
  "projects": [
    {"name": "my_project", "path": "/path/to/my_project", "status": "CONFIG_VALID"}
  ]
}
```

---

#### `workspace/removeDir`

Remove a workspace directory. Stops runners for affected projects and removes them
from context.

- **Type:** request
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"dir_path": "/path/to/workspace"}
```

**Result:** `{}`

---

### `actions/` — Action Discovery & Execution

#### `actions/list`

List available actions, optionally filtered by project. Flat listing for
programmatic use by MCP agents and CLI.

- **Type:** request
- **Clients:** MCP, CLI
- **Status:** stub

**Params:**

```json
{"project": "finecode"}
```

All fields optional. If `project` is omitted, returns actions from all projects.

**Result:**

```json
{
  "actions": [
    {
      "name": "lint",
      "source": "finecode_extension_api.actions.lint.LintAction",
      "project": "finecode",
      "handlers": [
        {"name": "ruff", "source": "fine_python_ruff.RuffLintFilesHandler", "env": "runtime"}
      ]
    }
  ]
}
```

---

#### `actions/getTree`

Get the hierarchical action tree for IDE sidebar display.

- **Type:** request
- **Clients:** LSP
- **Status:** stub

**Params:** `{}`

**Result:**

```json
{
  "nodes": [
    {
      "node_id": "ws_dir_0",
      "name": "/path/to/workspace",
      "node_type": 0,
      "status": "ok",
      "subnodes": [
        {
          "node_id": "project_0",
          "name": "finecode",
          "node_type": 1,
          "status": "ok",
          "subnodes": []
        }
      ]
    }
  ]
}
```

`node_type` values: 0=DIRECTORY, 1=PROJECT, 2=ACTION, 3=ACTION_GROUP, 4=PRESET,
5=ENV_GROUP, 6=ENV

---

#### `actions/run`

Execute a single action on a project.

- **Type:** request
- **Clients:** LSP, MCP, CLI
- **Status:** stub

**Params:**

```json
{
  "action": "lint",
  "project": "finecode",
  "params": {"file_paths": ["/path/to/file.py"]},
  "config_overrides": {"ruff": {"line_length": 120}},
  "options": {
    "result_formats": ["json", "string"],
    "trigger": "user",
    "dev_env": "ide"
  }
}
```

Required: `action`, `project`. All other fields optional.

`trigger` values: `"user"`, `"system"`, `"unknown"` (default: `"unknown"`)

`dev_env` values: `"ide"`, `"cli"`, `"ai"`, `"precommit"`, `"cicd"` (default: `"cli"`)

**Result:**

```json
{
  "result_by_format": {
    "json": {"messages": {"file.py": []}},
    "string": "All checks passed."
  },
  "return_code": 0
}
```

---

#### `actions/runBatch`

Execute multiple actions across multiple projects. Used for batch operations.

- **Type:** request
- **Clients:** CLI, MCP
- **Status:** stub

**Params:**

```json
{
  "actions": ["lint", "check_formatting"],
  "projects": ["finecode", "finecode_extension_api"],
  "params": {},
  "config_overrides": {},
  "options": {
    "concurrent": false,
    "result_formats": ["json", "string"],
    "trigger": "user",
    "dev_env": "cli"
  }
}
```

Required: `actions`. If `projects` is omitted, runs on all projects that have the
requested actions.

**Result:**

```json
{
  "results": {
    "/path/to/finecode": {
      "lint": {"result_by_format": {...}, "return_code": 0},
      "check_formatting": {"result_by_format": {...}, "return_code": 0}
    }
  },
  "return_code": 0
}
```

`return_code` at the top level is the bitwise OR of all individual return codes.

---

#### `actions/runWithPartialResults`

Execute an action with streaming partial results. The server sends
`actions/partialResult` notifications during execution.

- **Type:** request
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{
  "action": "lint",
  "project": "finecode",
  "params": {"file_paths": ["/path/to/file.py"]},
  "partial_result_token": "diag_1",
  "options": {
    "trigger": "system",
    "dev_env": "ide"
  }
}
```

Required: `action`, `project`, `partial_result_token`.

**Result:** Same as `actions/run` (the final aggregated result).

During execution, the server sends `actions/partialResult` notifications (see below).

---

#### `actions/reload`

Hot-reload handler code for an action without restarting runners.

- **Type:** request
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{"project": "finecode", "action": "lint"}
```

**Result:** `{}`

---

### `documents/` — Document Sync

Notifications from the LSP client to keep the API server (and extension runners)
informed about open documents. These are fire-and-forget (no response).

#### `documents/opened`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{"uri": "file:///path/to/file.py", "version": 1}
```

---

#### `documents/closed`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{"uri": "file:///path/to/file.py"}
```

---

#### `documents/changed`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{
  "uri": "file:///path/to/file.py",
  "version": 2,
  "content_changes": [
    {
      "range": {
        "start": {"line": 5, "character": 0},
        "end": {"line": 5, "character": 10}
      },
      "text": "new_text"
    }
  ]
}
```

---

### `runners/` — Runner Management

#### `runners/list`

List extension runners and their statuses.

- **Type:** request
- **Clients:** LSP, MCP
- **Status:** stub

**Params:**

```json
{"project": "finecode"}
```

`project` is optional. If omitted, returns runners for all projects.

**Result:**

```json
{
  "runners": [
    {
      "project": "finecode",
      "env": "runtime",
      "status": "RUNNING",
      "readable_id": "finecode::runtime"
    }
  ]
}
```

`status` values: `"NO_VENV"`, `"INITIALIZING"`, `"FAILED"`, `"RUNNING"`, `"EXITED"`

---

#### `runners/restart`

Restart an extension runner. Optionally start in debug mode.

- **Type:** request
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{"project": "finecode", "env": "runtime", "debug": false}
```

`debug` is optional, defaults to `false`.

**Result:** `{}`

---

### `server/` — Server Lifecycle & Notifications

#### `server/shutdown`

Explicitly shut down the API server.

- **Type:** request
- **Clients:** any
- **Status:** stub

**Params:** `{}`

**Result:** `{}`

---

### Server -> Client Notifications

These are sent by the API server to connected clients. Clients must implement
a background reader to receive them.

#### `actions/partialResult`

Sent during `actions/runWithPartialResults` execution as results stream in.

- **Type:** notification (server -> client)
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{"token": "diag_1", "value": {"messages": {"file.py": [...]}}}
```

`token` matches the `partial_result_token` from the originating request.

> **Note:** Notifications are delivered only to the client connection that
> initiated the corresponding `actions/runWithPartialResults` request.  The
> API server does **not** broadcast these messages to every connected client.

---

#### `actions/treeChanged`

Sent when a project's status or actions change (e.g., after config reload,
runner start/stop).

- **Type:** notification (server -> client)
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{
  "node": {
    "node_id": "project_0",
    "name": "finecode",
    "node_type": 1,
    "status": "ok",
    "subnodes": []
  }
}
```

---

#### `server/userMessage`

Broadcast user-facing messages (errors, warnings, info) to connected clients.

- **Type:** notification (server -> client)
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"message": "Runner failed to start", "type": "ERROR"}
```

`type` values: `"INFO"`, `"WARNING"`, `"ERROR"`
