# FineCode WM Server Protocol

The FineCode Workspace Manager Server (WM Server) is a TCP JSON-RPC 2.0 service that manages the workspace state
(projects, configs, extension runners). Any client — LSP server, MCP server, or CLI — can
connect to it.

## Transport

- TCP on `127.0.0.1`, random free port
- Content-Length framing (same as LSP): `Content-Length: N\r\n\r\n{json_body}`
- Discovery: port written to `.venvs/dev_workspace/cache/finecode/wm_port`
- Auto-stops when the last client disconnects (after a 30s grace period by default, configurable via `--disconnect-timeout`) or if no client connects within 30 seconds after WM Server startup

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
:func:`finecode.wm_server.services.run_service.find_action_project` with
``action_name="lint"`` and returns the corresponding project name.

---

#### `workspace/addDir`

Add a workspace directory. Discovers projects, reads configs, collects actions,
and optionally starts extension runners.

> **Design note:** Ideally, workspace directories would be a single shared
> definition independent of which client connects (LSP, MCP, CLI). Currently,
> each client calls `workspace/addDir` with its own working directory, so the
> WM Server's workspace is the union of what clients have registered. This is a
> known simplification — a future improvement would introduce a workspace
> configuration file or a dedicated workspace management layer so that the set
> of directories is not environment-specific.

- **Type:** request
- **Clients:** LSP, CLI
- **Status:** implemented

**Params:**

```json
{"dir_path": "/path/to/workspace", "start_runners": true, "projects": ["my_project"]}
```

`start_runners` is optional (default: `true`). When `false`, the server reads
configs and collects actions without starting any extension runners. Use this
when runner environments may not exist yet (e.g. before running `prepare-envs`).
Actions are still available in the result so clients can validate the workspace.

`projects` is optional. When provided, only the listed projects (by name) will
be config-initialized and have their runners started. All other projects in the
directory are still discovered (added to workspace state) but skipped for
initialization. This avoids the cost of reading configs and spawning runner
processes for projects that are not needed.

Calling `workspace/addDir` again for the same `dir_path` with a different
`projects` filter (or with `projects` omitted) will initialize the previously
skipped projects — the call is **incremental**, not idempotent. Only projects
that have not yet been config-initialized are processed on each call. This makes
it safe to issue a filtered call followed by an unfiltered one.

**Result:**

```json
{
  "projects": [
    {"name": "my_project", "path": "/path/to/my_project", "status": "CONFIG_VALID"}
  ]
}
```

The `projects` list contains only the projects initialized during **this call**,
not all projects in the workspace.

`status` values: `"CONFIG_VALID"`, `"CONFIG_INVALID"`

---

#### `workspace/startRunners`

Start extension runners for all (or specified) projects. Only starts runners
that are not already running — complements existing runner state rather than
replacing it. Also resolves preset-defined actions so that `actions/run` can
find them.

- **Type:** request
- **Clients:** CLI
- **Status:** implemented

**Params:**

```json
{"projects": ["my_project"]}
```

`projects` is optional. If omitted, starts runners for all projects.

**Result:** `{}`

---

#### `workspace/setConfigOverrides`

Set persistent handler config overrides on the server. Overrides are stored for
the lifetime of the server and applied to all subsequent action runs — unlike the
`config_overrides` field that was previously accepted by `actions/runBatch`, which
required runners to be stopped first.

- **Type:** request
- **Clients:** CLI
- **Status:** implemented

**Params:**

```json
{
  "overrides": {
    "lint": {
      "ruff": {"line_length": 120},
      "": {"some_action_level_param": "value"}
    }
  }
}
```

`overrides` format: `{action_name: {handler_name_or_"": {param: value}}}`.
The empty-string key `""` means the override applies to all handlers of that action.

**Result:** `{}`

**Behaviour:**

- Overrides are stored in the server's workspace context and applied to all
  subsequent action runs.
- If extension runners are already running, they receive a config update
  immediately; initialized handlers are dropped and will be re-initialized with
  the new config on the next run.
- The CLI `run` command sends this message **before** `workspace/addDir` in
  standalone mode (`--own-server`), so runners always start with the correct
  config and no update push is required.
- Config overrides are **not supported** in `--shared-server` mode: the CLI
  will print a warning and ignore them.
- Calling this method again replaces the previous overrides entirely.

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

#### `workspace/getProjectRawConfig`

Return the fully resolved raw configuration for a project, as stored in the
workspace context after config reading and preset resolution.

- **Type:** request
- **Clients:** CLI
- **Status:** implemented

**Params:**

```json
{"project": "my_project"}
```

**Result:**

```json
{
  "raw_config": {
    "tool": { "finecode": { ... } },
    ...
  }
}
```

**Errors:**

- `project` is required — returns a JSON-RPC error if omitted.
- Project not found — returns a JSON-RPC error if no project with the given name
  exists in the workspace context.

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

#### `actions/getPayloadSchemas`

Return payload schemas for the specified actions in a project. Used by the MCP
server to build accurate `inputSchema` entries for each tool.

- **Type:** request
- **Clients:** MCP
- **Status:** implemented

**Params:**

```json
{ "project": "/abs/path/to/project", "action_names": ["lint", "format"] }
```

**Result:**

```json
{
  "schemas": {
    "lint":   { "properties": { "file_paths": {"type": "array", "items": {"type": "string"}}, "target": {"type": "string", "enum": ["project", "files"]} }, "required": [] },
    "format": { "properties": { "save": {"type": "boolean"}, "target": {"type": "string"}, "file_paths": {"type": "array", "items": {"type": "string"}} }, "required": [] }
  }
}
```

Each schema value is `null` for actions whose class cannot be imported in any
Extension Runner. Schemas are cached per project in the WM and invalidated
whenever runner config is updated.

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
  "options": {
    "result_formats": ["json", "string"],
    "trigger": "user",
    "dev_env": "ide"
  }
}
```

Required: `action`, `project`. All other fields optional.

`trigger` values: `"user"`, `"system"`, `"unknown"` (default: `"unknown"`)

`dev_env` values: `"ide"`, `"cli"`, `"ai"`, `"precommit"`, `"ci"` (default: `"cli"`)

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
    "result_formats": ["json", "string"],
    "trigger": "system",
    "dev_env": "ide"
  }
}
```

Required: `action`, `project`, `partial_result_token`.

Supported `result_formats`: `"json"`, `"string"`, etc. (same as `actions/run`).

**Result:** Same as `actions/run` (the final aggregated result).

During execution, the server sends `actions/partialResult` notifications (see below).

> **Guarantee:** The WM Server always delivers results via `actions/partialResult`
> notifications, even when an extension runner does not stream incrementally (i.e.
> it collects all results internally and returns them as a single final response).
> In that case the server emits the final result as a partial result notification
> before returning the aggregated response.  Clients can therefore rely solely on
> `actions/partialResult` notifications to receive results and safely ignore the
> response body of this request.

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

Notifications from the LSP client to keep the WM Server (and extension runners)
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

#### `runners/checkEnv`

Check whether the named environment for a project is valid (i.e. the virtualenv
exists and its dependencies are correctly installed).

- **Type:** request
- **Clients:** CLI
- **Status:** implemented

**Params:**

```json
{"project": "my_project", "env_name": "dev_workspace"}
```

**Result:**

```json
{"valid": true}
```

---

#### `runners/removeEnv`

Remove the named environment for a project. If a runner is currently using that
environment, it is stopped first.

- **Type:** request
- **Clients:** CLI
- **Status:** implemented

**Params:**

```json
{"project": "my_project", "env_name": "dev_workspace"}
```

**Result:** `{}`

---

### `server/` — Server Lifecycle & Notifications

#### `server/getInfo`

Return static information about the running WM Server instance.

- **Type:** request
- **Clients:** LSP, MCP, CLI
- **Status:** implemented

**Params:** `{}`

**Result:**

```json
{
  "log_file_path": "/abs/path/to/.venvs/dev_workspace/logs/wm_server/wm_server.log"
}
```

`log_file_path` is the absolute path to the WM Server's log file for the current process.
Clients can log or display this path so the user can open the file directly when troubleshooting.

---

#### `server/shutdown`

Explicitly shut down the WM Server. Clients can use this when they intentionally
want the WM to stop or restart, rather than waiting for disconnect-timeout
auto-shutdown.

- **Type:** request
- **Clients:** any
- **Status:** implemented

**Params:** `{}`

**Result:** `{}`

---

### Server -> Client Notifications

These are sent by the WM Server to connected clients. Clients must implement
a background reader to receive them.

#### `actions/partialResult`

Sent during `actions/runWithPartialResults` execution as results stream in.

- **Type:** notification (server -> client)
- **Clients:** LSP
- **Status:** stub

**Params:**

```json
{
  "token": "diag_1",
  "value": {
    "result_by_format": {
      "json": {"messages": {"file.py": [...]}},
      "string": "3 issues found in file.py"
    }
  }
}
```

`token` matches the `partial_result_token` from the originating request.

`result_by_format` contains results in all formats requested in the originating
`actions/runWithPartialResults` params (same structure as `actions/run` response,
but without `return_code`).

> **Note:** Notifications are delivered only to the client connection that
> initiated the corresponding `actions/runWithPartialResults` request.  The
> WM Server does **not** broadcast these messages to every connected client.

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
