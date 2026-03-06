# Concepts

Understanding a few core concepts makes the rest of FineCode straightforward.

## Action

An **Action** is a named operation — `lint`, `format`, `build_artifact`, etc. It is defined as a Python class that declares the types of its payload, execution context, and result:

```python
class LintAction(code_action.Action[LintRunPayload, LintRunContext, LintRunResult]):
    PAYLOAD_TYPE = LintRunPayload
    RUN_CONTEXT_TYPE = LintRunContext
    RESULT_TYPE = LintRunResult
```

Actions are identified by their **import path** (e.g. `finecode_extension_api.actions.lint.LintAction`), not by the name used in config. The config name is just a human-readable alias.

## ActionHandler

An **ActionHandler** is a concrete implementation of an action. Multiple handlers can be registered for a single action. For example, the `lint` action might have handlers for ruff, flake8, and mypy — each independently checking the code.

Each handler:

- specifies which **virtual environment** (`env`) it runs in
- declares its own **dependencies** (installed automatically by `prepare-envs`)
- receives the action payload and returns a result

### Sequential vs. concurrent execution

Handlers for an action run either **sequentially** (default) or **concurrently**.

```mermaid
flowchart LR
    subgraph Sequential
        direction TB
        P1[payload] --> H1[handler 1]
        H1 -->|current_result| H2[handler 2]
        H2 -->|current_result| H3[handler 3]
        H3 --> R1[result]
    end

    subgraph Concurrent
        direction TB
        P2[payload] --> H4[handler 1]
        P2 --> H5[handler 2]
        P2 --> H6[handler 3]
        H4 --> M[merge]
        H5 --> M
        H6 --> M
        M --> R2[result]
    end
```

**Sequential mode** (default): handlers run one after another. Each handler can read the accumulated result so far via `context.current_result`. Useful when handlers depend on each other's output (e.g. formatter → save-to-disk).

**Concurrent mode** (`run_handlers_concurrently: true`): all handlers run in parallel and results are merged afterward. Accessing `context.current_result` in concurrent mode raises `RuntimeError`. Useful for independent linters.

## Preset

A **Preset** is a Python package that bundles action and handler declarations into a reusable, distributable configuration. Users install a preset as a `dev_workspace` dependency and reference it in `pyproject.toml`:

```toml
[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

A preset contains a `preset.toml` file that declares which handlers to activate for which actions. The user's `pyproject.toml` configuration is merged on top of the preset, giving the user full control to override, extend, or disable individual handlers.

### Handler modes

When configuring an action in `pyproject.toml`, you can control how your configuration relates to preset handlers:

- **`handlers_mode = "merge"`:** (default) your handlers are added to the preset's handlers.
- **`handlers_mode = "replace"`:** your handler list completely replaces the preset's handlers for that action.
- **`enabled = false` on a handler entry:** disables that specific inherited handler.

## Project

A **Project** is any directory containing a `pyproject.toml` with a `[tool.finecode]` section. FineCode discovers all projects under the workspace root automatically.

A project may belong to a **workspace** — a directory containing multiple projects. FineCode handles multi-project workspaces transparently: running `python -m finecode run lint` from the workspace root runs lint in all projects that define it.

## Workspace Manager and Extension Runner

FineCode has two runtime components:

### Workspace Manager (WM)

The `finecode` package. It:

- discovers projects and resolves merged configuration
- manages virtual environments (`prepare-envs`)
- exposes an **LSP server** to the IDE
- delegates action execution to Extension Runners

### Extension Runner (ER)

The `finecode_extension_runner` package. It:

- runs inside an isolated virtual environment (e.g. `.venvs/dev_no_runtime`)
- imports and executes handler code
- communicates results back to the WM via LSP/JSON-RPC

The WM/ER split means handler dependencies never pollute the workspace Python environment.

## Environments

Each handler declares an `env` (e.g. `dev_no_runtime`, `runtime`). FineCode creates a separate virtualenv for each env name it encounters, and installs the handler's declared `dependencies` into it. `prepare-envs` automates this.

```toml
# Example: handler in pyproject.toml
[[tool.finecode.action.lint.handlers]]
name = "ruff"
source = "fine_python_ruff.RuffLintFilesHandler"
env = "dev_no_runtime"
dependencies = ["fine_python_ruff~=0.2.0"]
```

The env name is arbitrary — it's just a label FineCode uses to group handlers that share a virtualenv.
