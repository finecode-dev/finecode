# Configuration

FineCode merges configuration from multiple sources in order of increasing priority:

```
preset.toml  →  pyproject.toml / finecode.toml  →  environment variables  →  CLI flags
```

Higher-priority sources override lower-priority ones.

## Where configuration lives

| Scope | File | Notes |
| --- | --- | --- |
| Workspace | `finecode-workspace.toml` at the workspace root | Only valid location for workspace-scoped settings |
| Project | `[tool.finecode.*]` in `pyproject.toml` | Default; recommended |
| Project | `finecode.toml` at the project root | Alternative to `pyproject.toml`; cannot be combined with it |

`finecode.toml` and `[tool.finecode.*]` in `pyproject.toml` are **mutually exclusive** within a project — pick one. `finecode.toml` uses a `[finecode]` top-level table; the structure is otherwise identical. A lone `finecode.toml` without an adjacent `pyproject.toml` is ignored.

## pyproject.toml

All FineCode project configuration lives under `[tool.finecode]`.

### Enabling presets

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" },
    { source = "my_custom_preset" },
]
```

Presets are applied in order. Later presets' handlers are added after earlier ones.

### Declaring actions and handlers

You can declare or extend actions directly in your project:

```toml
[tool.finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffLintFilesHandler", env = "dev_no_runtime", dependencies = ["fine_python_ruff~=0.2.0"] },
    { name = "mypy", source = "fine_python_mypy.MypyLintFilesHandler", env = "dev_no_runtime", dependencies = ["fine_python_mypy~=0.3.0"] },
]
```

### Replacing preset handlers

To completely replace the handlers from presets for an action:

```toml
[tool.finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers_mode = "replace"
handlers = [
    { name = "mypy", source = "fine_python_mypy.MypyLintFilesHandler", env = "dev_no_runtime", dependencies = ["fine_python_mypy~=0.3.0"] },
]
```

### Disabling a specific handler

```toml
[tool.finecode.action.lint]
handlers = [
    { name = "flake8", enabled = false },
]
```

### Configuring a handler

Use `[[tool.finecode.action_handler]]` to configure a handler by its source path:

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["B", "I"]
config.line_length = 100

[[tool.finecode.action_handler]]
source = "fine_python_flake8.Flake8LintFilesHandler"
config.max_line_length = 88
config.extend_ignore = ["E203", "E501"]
```

### Declaring services

Services are shared, long-lived dependencies used by handlers. Declare service bindings with `[[tool.finecode.service]]` entries:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.ilspclient.ILspClient"
source = "finecode_extension_runner.impls.lsp_client.LspClientImpl"
env = "dev_no_runtime"
dependencies = []
```

Service declarations are merged by `interface`. If a preset declares a service, you can rebind it in your project by declaring the same `interface` with a different `source`:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.ihttpclient.IHttpClient"
source = "my_company_http.MyHttpClient"
env = "dev_no_runtime"
dependencies = ["my_company_http~=1.2.0"]
```

### Configuring Extension Runner logging

Each Extension Runner is a separate subprocess. Its log level and per-group overrides are configured under `[tool.finecode.er]`. The WM reads this at startup and delivers the resolved config to the ER — the ER never reads config files directly.

`[tool.finecode.er.logging]` is the **project-level fallback** that applies to all ERs in the project. Per-env overrides live under `[tool.finecode.er.envs.<env_name>.logging]` and merge additively with the fallback.

```toml
# project-level fallback — applies to all ERs
[tool.finecode.er.logging]
default_level = "INFO"

[tool.finecode.er.logging.log_groups]
"finecode_extension_runner" = "WARNING"

# per-env override — applies only to the dev_no_runtime ER
[tool.finecode.er.envs.dev_no_runtime.logging]
default_level = "DEBUG"

[tool.finecode.er.envs.dev_no_runtime.logging.log_groups]
"fine_python_ruff" = "TRACE"
```

Env var overrides (highest priority):

| Variable | Effect |
| --- | --- |
| `FINECODE_ER_LOG_LEVEL` | project-level `default_level` |
| `FINECODE_ER_ENV_<ENV>_LOG_LEVEL` | per-env `default_level` (`<ENV>` uppercased, `-`→`_`) |
| `FINECODE_ER_LOG_GROUP_<GROUP>` | project-level `log_groups` entry (`<GROUP>` uppercased, `.`→`_`) |
| `FINECODE_ER_ENV_<ENV>_LOG_GROUP_<GROUP>` | per-env `log_groups` entry |

## finecode.toml

`finecode.toml` is an alternative location for project-scoped configuration. It uses a `[finecode]` top-level table instead of `[tool.finecode]`; every sub-table and field name is identical otherwise. The two files are mutually exclusive — if both exist for the same project, FineCode raises an error at startup.

```toml
[finecode]
presets = [{ source = "fine_python_recommended" }]

[finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffLintFilesHandler", env = "dev_no_runtime", dependencies = ["fine_python_ruff~=0.2.0"] },
]

[[finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.line_length = 100

[[finecode.service]]
interface = "finecode_extension_api.interfaces.ihttpclient.IHttpClient"
source = "finecode_httpclient.HttpClient"
env = "dev_no_runtime"
dependencies = ["finecode_httpclient~=0.1.0a1"]
```

The `[workspace]` table is not allowed in `finecode.toml`; workspace-scoped settings always go in `finecode-workspace.toml`.

## finecode-workspace.toml

Workspace-level configuration lives in `finecode-workspace.toml` at the workspace root, under the `[workspace]` table.

### Workspace editable packages

In a monorepo, local packages should be installed as editable installs. Declare them once in `finecode-workspace.toml`:

```toml
[workspace]
# When true, every project discovered in this workspace is automatically
# installed as an editable install when it appears as a dependency.
all_workspace_packages_editable = true

# Optional: explicit paths to treat as editable installs — useful for
# vendored forks outside normal project discovery. Paths are relative to
# the workspace root.
editable_packages = [
    "./vendored_forks/some_lib",
]
```

Any dependency whose package name matches a workspace editable package is automatically rewritten to an editable install from its declared path, across every env in every project. The resolved set is the union of every discovered project (when `all_workspace_packages_editable` is `true`) and every explicit `editable_packages` entry.

### WM telemetry

Configure the OTLP endpoint for the Workspace Manager and all Extension Runners under `[workspace.wm.telemetry]`:

```toml
[workspace.wm.telemetry]
otlp_endpoint = "http://localhost:4317"
```

The `FINECODE_OTLP_ENDPOINT` environment variable overrides this value (higher priority).

### WM logging

The Workspace Manager process reads its per-group log level overrides from `[workspace.wm.logging]`. This section controls only the WM process — it has no effect on ERs.

```toml
[workspace.wm.logging.log_groups]
"finecode.wm_server.runner.runner_manager" = "DEBUG"
"finecode_jsonrpc.client" = "TRACE"
```

The overall default log level is set via the `--log-level` CLI flag (default `INFO`). Env vars override the file config:

```bash
FINECODE_WM_LOG_GROUP_FINECODE_WM_SERVER_RUNNER_RUNNER_MANAGER=DEBUG
FINECODE_WM_LOG_GROUP_FINECODE_JSONRPC_CLIENT=TRACE
```

(`<GROUP>` is uppercased with `.` → `_`.)

## Environment variables

Override handler config at runtime without modifying files.

**Format:**

```
FINECODE_CONFIG_<ACTION>__<PARAM>=<json_value>
FINECODE_CONFIG_<ACTION>__<HANDLER>__<PARAM>=<json_value>
```

- `<ACTION>`, `<HANDLER>`, `<PARAM>` are **uppercase**, separated by double underscores (`__`)
- Values are parsed as **JSON** (use `"true"`, `123`, `"string"`, `["a","b"]`, etc.)

**Examples:**

```bash
# Set line_length for all handlers of the lint action
FINECODE_CONFIG_LINT__LINE_LENGTH=100 python -m finecode run lint

# Set line_length only for the ruff handler
FINECODE_CONFIG_LINT__RUFF__LINE_LENGTH=120 python -m finecode run lint

# Pass a JSON array
FINECODE_CONFIG_LINT__RUFF__EXTEND_SELECT='["B","I"]' python -m finecode run lint
```

To disable env var config entirely:

```bash
python -m finecode run --no-env-config lint
```

## CLI config flags

Override config inline on the command line. CLI flags take precedence over env vars.

**Format:**

```
--config.<param>=<value>
--config.<handler>.<param>=<value>
```

**Examples:**

```bash
# Action-level: applies to all handlers
python -m finecode run lint --config.line_length=120

# Handler-specific
python -m finecode run lint --config.ruff.line_length=120

# Combined
python -m finecode run lint --config.ruff.line_length=120 --config.mypy.strict=true

# CLI overrides env vars (line_length will be 120)
FINECODE_CONFIG_LINT__RUFF__LINE_LENGTH=100 python -m finecode run lint --config.ruff.line_length=120
```

## Inspecting resolved configuration

Dump the fully merged configuration for a project to a file:

```bash
python -m finecode dump-config --project=my_project
# Output written to finecode_config_dump/
```

This is useful for debugging config merging from multiple presets.
