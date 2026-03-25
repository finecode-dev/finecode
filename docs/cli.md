# CLI Reference

All commands are run from the workspace or project root directory, inside the `dev_workspace` virtual environment.

```bash
source .venvs/dev_workspace/bin/activate
python -m finecode <command> [options]
```

---

## Usage modes

The `run` command supports two usage modes.

### Standalone (one-shot) — default

Each `run` invocation is fully independent. FineCode starts a dedicated WM Server subprocess for the duration of the command, then shuts it down on exit. This is the default behavior.

```bash
python -m finecode run lint
```

Use this in CI/CD pipelines or any context where you don't want persistent background processes. Results from one action can be saved to the file cache and referenced by a later action via `--map-payload-fields` (see the `run` reference below).

### Persistent server

A long-lived WM Server holds warm state — loaded configuration, started runners — across multiple `run` calls. Use `--shared-server` to connect to a running shared instance instead of starting a dedicated one.

```bash
# Connect to the shared server (start it first if needed):
python -m finecode run --shared-server lint
python -m finecode run --shared-server format
```

This mode is used automatically by the LSP and MCP integrations. It gives faster repeated runs because configuration loading and runner startup are amortized across calls.

The server waits 30 seconds after the last client disconnects before shutting down (configurable via `--disconnect-timeout` on `start-wm-server`).

---

## `run`

Run one or more actions across projects.

```
python -m finecode run [options] <action> [<action> ...] [payload] [--config.<key>=<value> ...]
```

### Options

| Option | Description |
|---|---|
| `--workdir=<path>` | Use `<path>` as the workspace root instead of `cwd` |
| `--project=<name>` | Run only in this project (matched by `[project].name` from `pyproject.toml`). Repeatable for multiple projects. |
| `--concurrently` | Run actions concurrently within each project |
| `--shared-server` | Connect to the shared persistent WM Server instead of starting a dedicated one |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--no-env-config` | Ignore `FINECODE_CONFIG_*` environment variables |
| `--no-save-results` | Do not write action results to the cache directory |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected — see [Dev environment detection](#dev-environment-detection)) |

### Payload

Named parameters passed to the action payload. All must use `--<name>=<value>` form:

```bash
python -m finecode run format --save=true
python -m finecode run lint --target=files --file-paths='["src/main.py"]'
```

### Config overrides

Override handler configuration inline:

```bash
# Action-level (applies to all handlers)
python -m finecode run lint --config.line_length=120

# Handler-specific
python -m finecode run lint --config.ruff.line_length=120 --config.mypy.strict=true
```

See [Configuration](configuration.md) for full details on config precedence.

### Behavior

- With no `--project`: FineCode treats `cwd` (or `--workdir`) as the workspace root, discovers all projects, and runs the action in each project that defines it.
- With `--project`: the action must exist in every specified project.
- Action results are saved to `<venv>/cache/finecode/results/<action>.json` (one entry per project path).

### Examples

```bash
# Lint all projects
python -m finecode run lint

# Lint and check_formatting concurrently
python -m finecode run --concurrently lint check_formatting

# Run only in two specific projects
python -m finecode run --project=fine_python_mypy --project=fine_python_ruff run lint

# Run from a different directory
python -m finecode --workdir=./finecode_extension_api run lint

# Override ruff line length
python -m finecode run lint --config.ruff.line_length=120
```

---

## `prepare-envs`

Create and populate virtual environments for all handler dependencies.

```
python -m finecode prepare-envs [--recreate] [--env-names=<name>]...
                                 [--project=<name>]... [--log-level=<level>] [--debug]
```

Must be run from the workspace or project root. Creates venvs under `.venvs/<env_name>/` and installs each handler's declared dependencies.

See [Preparing Environments](guides/preparing-environments.md) for a full explanation of the three-step sequence and filtering options.

| Option | Description |
|---|---|
| `--recreate` | Delete and recreate all venvs from scratch |
| `--env-names=<name>` | Restrict handler dependency installation to the named env(s). Repeatable. See note below. |
| `--project=<name>` | Restrict preparation to the named project(s) (matched by `[project].name` from `pyproject.toml`). Repeatable. |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--debug` | Wait for a debugpy client on port 5680 before starting |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected) |


!!! note `--env-names` restricts only the `install_envs` step. The `create_envs` step still runs for **all** envs regardless of this flag — virtualenvs must exist for every env even when you only need to update dependencies in one of them.

---

## `dump-config`

Dump the fully resolved configuration for a project to disk, useful for debugging preset and config merging.

```
python -m finecode dump-config --project=<name> [--log-level=<level>] [--debug]
```

Output is written to `<cwd>/finecode_config_dump/`.

| Option | Description |
|---|---|
| `--project=<name>` | **(Required)** Project to dump config for (matched by `[project].name` from `pyproject.toml`) |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--debug` | Wait for a debugpy client on port 5680 |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected) |

---

## Dev environment detection

FineCode tracks which environment triggered an action run (e.g. IDE, CLI, CI/CD). This value is passed to handlers via `RunActionMeta.dev_env` and can be used to adjust behavior — for example, to emit machine-readable output in CI.

The `run`, `prepare-envs`, and `dump-config` commands detect the environment automatically:

| Condition | Detected value |
|---|---|
| `CI` environment variable is set (any non-empty value) | `ci` |
| Default | `cli` |

The `CI` variable is set automatically by GitHub Actions, GitLab CI, CircleCI, Travis CI, Bitbucket Pipelines, and most other CI systems.

Use `--dev-env=<value>` on any command to override the detected value explicitly:

```bash
# Force CI/CD mode locally
python -m finecode run --dev-env=ci lint

# Mark as a pre-commit run
python -m finecode run --dev-env=precommit lint
```

Valid values: `ai`, `ci`, `cli`, `ide`, `precommit`.

---

## `start-lsp`

Start the FineCode LSP server. Used by the IDE extension — you typically don't call this directly.

```
python -m finecode start-lsp --stdio | --socket <port> | --ws [--host <host>] [--port <port>]
```

| Option | Description |
|---|---|
| `--stdio` | Communicate over stdin/stdout |
| `--socket <port>` | Start a TCP server on the given port |
| `--ws` | Start a WebSocket server |
| `--host <host>` | Host for TCP/WS server (default: 127.0.0.1 for TCP) |
| `--port <port>` | Port for TCP/WS server |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--debug` | Wait for a debugpy client on port 5680 |

The LSP server connects to the **FineCode WM Server** on startup (starting one if needed). See [LSP and MCP Architecture](reference/lsp-mcp-architecture.md) for details.

---

## `start-mcp`

Start the FineCode MCP server on stdio. Connects to a running FineCode WM Server (or starts one) and exposes FineCode tools via the Model Context Protocol.

```text
.venvs/dev_workspace/bin/python -m finecode start-mcp [--workdir=<path>] [--log-level=<level>]
```

| Option | Description |
| --- | --- |
| `--workdir=<path>` | Workspace root directory (default: current directory). |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

Typically started automatically by MCP-compatible clients (for example, Claude Code) — see [IDE and MCP Setup](getting-started-ide-mcp.md#mcp-setup-for-ai-clients).

---

## `start-wm-server`

Start the FineCode Workspace Manager Server standalone (TCP JSON-RPC), listen for client connections. Shuts down after the last client disconnects and the disconnect timeout expires.

```text
python -m finecode start-wm-server [--log-level=<level>] [--disconnect-timeout=<seconds>]
```

| Option | Description |
| --- | --- |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--disconnect-timeout=<seconds>` | Seconds to wait after the last client disconnects before shutting down (default: 30) |

Usually started automatically by `start-lsp` or `start-mcp`. Can also be started manually for debugging.
