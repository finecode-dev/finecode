# Configuration

FineCode merges configuration from multiple sources in order of increasing priority:

```
preset.toml  →  pyproject.toml / finecode.toml  →  finecode-user.toml  →  environment variables  →  CLI flags
```

Higher-priority sources override lower-priority ones.

## Where configuration lives

| Scope | File | Notes |
| --- | --- | --- |
| Workspace | `finecode-workspace.toml` at the workspace root | Only valid location for workspace-scoped settings |
| Project | `[tool.finecode.*]` in `pyproject.toml` | Default; recommended |
| Project | `finecode.toml` at the project root | Alternative to `pyproject.toml`; cannot be combined with it |
| User (project) | `finecode-user.toml` at the project root | Personal preferences; gitignored; optional |
| User (preset) | `finecode-user.toml` next to `preset.toml` | Editable presets only; gitignored; optional |

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

**By source path** — use `[[tool.finecode.action_handler]]`:

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

**By action and handler name** — use a table keyed by handler name under the action:

```toml
[tool.finecode.action.lint.handlers.ruff]
config.line_length = 100

[tool.finecode.action.lint.handlers.flake8]
config.max_line_length = 88
enabled = false
```

Any handler field can be overridden this way: `config`, `enabled`, `env`, `dependencies`, etc. The entry is merged into the handler already declared by the action (typically from a preset) — it does not replace it. This syntax is useful when you want to reference a handler by its logical name rather than its source class, for example to supply deployment-specific config for a handler that a preset declares.

### Pinning extension tool versions

Each extension declares a compatibility range for the tool it wraps (see [Creating an Extension — Tool versioning](guides/creating-extension.md#tool-versioning)). To pin a specific version across every handler the extension contributes, configure the extension once by its package name:

```toml
[tool.finecode.extension.fine_python_ruff]
dependencies_override = ["ruff==0.9.0", "ruff-plugin-foo==1.2.3"]
```

For the rare case where you need different tool versions for different handlers from the same extension, fall back to a handler-level `dependencies_override` — it wins over the extension-level value for that handler.

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

Services can also declare `config`:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.icommandrunner.ICommandRunner"
source = "finecode_extension_runner.impls.command_runner.CommandRunner"
env = "dev_no_runtime"
config.max_concurrent_processes = 4
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

## finecode-user.toml

`finecode-user.toml` is a personal, gitignored configuration layer for developer-specific preferences that must not affect the shared workspace configuration. A motivating example is installing a personal AI assistant inside a devcontainer: different developers may want Copilot, Codeium, or nothing at all, and none of these choices belong in a committed config file.

### File locations

There are two optional locations, following a **uniform sibling rule**: every project-level config file and every `preset.toml` gets an optional sibling `finecode-user.toml` in the same directory.

| Location | Scope |
| --- | --- |
| `{project-root}/finecode-user.toml` | Personal project-level preferences; merged into that project's resolved config above project config |
| `{preset-dir}/finecode-user.toml` | Merged into that preset's config at read time; sits at preset priority |

`finecode-workspace.toml` does not get a sibling — workspace-scoped settings are shared by definition.

### Schema

`finecode-user.toml` uses the same fields as `finecode.toml` but with **no `[finecode]` wrapper** — tables are written at the top level:

```toml
presets = [{ source = "my_personal_preset" }]

[action.setup_dev_tools]
handlers = [
    { name = "copilot", source = "fine_vscode_ext.InstallExtHandler", config.ext_id = "GitHub.copilot" },
]

[action.lint.handlers.ruff]
config.line_length = 90

[[service]]
interface = "myext.IMyService"
source = "myext.MyServiceImpl"
env = "dev_no_runtime"
dependencies = []

[dependency-groups]
dev_workspace = ["my_personal_preset>=1.0"]
```

`[workspace]` is not allowed in any user file — workspace settings always go in `finecode-workspace.toml`.

### What it can do

Everything `finecode.toml` can do: declare presets, add handlers to actions, configure existing handlers (`config`, `enabled`, `env`, `dependencies`), replace handler lists (`handlers_mode = "replace"`), declare services, configure the Extension Runner (`[er]`).

One capability beyond `finecode.toml`: user files may declare a `[dependency-groups]` section to add packages to any dependency group (see below).

### Adding dependency groups

```toml
[dependency-groups]
dev_workspace = ["my_personal_preset>=1.0"]
```

User-declared groups are merged **additively** into the project's groups — existing packages are not removed. This is the primary mechanism for installing a personal preset package into `dev_workspace` so `prepare-envs` picks it up automatically.

Handler `dependencies` fields still work as normal; `[dependency-groups]` is only needed when installing an entire personal preset package that is not part of the shared config.

### Declaring personal presets

```toml
presets = [{ source = "my_personal_preset" }]

[dependency-groups]
dev_workspace = ["my_personal_preset>=1.0"]
```

User-declared presets are resolved in the same pass as shared presets and sit at preset priority — project config, workspace config, and the project-level user file can all override them.

The preset package must be importable from `dev_workspace`. If it is not part of the shared config, declare it under `[dependency-groups]` in the same user file and `prepare-envs` will install it.

**First-run two-step.** On the first `prepare-envs` after adding a personal preset this way, the package is installed in `dev_workspace` but the preset's own handler dependencies cannot yet be resolved (the package was unavailable during that run). Run `prepare-envs` a second time to install those. This is the same behavior as adding any new preset to shared config.

### Merge priority

```
(preset + preset-user, merged at read time)
  → project (pyproject.toml / finecode.toml)
  → workspace (finecode-workspace.toml)
  → project-user  ({project-root}/finecode-user.toml)
  → env vars
  → CLI flags
```

Project-level user config wins over all file-based shared config. Preset-level user config sits at preset priority and can be overridden by project config, workspace config, and the project-level user file.

### Preset-level user config — limitations

**Editable presets only.** The preset-level user file is only practical for presets that are editable installs with a stable on-disk directory (e.g. local monorepo presets). For presets installed as non-editable packages, the preset directory lives inside the virtualenv and is overwritten by `prepare-envs`. Use the project-level user file for non-editable presets.

**No `[dependency-groups]` at preset level.** Dependency group declarations in a preset-level user file are not supported and generate a warning. Declare personal packages in the project-root `finecode-user.toml` instead.

### gitignore convention

Add `finecode-user.toml` to your `.gitignore`:

```
finecode-user.toml
```

The file is gitignored by convention; FineCode does not enforce this. If the file is absent, all behavior is a safe no-op.

### Example

**Shared project config (`pyproject.toml`) — committed to git:**

```toml
[tool.finecode.action.setup_dev_tools]
source = "finecode_extension_api.actions.setup_dev_tools.SetupDevToolsAction"
handlers = []
```

**Developer A's project-root `finecode-user.toml` — gitignored:**

```toml
presets = [{ source = "my_personal_preset" }]

[action.setup_dev_tools]
handlers = [
    { name = "copilot", source = "fine_vscode_ext.InstallExtHandler", config.ext_id = "GitHub.copilot" },
]

[dependency-groups]
dev_workspace = ["my_personal_preset>=1.0"]
```

**Developer B's project-root `finecode-user.toml`:**

```toml
[action.setup_dev_tools]
handlers = [
    { name = "codeium", source = "fine_vscode_ext.InstallExtHandler", config.ext_id = "Codeium.codeium" },
]
```

**`.devcontainer/devcontainer.json` — shared, identical for everyone:**

```json
{
  "postCreateCommand": "finecode run setup_dev_tools"
}
```

For developers without a `finecode-user.toml`, the action runs with no handlers and is a no-op. For developers who have added handlers, their tools are installed automatically on devcontainer rebuild.

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

To surface WM and ER logs in a CI job log (rather than only in files), pair `--log-level` with `--verbose` — see [Diagnostic logs in CI](cli.md#diagnostic-logs-in-ci) for the recommended INFO-by-default, DEBUG-on-demand recipe.

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
