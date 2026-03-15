# Preparing Environments

FineCode runs handlers in purpose-specific virtual environments. Handlers that share the same `env` name (e.g. `dev_no_runtime`) run in the same virtualenv. Before handlers can execute, their environments must exist and contain the right dependencies. This guide explains how that process works and how to control it.

## The three-step sequence

Environment preparation is split into three distinct actions that must run in order:

```
create_envs  →  prepare_runner_envs  →  prepare_handler_envs
```

### Step 1 — `create_envs`

Creates the virtual environments (`.venvs/<env_name>/`) discovered from the project's `dependency-groups`. No packages are installed yet.

Each env name found in `[dependency-groups]` becomes a virtualenv:

```toml
[dependency-groups]
dev_workspace    = ["finecode==0.3.*", ...]
dev_no_runtime   = ["fine_python_ruff~=0.2.0", ...]
runtime          = ["fastapi>=0.100", ...]
```

→ Creates `.venvs/dev_workspace/`, `.venvs/dev_no_runtime/`, `.venvs/runtime/`.

### Step 2 — `prepare_runner_envs`

Installs the **Extension Runner** (`finecode_extension_runner`) into each virtualenv. This is what lets FineCode start runners that can actually load handler code.

Preset packages are only installed in `dev_workspace` (handled during the bootstrap phase — see below). Only the runner is installed into other envs here — not the full handler dependency trees.

!!! note
    `prepare_runner_envs` must run **after** `create_envs` and **before** runners are started. Runners are started automatically between steps 2 and 3 by the WM during `prepare-envs`.

### Step 3 — `prepare_handler_envs`

Installs the full dependency set for each handler into its declared `env` virtualenv. This reads the `dependency-groups` entries and calls `install_deps_in_env` for each env.

After this step every handler has all its dependencies available and can execute.

---

## The `dev_workspace` bootstrap env

The `dev_workspace` env is special: it contains FineCode itself and the preset packages. This means the handlers that implement `prepare_runner_envs` and `prepare_handler_envs` *live inside* `dev_workspace`.

Because of this, `prepare-envs` handles `dev_workspace` separately, **before** starting runners:

1. `create_envs` (dev_workspace only) — create the venv if it doesn't exist
2. `prepare_handler_envs` (dev_workspace only) — install FineCode + presets

Only after this bootstrap are runners started, and only then can the remaining steps run across all envs.

---

## CLI command

The `prepare-envs` command runs the full sequence automatically:

```bash
python -m finecode prepare-envs
```

This is the only command most users need. It:

1. Discovers all projects in the workspace
2. Bootstraps `dev_workspace` (steps 1–2 above) for each project
3. Starts Extension Runners
4. Runs `create_envs` across all projects
5. Runs `prepare_runner_envs` across all projects
6. Runs `prepare_handler_envs` across all projects

See [CLI reference — prepare-envs](../cli.md#prepare-envs) for available options.

### Re-creating environments

```bash
python -m finecode prepare-envs --recreate
```

Deletes all existing virtualenvs and rebuilds them from scratch. Use this when a venv becomes corrupted or when you want a clean slate after dependency changes.

### Filtering by project

```bash
python -m finecode prepare-envs --project=package_a --project=package_b
```

Only prepares environments for the listed projects. Useful in a large workspaces with multiple projects when you've only changed dependencies for a subset of packages.

### Filtering by environment name

```bash
python -m finecode prepare-envs --env-names=dev_no_runtime
```

Restricts the `prepare_handler_envs` step (step 3) to the named environments. The `create_envs` and `prepare_runner_envs` steps still run for all envs — only the final dependency-installation step is filtered.

Useful when you've added a new handler in one env and want to update only that env without reinstalling everything.

---

## Calling actions directly

The three actions (`create_envs`, `prepare_runner_envs`, `prepare_handler_envs`) are standard FineCode actions and can be invoked individually via the WM API or `python -m finecode run`. This is useful when writing custom orchestration.

| Action | Source |
|---|---|
| `create_envs` | `finecode_extension_api.actions.create_envs.CreateEnvsAction` |
| `prepare_runner_envs` | `finecode_extension_api.actions.prepare_runner_envs.PrepareRunnerEnvsAction` |
| `prepare_handler_envs` | `finecode_extension_api.actions.prepare_handler_envs.PrepareHandlerEnvsAction` |

See [Built-in Actions reference](../reference/actions.md) for payload fields and result types.
