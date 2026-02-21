# CLI Reference

All commands are run from the workspace or project root directory, inside the `dev_workspace` virtual environment.

```bash
source .venvs/dev_workspace/bin/activate
python -m finecode <command> [options]
```

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
| `--project=<name>` | Run only in this project. Repeatable for multiple projects. |
| `--concurrently` | Run actions concurrently within each project |
| `--trace` | Enable verbose (trace-level) logging |
| `--no-env-config` | Ignore `FINECODE_CONFIG_*` environment variables |
| `--no-save-results` | Do not write action results to the cache directory |

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
python -m finecode prepare-envs [--recreate] [--trace] [--debug]
```

Must be run from the workspace or project root. Creates venvs under `.venvs/<env_name>/` and installs each handler's declared dependencies.

| Option | Description |
|---|---|
| `--recreate` | Delete and recreate all venvs from scratch |
| `--trace` | Enable verbose logging |
| `--debug` | Wait for a debugpy client on port 5680 before starting |

---

## `dump-config`

Dump the fully resolved configuration for a project to disk, useful for debugging preset and config merging.

```
python -m finecode dump-config --project=<name> [--trace] [--debug]
```

Output is written to `<cwd>/finecode_config_dump/`.

| Option | Description |
|---|---|
| `--project=<name>` | **(Required)** Project to dump config for |
| `--trace` | Enable verbose logging |
| `--debug` | Wait for a debugpy client on port 5680 |

---

## `start-api`

Start the FineCode LSP server. Used by the IDE extension — you typically don't call this directly.

```
python -m finecode start-api --stdio | --socket <port> | --ws [--host <host>] [--port <port>]
```

| Option | Description |
|---|---|
| `--stdio` | Communicate over stdin/stdout |
| `--socket <port>` | Start a TCP server on the given port |
| `--ws` | Start a WebSocket server |
| `--host <host>` | Host for TCP/WS server (default: 127.0.0.1 for TCP) |
| `--port <port>` | Port for TCP/WS server |
| `--mcp` | Also start an MCP server |
| `--mcp-port <port>` | Port for the MCP server |
| `--trace` | Enable verbose logging |
| `--debug` | Wait for a debugpy client on port 5680 |
