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

All field names in params and results use **camelCase**.

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
{"filePath": "/abs/path/to/some/file.py"}
```

**Result:**

```json
{"project": "/abs/path/to/project"}
```

Returns `{"project": null}` if the file does not belong to any known project.

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
{"dirPath": "/path/to/workspace", "startRunners": true, "projects": ["my_project"]}
```

`startRunners` is optional (default: `true`). When `false`, the server reads
configs and collects actions without starting any extension runners. Use this
when runner environments may not exist yet (e.g. before running `prepare-envs`).
Actions are still available in the result so clients can validate the workspace.

`projects` is optional. When provided, only the listed projects (by name) will
be config-initialized and have their runners started. All other projects in the
directory are still discovered (added to workspace state) but skipped for
initialization. This avoids the cost of reading configs and spawning runner
processes for projects that are not needed.

Calling `workspace/addDir` again for the same `dirPath` with a different
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
{"projects": ["my_project"], "resolvePresets": true}
```

`projects` is optional. If omitted, starts runners for all projects.

`resolvePresets` is optional (default: `true`). When `false`, the server starts
missing runners without resolving presets, so project action lists are not
refreshed by this call.

**Result:** `{}`

---

#### `workspace/setConfigOverrides`

Set persistent handler config overrides on the server. Overrides are stored for
the lifetime of the server and applied to all subsequent action runs — unlike the
`configOverrides` field that was previously accepted by `actions/runBatch`, which
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
{"dirPath": "/path/to/workspace"}
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
{"project": "/abs/path/to/project"}
```

**Result:**

```json
{
  "rawConfig": {
    "tool": { "finecode": { "..." : "..." } }
  }
}
```

**Errors:**

- `project` is required — returns a JSON-RPC error if omitted.
- Project not found — returns a JSON-RPC error if no project with the given path
  exists in the workspace context.

---

### `actions/` — Action Discovery & Execution

#### `actions/list`

List available actions, optionally filtered by project. Flat listing for
programmatic use by MCP agents and CLI.

- **Type:** request
- **Clients:** MCP, CLI
- **Status:** implemented

**Params:**

```json
{"project": "/abs/path/to/project"}
```

All fields optional. If `project` is omitted, returns actions from all projects.

**Result:**

```json
{
  "actions": [
    {
      "name": "lint",
      "source": "finecode_extension_api.actions.LintAction",
      "project": "/abs/path/to/project",
      "handlers": [
        {"name": "ruff", "source": "fine_python_ruff.RuffLintFilesHandler", "env": "runtime"}
      ]
    }
  ]
}
```

`source` is the import-path alias that uniquely identifies the action class (ADR-0019).
It is the value to pass as `actionSource` in `actions/run`, `actions/runBatch`, etc.

---

#### `actions/getPayloadSchemas`

Return payload schemas for the specified actions in a project. Used by the MCP
server to build accurate `inputSchema` entries for each tool.

- **Type:** request
- **Clients:** MCP
- **Status:** implemented

**Params:**

```json
{"project": "/abs/path/to/project", "actionSources": ["finecode_extension_api.actions.LintAction", "finecode_extension_api.actions.FormatAction"]}
```

**Result:**

```json
{
  "schemas": {
    "finecode_extension_api.actions.LintAction": {
      "properties": {
        "file_paths": {"type": "array", "items": {"type": "string"}},
        "target": {"type": "string", "enum": ["project", "files"]}
      },
      "required": []
    },
    "finecode_extension_api.actions.FormatAction": {
      "properties": {
        "save": {"type": "boolean"},
        "target": {"type": "string"},
        "file_paths": {"type": "array", "items": {"type": "string"}}
      },
      "required": []
    }
  }
}
```

Result is keyed by action source. Each value is `null` for actions whose class
cannot be imported in any Extension Runner. Schemas are cached per project in the
WM and invalidated whenever runner config is updated.

---

#### `actions/getTree`

Get the hierarchical action tree for IDE sidebar display.

- **Type:** request
- **Clients:** LSP
- **Status:** implemented

**Params:** `{}`

**Result:**

```json
{
  "nodes": [
    {
      "nodeId": "/path/to/workspace",
      "name": "workspace",
      "nodeType": 0,
      "status": "",
      "subnodes": [
        {
          "nodeId": "/path/to/workspace/my_project",
          "name": "my_project",
          "nodeType": 1,
          "status": "CONFIG_VALID",
          "subnodes": [
            {
              "nodeId": "/path/to/my_project::actions",
              "name": "Actions",
              "nodeType": 3,
              "status": "",
              "subnodes": [
                {
                  "nodeId": "/path/to/my_project::finecode_extension_api.actions.LintAction",
                  "name": "lint",
                  "source": "finecode_extension_api.actions.LintAction",
                  "nodeType": 2,
                  "status": "",
                  "subnodes": []
                }
              ]
            },
            {
              "nodeId": "/path/to/my_project::envs",
              "name": "Environments",
              "nodeType": 5,
              "status": "",
              "subnodes": [
                {
                  "nodeId": "/path/to/my_project::envs::runtime",
                  "name": "runtime",
                  "nodeType": 6,
                  "status": "",
                  "subnodes": []
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

`nodeType` values: `0`=DIRECTORY, `1`=PROJECT, `2`=ACTION, `3`=ACTION_GROUP, `4`=PRESET,
`5`=ENV_GROUP, `6`=ENV

Node ID format:

- Directory/project: absolute path string (e.g. `"/path/to/project"`)
- Action group: `"<project_path>::actions"`
- Action: `"<project_path>::<actionSource>"` (e.g. `"/path/to/project::finecode_extension_api.actions.LintAction"`)
- Handler: `"<project_path>::<actionSource>::<handlerName>"`
- Env group: `"<project_path>::envs"`
- Env: `"<project_path>::envs::<envName>"`

---

#### `actions/run`

Execute a single action on a project.

- **Type:** request
- **Clients:** LSP, MCP, CLI
- **Status:** implemented

**Params:**

```json
{
  "actionSource": "finecode_extension_api.actions.LintAction",
  "project": "/abs/path/to/project",
  "params": {"file_paths": ["/path/to/file.py"]},
  "options": {
    "resultFormats": ["json", "string"],
    "trigger": "user",
    "devEnv": "ide"
  }
}
```

Required: `actionSource`, `project`. All other fields optional.

`actionSource` is an import-path alias identifying the action class (ADR-0019).
Any valid import path resolving to the same registered action class is accepted
(e.g. the short alias `"finecode_extension_api.actions.LintAction"` or the canonical
`"finecode_extension_api.actions.code_quality.lint_action.LintAction"` both work).

`trigger` values: `"user"`, `"system"`, `"unknown"` (default: `"unknown"`)

`devEnv` values: `"ide"`, `"cli"`, `"ai"`, `"precommit"`, `"ci"` (default: `"cli"`)

**Streaming options (both optional):**

- `partialResultToken` — when present, all result data is delivered via
  `actions/partialResult` notifications during execution; the final JSON-RPC
  response contains only `returnCode` as a completion signal.  May be combined
  with `progressToken`.
- `progressToken` — when present (and `partialResultToken` is absent), the server
  sends `actions/progress` notifications during execution.

Pass `project=""` to run across all projects that expose the action (same
semantics as `actions/runBatch` with no `projects` filter).

**Result (without `partialResultToken`):**

```json
{
  "resultByFormat": {
    "json": {"messages": {"file.py": []}},
    "string": "All checks passed."
  },
  "returnCode": 0
}
```

**Result (with `partialResultToken`):**

```json
{"returnCode": 0}
```

All result data is carried by `actions/partialResult` notifications.

---

#### `actions/runBatch`

Execute multiple actions across multiple projects. Used for batch operations.

- **Type:** request
- **Clients:** CLI, MCP
- **Status:** implemented

**Params:**

```json
{
  "actionSources": [
    "finecode_extension_api.actions.LintAction",
    "finecode_extension_api.actions.FormatAction"
  ],
  "projects": ["/abs/path/to/project_a", "/abs/path/to/project_b"],
  "params": {},
  "options": {
    "concurrently": false,
    "resultFormats": ["json", "string"],
    "trigger": "user",
    "devEnv": "cli"
  }
}
```

Required: `actionSources`. If `projects` is omitted, runs on all projects that have the
requested actions.

**Streaming options (both optional):**

- `partialResultToken` — when present, the server emits one `actions/partialResult`
  notification per completed project in completion order.  Each notification carries
  the full result block for that project (see `actions/partialResult` below).  The
  final response contains only `returnCode` as a completion signal.
- `progressToken` — when present (and `partialResultToken` is absent), the server
  sends aggregated `actions/progress` notifications across all (project × action) slots.

**Result (without `partialResultToken`):**

```json
{
  "results": {
    "/abs/path/to/project_a": {
      "finecode_extension_api.actions.LintAction": {"resultByFormat": {"...": "..."}, "returnCode": 0},
      "finecode_extension_api.actions.FormatAction": {"resultByFormat": {"...": "..."}, "returnCode": 0}
    }
  },
  "returnCode": 0
}
```

Result is keyed by project path, then by action source. `returnCode` at the top level
is the bitwise OR of all individual return codes.

**Result (with `partialResultToken`):**

```json
{"returnCode": 0}
```

All per-project result data is carried by `actions/partialResult` notifications.

---

---

#### `actions/reload`

Hot-reload handler code for an action without restarting runners.

- **Type:** request
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"actionNodeId": "/abs/path/to/project::finecode_extension_api.actions.LintAction"}
```

`actionNodeId` uses the same `<project_path>::<actionSource>` format as the node IDs
in the `actions/getTree` response.

**Result:** `{}`

---

### `documents/` — Document Sync

Notifications from the LSP client to keep the WM Server (and extension runners)
informed about open documents. These are fire-and-forget (no response).

#### `documents/opened`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"uri": "file:///path/to/file.py", "version": 1}
```

---

#### `documents/closed`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{"uri": "file:///path/to/file.py"}
```

---

#### `documents/changed`

- **Type:** notification (client -> server)
- **Clients:** LSP
- **Status:** implemented

**Params:**

```json
{
  "uri": "file:///path/to/file.py",
  "version": 2,
  "contentChanges": [
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
- **Status:** implemented

**Params:**

```json
{"project": "/abs/path/to/project"}
```

`project` is optional. If omitted, returns runners for all projects.

**Result:**

```json
{
  "runners": [
    {
      "project": "/abs/path/to/project",
      "env": "runtime",
      "status": "RUNNING",
      "readable_id": "my_project::runtime"
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
- **Status:** implemented

**Params:**

```json
{"project": "/abs/path/to/project", "env": "runtime", "debug": false}
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
{"project": "/abs/path/to/project", "envName": "dev_workspace"}
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
{"project": "/abs/path/to/project", "envName": "dev_workspace"}
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
  "logFilePath": "/abs/path/to/.venvs/dev_workspace/logs/wm_server/wm_server.log"
}
```

`logFilePath` is the absolute path to the WM Server's log file for the current process.
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

Sent when an `actions/run` or `actions/runBatch` request includes a
`partialResultToken`.

- **Type:** notification (server -> client)
- **Clients:** LSP, MCP, CLI
- **Status:** implemented

`token` matches the `partialResultToken` from the originating request.

> **Note:** Notifications are delivered only to the client connection that
> initiated the request.  The WM Server does **not** broadcast these messages to
> every connected client.

**Params for `actions/run` + `partialResultToken`:**

```json
{
  "token": "diag_1",
  "value": {
    "project": "/abs/path/to/project",
    "resultByFormat": {
      "json": {"messages": {"file.py": [...]}},
      "string": "3 issues found in file.py"
    }
  }
}
```

`value.project` is the absolute path of the project that produced this partial result.
When `project=""` is passed to `actions/run` (run across all projects), multiple
notifications are emitted — one per project — and `value.project` identifies which
project each belongs to, allowing clients to group results by project.

`value.resultByFormat` mirrors the `actions/run` result shape (without `returnCode`).

> **Guarantee:** The WM Server always delivers results via `actions/partialResult`
> notifications, even when an extension runner does not stream incrementally (i.e.
> it collects all results internally and returns them as a single final response).
> In that case the server emits the final result as a partial result notification
> before sending the final response.  Clients must not rely on the final response
> body for result data — it contains only `returnCode`.

**Params for `actions/runBatch` + `partialResultToken`:**

```json
{
  "token": "batch_1",
  "value": {
    "project": "/abs/path/to/project_a",
    "results": {
      "finecode_extension_api.actions.LintAction": {
        "resultByFormat": {"json": {}, "string": "..."},
        "returnCode": 0
      }
    },
    "returnCode": 0
  }
}
```

One notification is emitted per project, in completion order (the fastest project
finishes first).  `value.returnCode` is the bitwise OR of all action return codes
for that project.  `value.results` is keyed by action source, matching the shape of
a single entry in the final `actions/runBatch` response.

---

#### `actions/progress`

Sent during `actions/run` or `actions/runBatch` when the request includes a
`progressToken` (and no `partialResultToken`).

- **Type:** notification (server -> client)
- **Clients:** LSP, MCP, CLI
- **Status:** implemented

**Params:**

```json
{
  "token": "progress_1",
  "value": {
    "type": "report",
    "message": "Checked 12/42 files",
    "percentage": 28
  }
}
```

`token` matches the `progressToken` from the originating request.

`value.type` values: `"begin"`, `"report"`, `"end"`

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
    "nodeId": "/path/to/project",
    "name": "my_project",
    "nodeType": 1,
    "status": "CONFIG_VALID",
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
