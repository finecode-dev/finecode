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
```

→ Creates `.venvs/dev_workspace/`, `.venvs/dev_no_runtime/`.

### Step 2 — `install_envs`

Installs the full dependency set into each virtualenv. This reads the `dependency-groups` entries and calls `install_deps_in_env` for each env, including `finecode_extension_runner` and all handler tool dependencies (e.g. ruff, mypy).

After this step every handler has all its dependencies available and can execute.

---

## Declaring an env

A FineCode environment is usually declared by adding an entry to `[dependency-groups]` in `pyproject.toml`. The group name becomes the environment name; handlers reference that name with `env = "<name>"`.

```toml
[dependency-groups]
dev  = ["pytest==7.4.*", "debugpy==1.8.*"]
docs = ["mkdocs==1.6.*", "mkdocs-material==9.7.*"]
```

`[dependency-groups]` is the canonical explicit place to declare environments, and `create_envs` / `install_envs` ultimately read that table to decide what to create and install. FineCode also synthesizes missing groups for env names referenced by action handlers or services, so an env used only through `env = "<name>"` is still created and installed even if it was not written explicitly under `[dependency-groups]`.

Even so, explicit `[dependency-groups]` entries are still preferred when you want the environment to be visible in the raw `pyproject.toml` and usable by standard tooling such as `uv sync --group=...` or `pip install --group=...`. This rule is documented in [ADR-0018](../adr/0018-pep735-groups-as-env-registry.md).

### Runtime dependencies

Environments that need the project's runtime dependencies reference the project itself by name — for example `dev = ["finecode", ...]`. This pulls `[project.dependencies]` transitively through the project package and keeps the runtime dependency list in exactly one place. Do not re-list the project's runtime deps inside the group.

### Workspace editable packages

In a workspace, local packages can be installed as editable installs. PEP 508 requirement strings cannot express editable installs from a local path, so FineCode provides a workspace-level mechanism in `finecode-workspace.toml` at the workspace root:

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

Any dependency whose package name matches a workspace editable package is automatically rewritten to an editable install from its declared path, across every env in every project. No per-env supplement tables are needed.

The resolved editable-packages set is the union of every discovered project (when `all_workspace_packages_editable` is `true`) and every explicit `editable_packages` entry.

### Installing the project under test

Some tools must import and exercise the project itself — a test runner is the
canonical example. An env opts into this with a scalar sibling to the
`dependencies` supplement:

```toml
[tool.finecode.env.dev]
install_project = true
```

This installs the project being configured — editable, from its own
directory — into that env in addition to its dependency-group packages. The
setting defaults to `false`; most FineCode envs are isolated tool envs
(`dev_no_runtime`, formatter/linter envs) that must not have the project on
their import path.

A preset that ships a project-importing tool (e.g. a test runner) sets
`install_project = true` for the env its handler runs in — the preset cannot
know the consuming project's package name, so this is the only way for it to
avoid a first-run `ModuleNotFoundError`. A user who does not want the project
installed into that env overrides it in their own `pyproject.toml`:

```toml
[tool.finecode.env.dev]
install_project = false
```

If the env's dependency group also names the project directly (the
[runtime dependencies](#runtime-dependencies) pattern above), the two are
idempotent: the editable install from `install_project` takes precedence over
the plain named requirement, and the project is installed once.

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

The `dev_workspace` env is special: it contains the FineCode packages that are needed to prepare the rest of the workspace. The handlers that implement `create_envs` and `install_envs` run from `dev_workspace` — which creates a bootstrapping constraint.

### Two-phase installation

`dev_workspace` installation happens in two distinct phases. The workspace root is the important case to understand: you run `bootstrap` once to create its `dev_workspace`, then `prepare-envs` can use that environment to prepare the rest of the workspace.

| Phase | When | Config source | What gets installed |
| --- | --- | --- | --- |
| 1 | `bootstrap` for the workspace root | Raw `pyproject.toml` only (no preset resolution) | The seed requirements listed directly in `[dependency-groups].dev_workspace` |
| 2 | `prepare-envs` after runners start | Merged config (presets resolved via the real venv runner) | Preset-contributed packages (`finecode_extension_api`, `finecode_jsonrpc`, extensions, …) |

Phase 1 installs only the requirements written directly in the `dev_workspace` group, because presets have not been resolved yet, for example:

```toml
[dependency-groups]
dev_workspace = [
    "finecode~=0.3.*",
    "finecode_extension_runner~=0.3.*",
    "finecode_dev_common_preset~=0.3.*",
]
```

After the root `dev_workspace` exists, FineCode can start its runner. `prepare-envs` then resolves presets, merges the full configuration, and runs Phase 2. The preset can then contribute the additional packages needed by its handlers and services.

In a multi-project workspace, subproject `dev_workspace` envs follow the same raw-then-merged pattern, but you do not run `bootstrap` for them manually. `prepare-envs` creates their raw `dev_workspace` envs automatically before starting their runners, then runs the preset-resolved install after those runners are available.

Editable installs are only relevant for packages that are local to your workspace and that you want FineCode to install from source. If you enable workspace editable packages in `finecode-workspace.toml`, those local packages are rewritten to editable installs automatically (see [Workspace editable packages](#workspace-editable-packages)). Published FineCode packages such as `finecode` and `finecode_extension_runner` remain ordinary dependency requirements unless you are developing FineCode itself in a local checkout.

### Workspace root bootstrap (one-time)

The workspace root's `dev_workspace` is the **seed** for everything. `prepare-envs` cannot run unless FineCode is already installed somewhere, so the workspace root's `dev_workspace` must be created before `prepare-envs` can run.

Use the `bootstrap` command — it handles this automatically using the invoking Python (e.g. the pipx/uvx ephemeral environment):

```bash
# With pipx (bundled with Python 3.13+):
pipx run finecode bootstrap

# With uv (also works when you have no Python — uv installs it):
uvx finecode bootstrap
```

> **Note:** `bootstrap` uses the built-in default handlers: `uv` for both environment creation and for dependency installation. If your project requires custom handlers for either action (e.g. a different package manager or venv backend), `bootstrap` is not suitable — you must bootstrap the `dev_workspace` manually (see the **Manual alternative** below) or via your own tooling.

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

For subprojects in the workspace, `prepare-envs` creates their `dev_workspace` envs automatically — **before** starting any subproject runners — using the workspace root's handler configuration:

1. `create_envs` (subproject `dev_workspace` envs) — create the venvs
2. `install_envs` (subproject `dev_workspace` envs) — install the raw `dev_workspace` requirements only, no preset resolution yet
3. Runners start in each `dev_workspace`
4. `install_envs` runs again — presets are now resolved and their contributed packages are installed

**Requirement:** the workspace root's `create_envs` and `install_envs` configuration must produce a valid `dev_workspace` for every subproject. In practice this is rarely a constraint: `dev_workspace` envs exist only to run FineCode and preset packages, so their setup is uniform across projects. If a subproject genuinely requires different handler configuration for either action, its `dev_workspace` must be bootstrapped manually the same way as the workspace root's.

Only after all `dev_workspace` envs exist are runners started, and only then can the remaining steps run across all envs.

---

## Automatic env repair

When an Extension Runner fails to apply a config update because a required package is missing from its env, the WM automatically reinstalls the env and restarts the ER — without requiring a manual `prepare-envs` run.

### Trigger

The ER signals the problem by returning error code `-32001` (`ENV_REINSTALL_NEEDED`) from `finecodeRunner/updateConfig`. This code is returned in two cases:

- A handler package is installed but its `finecode.activator` entry points are stale (editable install not re-registered after `pyproject.toml` change).
- A required Python module is not installed in the env at all.

The WM catches this, runs `CreateEnvsAction` + `InstallEnvsAction` for the affected env, then restarts the ER.

### Runner routing

The runner that executes `CreateEnvsAction` / `InstallEnvsAction` during auto-repair depends on which env is being fixed:

| Env being repaired | Executor runner |
|---|---|
| `dev_workspace` | Workspace **root's** dev_workspace runner |
| Any other env (e.g. `dev_no_runtime`) | The **subproject's own** dev_workspace runner |

**Why the split?** When `dev_workspace` needs repairing, the subproject's own runner does not exist yet — the root runner is the only available executor. For all other envs, the subproject's `dev_workspace` is already running and carries the correct project-local configuration (env specs, package lists), so it is the right executor.

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

For an ordinary, non-matrix env, this restricts the `install_envs` step (step 5) to the named environments. The `create_envs` step still runs for **all** non-matrix envs regardless of this flag.

**Why?** Virtualenvs must exist for every env — they are cheap to create and skip if already valid. Filtering at that step would leave envs in a broken state if they don't exist yet.

Useful when you've added a new handler in one env and want to update only that env without reinstalling everything.

## Bounding concurrency

`prepare-envs` fans work out at two independent points, and each spawns real OS processes:

1. **Across projects** — `create_envs`/`install_envs` run concurrently for every project in the workspace, each on its own Extension Runner (ER) process.
2. **Across envs, within one ER** — `install_envs` installs every env of a project concurrently, each running a package-manager subprocess (e.g. `uv install`). Interpreter-matrix envs (ADR-0047) make this worse, since one matrix env can expand into many concurrent children.

On a resource-constrained machine these two fan-outs compose multiplicatively — N projects × M envs concurrent subprocesses — and can starve the WM's own event loop. Both fan-outs are bounded to avoid this (ADR-0055).

### Layer 1 — concurrent projects

```bash
python -m finecode prepare-envs --max-concurrent-projects=2
```

Or via environment variable:

```bash
export FINECODE_WM_PREPARE_ENVS_MAX_CONCURRENT_PROJECTS=2
```

Priority: `--max-concurrent-projects` > the env var > the default formula (below). This is a **machine-bound** setting — it has no `finecode-workspace.toml` equivalent, because that file is shared/committed and a number tuned for one developer's machine would be wrong on everyone else's.

### Layer 2 — concurrent subprocesses per ER

Every subprocess spawned inside an ER (via `CommandRunner`, e.g. `uv install`) goes through a shared cap, configured as service config on `ICommandRunner` (ADR-0056):

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.icommandrunner.ICommandRunner"
source = "finecode_extension_runner.impls.command_runner.CommandRunner"
env = "dev_no_runtime"
config.max_concurrent_processes = 4
```

Since this is also a machine-bound tuning value rather than a project setting, put it in a gitignored `finecode-user.toml` instead of a committed `pyproject.toml` — see [`finecode-user.toml`](../configuration.md#finecode-usertoml).

### The default formula

When a layer's own cap is left unset, both layers default from the same formula:

1. Start from the machine's usable CPU budget: `len(os.sched_getaffinity(0))` (respects container CPU quotas/pinning, unlike `os.cpu_count()`), minus one core of headroom so the WM keeps a guaranteed scheduling slot even under full subprocess load.
2. Split that budget between the two layers via its square root, rather than handing each layer the full budget independently. The two layers compose multiplicatively in the worst case, so giving each the full budget would let their product overshoot the machine's real capacity by up to a squared factor (e.g. a 7-subprocess budget → 49 concurrent subprocesses if both layers used 7). The square-root split keeps the worst-case product close to the actual machine budget:

   | Machine budget | Per-layer default | Worst-case product |
   | --- | --- | --- |
   | 1 | 1 | 1 |
   | 3 | 2 | 4 |
   | 7 | 3 | 9 |
   | 15 | 4 | 16 |

A configured value of `0` or less at either layer is treated as `1` — a zero-sized concurrency limit would deadlock the affected step forever, not disable it.

### A third, separate cap: ER startup concurrency

The two layers above bound `create_envs`/`install_envs` specifically. A related but independent cap
bounds how many Extension Runner *processes* may be starting at once, regardless of which command
triggered the starts — including `prepare-envs`' own "start runners in each `dev_workspace`" step,
workspace init, and a matrixed `run`. It uses the same machine-budget formula but not the sqrt-split
(a single flat axis, not two composing layers), and is configured separately via
`FINECODE_WM_MAX_CONCURRENT_ER_STARTS`. See [ER startup concurrency](wm-server-internals.md#er-startup-concurrency)
(ADR-0063) for the full picture.

---

## `uv` cache placement in containers

The built-in `create_envs`/`install_envs` handlers (`fine_python_uv`) shell out to `uv`. `uv` avoids re-copying package files into every venv by hardlinking (or CoW-cloning) them out of its local cache directly into each venv's `site-packages` — this is what keeps N venvs from each consuming the full size of every shared dependency.

Hardlinks and CoW clones only work within a single filesystem. If your devcontainer (or any container setup) puts `uv`'s cache (`~/.cache/uv` by default) on a different filesystem than the venvs it populates, `uv` silently falls back to full copies for every package in every venv — no warning, no error. This is easy to hit by accident: a `docker-compose.yml`/`devcontainer.json` that bind-mounts the project as one volume (e.g. `.:/workspaces/myproject`) leaves the container's home directory — where `uv`'s default cache lives — on the container's own root/overlay filesystem, a different device from the bind-mounted workspace.

**Symptom:** every env (including matrix children like `testing@cpython-3.11`) grows by the full size of every sizeable dependency instead of sharing one cached copy. `uv` itself is a good example if `fine_python_uv` is present in an env — its own PyPI package bundles a ~60MB binary. Across a workspace with many projects × many envs, this adds up to tens of GB of pure duplication.

**Fix:** point `UV_CACHE_DIR` at a path on the same filesystem as your venvs, and gitignore it:

```yaml
# docker-compose.yml
environment:
  - UV_CACHE_DIR=/workspaces/myproject/.uv-cache
```

```gitignore
.uv-cache
```

**Verifying it worked:** check the link count on a file that should be shared, not its apparent size. `du -sh` on a single venv directory reports the file's full logical size regardless of hardlinking — it has no visibility into the fact that the same blocks are also claimed by the cache directory outside its traversal.

```bash
stat -c '%h' path/to/venv/bin/uv   # >1 means hardlinked; 1 means it was copied
```

---

## Calling actions directly

The two actions (`create_envs`, `install_envs`) are standard FineCode actions and can be invoked individually via the WM API or `python -m finecode run`. This is useful when writing custom orchestration.

| Action | Source |
| --- | --- |
| `create_envs` | `finecode_extension_api.actions.create_envs.CreateEnvsAction` |
| `install_envs` | `finecode_extension_api.actions.install_envs.InstallEnvsAction` |

See [Built-in Actions reference](../reference/actions.md) for payload fields and result types.
