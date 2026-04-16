# Preparing Environments

FineCode runs handlers in purpose-specific virtual environments. Handlers that share the same `env` name (e.g. `dev_no_runtime`) run in the same virtualenv. Before handlers can execute, their environments must exist and contain the right dependencies. This guide explains how that process works and how to control it.

## The two-step sequence

Environment preparation is split into two distinct actions that must run in order:

```
create_envs  →  install_envs
```

### Step 1 — `create_envs`

Creates the virtual environments (`.venvs/<env_name>/`) discovered from the project's effective `dependency-groups`. No packages are installed yet.

Each env name found in `[dependency-groups]` becomes a virtualenv:

```toml
[dependency-groups]
dev_workspace    = ["finecode==0.3.*", ...]
dev_no_runtime   = ["fine_python_ruff~=0.2.0", ...]
runtime          = ["fastapi>=0.100", ...]
```

→ Creates `.venvs/dev_workspace/`, `.venvs/dev_no_runtime/`, `.venvs/runtime/`.

### Step 2 — `install_envs`

Installs the full dependency set into each virtualenv. This reads the `dependency-groups` entries and calls `install_deps_in_env` for each env, including `finecode_extension_runner` and all handler tool dependencies (e.g. ruff, mypy).

After this step every handler has all its dependencies available and can execute.

---

## Declaring an env

A FineCode environment is usually declared by adding an entry to `[dependency-groups]` in `pyproject.toml`. The group name becomes the environment name; handlers reference that name with `env = "<name>"`.

```toml
[dependency-groups]
dev  = ["finecode", "pytest==7.4.*", "debugpy==1.8.*"]
docs = ["mkdocs==1.6.*", "mkdocs-material==9.7.*"]
```

`[dependency-groups]` is the canonical explicit place to declare environments, and `create_envs` / `install_envs` ultimately read that table to decide what to create and install. FineCode also synthesizes missing groups for env names referenced by action handlers or services, so an env used only through `env = "<name>"` is still created and installed even if it was not written explicitly under `[dependency-groups]`.

Even so, explicit `[dependency-groups]` entries are still preferred when you want the environment to be visible in the raw `pyproject.toml` and usable by standard tooling such as `uv sync --group=...` or `pip install --group=...`. This rule is documented in [ADR-0018](../adr/0018-pep735-groups-as-env-registry.md).

### Runtime dependencies

Environments that need the project's runtime dependencies reference the project itself by name — for example `dev = ["finecode", ...]`. This pulls `[project.dependencies]` transitively through the project package and keeps the runtime dependency list in exactly one place. Do not re-list the project's runtime deps inside the group.

### Editable path installs

PEP 508 requirement strings cannot express editable installs from a local path. For development workspaces that need them, add a companion table under `[tool.finecode.env.<name>.dependencies]`:

```toml
[dependency-groups]
dev = ["finecode", "pytest==7.4.*"]

[tool.finecode.env.dev.dependencies]
finecode               = { path = "./",                       editable = true }
finecode_extension_api = { path = "./finecode_extension_api", editable = true }
```

The companion table is a **supplement**: FineCode's install logic rewrites matching entries in the group to use the editable local path. It does not introduce new environments by itself, and it is ignored by tools that only understand PEP 735 (`uv sync --group=dev`, `pip install --group=dev`). The env must still come from either a `[dependency-groups]` entry of the same name or from a handler/service that references that `env` name — the supplement alone is not enough.

### Composing environments

To reuse a group inside another group, use the standard PEP 735 `include-group`:

```toml
[dependency-groups]
docs = ["mkdocs==1.6.*"]
dev  = [{ include-group = "docs" }, "pytest==7.4.*"]
```

Because every FineCode environment is also a real `[dependency-groups]` entry, `include-group` always resolves and standard tooling accepts the file unchanged. FineCode does not provide a separate composition primitive.

---

## The `dev_workspace` bootstrap

The `dev_workspace` env is special: it contains FineCode itself and the preset packages. The handlers that implement `create_envs` and `install_envs` live inside `dev_workspace` — which creates a bootstrapping constraint.

### Workspace root bootstrap (one-time)

The workspace root's `dev_workspace` is the **seed** for everything. `prepare-envs` cannot run unless FineCode is already installed somewhere, so the workspace root's `dev_workspace` must be created before `prepare-envs` can run.

Use the `bootstrap` command — it handles this automatically using the invoking Python (e.g. the pipx/uvx ephemeral environment):

```bash
# With pipx (bundled with Python 3.13+):
pipx run finecode bootstrap

# With uv (also works when you have no Python — uv installs it):
uvx finecode bootstrap
```

> **Note:** `bootstrap` uses the built-in default handlers: `virtualenv` for environment creation and `pip` for dependency installation. If your project requires custom handlers for either action (e.g. a different package manager or venv backend), `bootstrap` is not suitable — you must bootstrap the `dev_workspace` manually (see the **Manual alternative** below) or via your own tooling.

To delete and recreate an existing `dev_workspace`:

```bash
pipx run finecode bootstrap --recreate
```

**Manual alternative** (requires pip 25.1+):

```bash
python -m venv .venvs/dev_workspace
source .venvs/dev_workspace/bin/activate   # Windows: .venvs\dev_workspace\Scripts\activate
python -m pip install --group="dev_workspace"
```

See [Getting Started](../getting-started.md) for the full first-time setup sequence.

### Subproject bootstrap (automated by `prepare-envs`)

For subprojects in the workspace, `prepare-envs` creates their `dev_workspace` envs automatically — **before** starting any runners — using the workspace root's handler configuration:

1. `create_envs` (subproject `dev_workspace` envs) — create the venvs
2. `install_envs` (subproject `dev_workspace` envs) — install FineCode + presets

**Requirement:** the workspace root's `create_envs` and `install_envs` configuration must produce a valid `dev_workspace` for every subproject. In practice this is rarely a constraint: `dev_workspace` envs exist only to run FineCode and preset packages, so their setup is uniform across projects. If a subproject genuinely requires different handler configuration for either action, its `dev_workspace` must be bootstrapped manually the same way as the workspace root's.

Only after all `dev_workspace` envs exist are runners started, and only then can the remaining steps run across all envs.

---

## CLI command

The `prepare-envs` command runs the full sequence automatically:

```bash
python -m finecode prepare-envs
```

This is the only command most users need. It:

1. Discovers all projects in the workspace
2. Bootstraps `dev_workspace` for each subproject (`create_envs` + `install_envs`, using workspace root config)
3. Starts Extension Runners
4. Runs `create_envs` across all projects
5. Runs `install_envs` across all projects

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
python -m finecode prepare-envs --env=dev_no_runtime
```

Restricts the `install_envs` step (step 5) to the named environments. The `create_envs` step still runs for **all** envs regardless of this flag.

**Why?** Virtualenvs must exist for every env — they are cheap to create and skip if already valid. Filtering at that step would leave envs in a broken state if they don't exist yet.

Useful when you've added a new handler in one env and want to update only that env without reinstalling everything.

---

## Calling actions directly

The two actions (`create_envs`, `install_envs`) are standard FineCode actions and can be invoked individually via the WM API or `python -m finecode run`. This is useful when writing custom orchestration.

| Action | Source |
|---|---|
| `create_envs` | `finecode_extension_api.actions.create_envs.CreateEnvsAction` |
| `install_envs` | `finecode_extension_api.actions.install_envs.InstallEnvsAction` |

See [Built-in Actions reference](../reference/actions.md) for payload fields and result types.
