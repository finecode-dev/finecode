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

### Workspace packages

In a workspace, local packages can be installed as editable installs, or from wheels built from the checkout. PEP 508 requirement strings cannot express editable installs from a local path, so FineCode provides a workspace-level mechanism in `finecode-workspace.toml` at the workspace root:

```toml
[workspace.workspace_packages]
# Defaults to true, so this table is optional: with no finecode-workspace.toml
# every discovered project is a workspace package.
all_projects = true

# Optional: explicit paths to treat as workspace packages — useful for
# vendored forks outside normal project discovery. Paths are relative to
# the workspace root.
extra = [
    "./vendored_forks/some_lib",
]
```

How they are installed is selected separately, per dev-env (exact key, then the `local`/`ci` bucket, then the bucket default — `editable` for non-`ci`, `wheel` for `ci`; the CLI flag wins). Both entries are the defaults, so this table is optional:

```toml
[workspace.workspace_packages_install]
local = "editable"
ci    = "wheel"
exclude = ["pkg-a"]
```

- `editable` rewrites a matching dependency to an editable install from its declared path, across every env in every project. No per-env supplement tables are needed.
- `wheel` builds one wheel per package with `build_python_artifact` into `<workspace-root>/.venvs/dev_workspace/cache/wheelhouse` and installs it as a direct `name @ file:///…whl` reference, so every env resolves the package to the artifact built from the checkout — never a PyPI release. Each package is built by **its own project's** builder, so a project's handler override and config apply.
- `exclude` keeps the named packages editable in every env and omits them from the wheelhouse — the escape hatch for a package no builder can turn into a wheel.

In wheel mode, a workspace package that has no wheel in the wheelhouse is an **error** naming the package and pointing at `prepare-envs`; it is never silently installed editable. Only packages listed in `exclude` install editable in wheel mode.

`prepare-envs --workspace-packages=wheel|editable` overrides the config for a single run. Wheel mode builds a workspace-wide wheelhouse, so it cannot be combined with `--project`: run `prepare-envs --workspace-packages=wheel` at the workspace root, or use the default editable mode for a `--project` run.

The resolved workspace-packages set is the union of every discovered project (unless `all_projects = false`) and every explicit `extra` entry.

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

Workspace packages are only relevant for packages that are local to your workspace. When you declare them in `finecode-workspace.toml`, those local packages are rewritten to editable installs or to wheels built from source, depending on the selected mode (see [Workspace packages](#workspace-packages)). Published FineCode packages such as `finecode` and `finecode_extension_runner` remain ordinary dependency requirements unless you are developing FineCode itself in a local checkout.

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

`--recreate` rebuilds the envs *discovery found* — it does not remove envs that are no longer declared. See [Orphaned environments](#orphaned-environments) below.

### Orphaned environments

An environment is **orphaned** when its `.venvs/` directory still exists but the project's resolved configuration no longer declares it. Nothing references it: no handler names it, no Extension Runner will ever start in it, and neither `prepare-envs` nor `prepare-envs --recreate` touches it, because both only act on envs that discovery found. It simply stays on disk.

This is not an edge case — it is the normal result of ordinary configuration changes:

- **renaming an env**, or dropping one from `[dependency-groups]`;
- **a preset change** that stops contributing an env;
- **converting a single-interpreter env to a matrix** (ADR-0047). This one is easy to miss, because it does not look like a removal. After expansion the matrix base name is gone from the configuration a handler sees — only the concrete children remain:

  ```toml
  [tool.finecode.env.testing]
  interpreters = ["3.11", "3.12", "3.13"]
  ```

  → declares `testing@cpython-3.11`, `testing@cpython-3.12`, `testing@cpython-3.13`. The `.venvs/testing` directory created before the matrix existed is now orphaned. A matrix env's children are each a full virtualenv, so the leftover base is easily hundreds of megabytes per project.

Orphan-ness is resolved **per project**: the same env name can be orphaned in one project and legitimately declared in another that has no matrix.

Check first with [`list_envs`](../reference/actions.md#list_envs):

```bash
python -m finecode run list_envs
```

```text
/workspaces/myrepo
  dev_workspace         declared  created
  testing@cpython-3.11  declared  created
  testing@cpython-3.14  declared  MISSING
  testing               ORPHANED  created
```

`MISSING` is not an orphan — it is a declared env whose venv has not been created yet (for example a matrix child excluded by [`default_interpreters`](#default-interpreter-subset)). Only `ORPHANED` rows are safe to delete.

Then remove them with [`remove_envs`](../reference/actions.md#remove_envs), which defaults to exactly the orphaned set:

```bash
python -m finecode run remove_envs
```

To remove a specific env instead, name it — a still-declared env requires `force`, and the env FineCode itself is running in is never removable:

```bash
python -m finecode run remove_envs --env-names='["stale_env"]'
python -m finecode run remove_envs --env-names='["dev_no_runtime"]' --force=true
python -m finecode prepare-envs   # recreate what you forced away
```

Removal is deliberately tolerant of damage — a half-created venv or one whose files lost write permission is exactly what you want gone — and a single undeletable env is reported without aborting the rest.

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

#### Matrix environments

For a matrix environment (ADR-0047 — one declaring an `interpreters` axis), the rule above changes: naming a concrete matrix child — or its base name, which expands to all of its children — restricts **both** `create_envs` and `install_envs` to the selected children (PRD-0003 AC8). Unselected children of that matrix are not created at all, since there is no point creating a venv for an interpreter nobody asked for in this run.

```bash
# Select every child of the "testing" matrix env.
python -m finecode prepare-envs --env=testing

# Select only the cpython@3.11 child.
python -m finecode prepare-envs --env=testing@cpython-3.11
```

A matrix base named by `--env` is always expanded to *all* of its children, ignoring that base's own `default_interpreters` policy (see below) — `--env` is more specific than a config default. A sibling matrix base *not* named by `--env` is unaffected by this and keeps applying its own config default (or its full axis, if it has none).

### Filtering by interpreter

```bash
python -m finecode prepare-envs --interpreter=3.11
python -m finecode prepare-envs --interpreter=pypy@3.11
```

Restricts every matrix environment's interpreter axis to the named interpreter(s), the same way for both `create_envs` and `install_envs`. `--interpreter` is repeatable to select more than one interpreter. Values may be the canonical `<impl>@<version>` form or a bare version, which is shorthand for `cpython@<version>`.

`--interpreter` can be combined with `--env`: the effective selection is the intersection of the two — e.g. `--env=testing --interpreter=3.12` selects only `testing`'s `cpython@3.12` child. An `--interpreter` value that doesn't exist in a given matrix env's axis simply contributes nothing for that env (it is not an error by itself — see below for when a selector *is* rejected).

Non-matrix envs are unaffected by `--interpreter`; they are always created, and installed unless excluded by `--env`.

### Default interpreter subset

A matrix environment can declare a default interpreter subset per dev-env, so that a plain `prepare-envs` run (no `--env`/`--interpreter`) still narrows the axis automatically:

```toml
[tool.finecode.env.testing]
interpreters = ["3.11", "3.12", "3.13"]

[tool.finecode.env.testing.default_interpreters]
local = "newest"
ci    = "all"
```

Each key is either an exact dev-env (`ide`/`cli`/`ai`/`git_hook`/`ci`) or one of the two buckets `local`/`ci` (see lookup below — `local` is a bucket name, not a dev-env); each value is a policy:

- `"all"` — the full interpreter axis (this is also the implicit default when `default_interpreters` is absent — R7).
- `"newest"` / `"oldest"` — the interpreter(s) at the maximum/minimum declared version. If two implementations share that version (e.g. `cpython@3.13` and `pypy@3.13`), both are selected — a shared version is never arbitrarily dropped.
- An explicit list of interpreter strings (canonical or version-only shorthand), e.g. `["cpython@3.11", "cpython@3.13"]`.

Lookup for the active dev-env `D` (see [dev environment detection](../cli.md#dev-environment-detection) — `ide`/`cli`/`ai`/`git_hook`/`ci`) tries, in order: the exact key `D`, then the bucket key (`"ci"` if `D == "ci"`, otherwise `"local"`), then falls back to `"all"`. In the example above, `local = "newest"` covers `ide`/`cli`/`ai`/`git_hook`, while `ci = "all"` covers `ci` — a common pattern where local development only needs the newest interpreter, but CI verifies every interpreter in the matrix.

An explicit `--interpreter` selector always overrides the config default outright, for every matrix base. A config default (or its explicit-list policy) that names an interpreter outside the env's declared axis is rejected at resolution time with a clear error.

### `run` uses the same selection

`python -m finecode run` accepts the same `--env`/`--interpreter` selectors, with identical semantics (ADR-0050), to restrict which interpreter variants of a matrixed action actually execute — see [CLI reference — `run`](../cli.md#run). The config-declared `default_interpreters` policy applies there too: a plain `run` (no selectors) executes only the dev-env's default subset of a matrix (e.g. just the newest interpreter locally), while `ci` runs the full axis by default, exactly mirroring `prepare-envs`. Selection is resolved once per project (via `env_selection.resolve_selected_interpreters`) and passed down to whichever fan-out site handles the request — `matrix_runner` (non-streaming) or `matrix_streaming` (CLI / IDE streaming) — so both paths filter identically.

---

## Bounding concurrency

`prepare-envs` fans work out across projects and across envs, and each fan-out ultimately spawns
real OS processes. All of them draw from one machine-wide budget (ADR-0090): the WM leases
subprocess *work slots* to each ER for the duration of an action run, and every spawn inside that
ER — `CommandRunner` subprocesses (e.g. `uv install`) and `ProcessExecutor` pool workers alike —
draws from the ER's leased gate. The work budget is the work half of the combined
subprocess-concurrency budget: one machine-bound total, split into an ER-startup cap and this work
cap so their sum always leaves a core free for the WM's event loop
([Combined subprocess-concurrency budget](wm-server-internals.md#combined-subprocess-concurrency-budget),
ADR-0093). See [Process budget](wm-server-internals.md#process-budget) for the mechanics.

A nested run (one asked for by another run's fan-out) is always granted at least one slot, so it
can always make progress; the whole machine is never over-subscribed in the common, non-nested
case.

`prepare-envs` closes the gap for its own big fan-out: steps 5 and 6 give each project's
`create_envs` / `install_envs` run a budget that waits and asks for `max(1, W // N)` slots
(`W` = the work cap, `N` = the number of in-scope projects). With more projects than slots every
project takes one, so about `W` projects build envs at once; with no more projects than slots a
single project still gets the whole work cap. Step 3's batched `dev_workspace` bootstrap and
auto-repair keep the ER's full request, because they run *inside* other dispatches and must not
wait on slots their ancestors hold.

### Optional per-ER ceiling

A project that wants to pin one noisy ER below the machine budget can still set a local ceiling as
service config on `ICommandRunner` (ADR-0056):

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.icommandrunner.ICommandRunner"
source = "finecode_extension_runner.impls.command_runner.CommandRunner"
env = "dev_no_runtime"
config.max_concurrent_processes = 4
```

This is an *additional* ceiling on top of the shared gate, not a replacement for it, and defaults
to unset. Since it is a machine-bound tuning value rather than a project setting, put it in a
gitignored `finecode-user.toml` instead of a committed `pyproject.toml` — see
[`finecode-user.toml`](../configuration.md#finecode-usertoml).

### A separate budget: ER startup concurrency

The process budget above bounds subprocess *work*. A related but independent cap bounds how many
Extension Runner *processes* may be starting at once, regardless of which command triggered the
starts — including `prepare-envs`' own "start runners in each `dev_workspace`" step, workspace
init, and a matrixed `run`. It is deliberately separate: the WM starts runners lazily *inside* a
fan-out, so a run holding process-budget slots must never block on the same budget to start the ER
it is fanning out into (ADR-0063, ADR-0090).

It is sized as the other half of the combined budget — set `FINECODE_MAX_CONCURRENT_PROCESSES` to
size the total and both halves move together. There is no separate ER-startup knob. See
[Combined subprocess-concurrency budget](wm-server-internals.md#combined-subprocess-concurrency-budget)
(ADR-0093).

---

## `uv` cache placement in containers

The built-in `create_envs`/`install_envs` handlers (`fine_python_uv`) shell out to `uv`. `uv` avoids re-copying package files into every venv by hardlinking (or CoW-cloning) them out of its local cache directly into each venv's `site-packages` — this is what keeps N venvs from each consuming the full size of every shared dependency.

Hardlinks and CoW clones only work within a single filesystem. If your devcontainer (or any container setup) puts `uv`'s cache (`~/.cache/uv` by default) on a different filesystem than the venvs it populates, `uv` prints `Failed to hardlink files; falling back to full copy. This may lead to degraded performance.` and continues with full copies for every package in every venv. This is easy to hit by accident: a `docker-compose.yml`/`devcontainer.json` that bind-mounts the project as one volume (e.g. `.:/workspaces/myproject`) leaves the container's home directory — where `uv`'s default cache lives — on the container's own root/overlay filesystem, a different device from the bind-mounted workspace.

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

CI does the same, into a per-job scratch directory that `actions/cache` carries across runs: `UV_CACHE_DIR=$RUNNER_TEMP/uv-cache` in the three `prepare-envs` jobs (`build`, `audit-private`, `deploy`). `RUNNER_TEMP` is on the workspace volume and is wiped between jobs, so the cache is restored only when the venvs cache misses (a hit means nothing installs) and saved only after a successful install. Before saving, the workspace packages are removed with `uv cache clean <names>` followed by `uv cache prune`, so the persisted entry holds only third-party PyPI artifacts; the `audit-private` job uses a separate key prefix so a public run can never restore an entry containing private-package sources. The cache key carries a `YYYY-MM` generation prefix, which starts a fresh chain each month and bounds the wheels of superseded versions that plain `prune` never removes.

**Verifying it worked:** check the link count on a file that should be shared, not its apparent size. `du -sh` on a single venv directory reports the file's full logical size regardless of hardlinking — it has no visibility into the fact that the same blocks are also claimed by the cache directory outside its traversal.

```bash
# Linux:
stat -c '%h' path/to/venv/bin/uv   # >1 means hardlinked; 1 means it was copied
# Windows:
fsutil hardlink list path\to\venv\Scripts\uv.exe   # more than one path listed means hardlinked
```

On macOS, APFS uses copy-on-write clones rather than hardlinks, so the link count stays `1` even when sharing works. The signal there is the *absence* of the `Failed to hardlink files; falling back to full copy` warning, not the link count.

---

## Calling actions directly

The two actions (`create_envs`, `install_envs`) are standard FineCode actions and can be invoked individually via the WM API or `python -m finecode run`. This is useful when writing custom orchestration.

| Action | Source |
| --- | --- |
| `create_envs` | `finecode_extension_api.actions.create_envs.CreateEnvsAction` |
| `install_envs` | `finecode_extension_api.actions.install_envs.InstallEnvsAction` |

See [Built-in Actions reference](../reference/actions.md) for payload fields and result types.
