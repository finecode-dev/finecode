# Developing FineCode

This guide is for developers contributing to FineCode itself — the monorepo structure, conventions, and workflows used internally.

## Repository structure

The repo is a monorepo. Each package has its own `pyproject.toml`. The root directory is the workspace.

```text
finecode/                          # Main package (Workspace Manager)
finecode_extension_api/            # Public API for extension authors
finecode_extension_runner/         # Extension execution engine
finecode_jsonrpc/                  # JSON-RPC client/transport layer
finecode_httpclient/               # HTTP client for extensions
finecode_builtin_handlers/         # Built-in action handlers
extensions/                        # Extension packages (ruff, flake8, mypy, ...)
presets/                           # Preset packages (recommended, lint, format)
finecode_dev_common_preset/        # Preset used for developing FineCode itself
tests/                             # Test suite
```

## Setting up the development environment

```bash
# From the repo root, inside the dev_workspace venv:
python -m finecode prepare-envs
```

## Running checks

```bash
python -m finecode run lint
python -m finecode run check_formatting
pytest tests/
```

## Dependency lock files

FineCode uses [pylock.toml](https://packaging.python.org/en/latest/specifications/pylock-toml/) lock files for reproducible dependency installation.

### Why lock files

Without lock files, `prepare-envs` resolves dependency versions from the ranges declared in `pyproject.toml` at install time. This means two developers (or CI runs) can end up with different versions depending on when they ran the command. Lock files pin exact versions for reproducible environments.

### Lock files are environment-specific

Each FineCode environment (`dev_workspace`, `dev_no_runtime`, `runtime`, etc.) has its own set of dependencies, so each needs its own lock file:

```text
pylock.<env_name>.toml
```

For example:

```text
myproject/
  pyproject.toml
  pylock.dev_workspace.toml
  pylock.dev_no_runtime.toml
  pylock.runtime.toml
```

### Lock files are platform- and Python version-specific

A lock file records the exact dependency resolution for one platform and one Python version. The same `pyproject.toml` can resolve differently on Linux vs macOS, or Python 3.12 vs 3.13.

If the project targets a single platform and Python version, one lock file per env is enough. For multiple targets, encode platform and version into the file name (the `<name>` segment in `pylock.<name>.toml` must not contain dots):

```text
myproject/
  pyproject.toml
  locks/
    pylock.dev_workspace-linux-py312.toml
    pylock.dev_workspace-linux-py313.toml
    pylock.dev_workspace-macos-py312.toml
    pylock.dev_no_runtime-linux-py312.toml
    ...
```

### Generating lock files

Use the `lock_dependencies` action:

```bash
python -m finecode run lock_dependencies \
    --src_artifact_def_path=pyproject.toml \
    --output_path=pylock.dev_workspace.toml
```

For the Python ecosystem, the `PipLockDependenciesHandler` runs `pip lock` under the hood.

### Installing from lock files

The `PrepareEnvsInstallDepsFromLockHandler` is an alternative to `PrepareEnvsInstallDepsHandler`. Instead of reading dependency versions from `pyproject.toml`, it parses the lock file and passes the pinned versions to `install_deps_in_env`.

By default it looks for `pylock.<env_name>.toml` next to the project's `pyproject.toml`. If a lock file is not found for an env, it is skipped with a warning.

### Lock files in CI

Lock files should be committed to the repository. CI should install from them, not regenerate them:

```bash
# CI installs from existing lock files — reproducible
python -m finecode prepare-envs
```

To update lock files, run `lock_dependencies` locally or in a scheduled CI job and commit the result. For multi-platform projects, use a CI matrix to generate lock files on each target platform.


## Code Style

### Typing

- type the code
-- use complete types, no holes in generics like `list` instead of `list[int]`

### Imports

- keep imports at the top of the module
- keep imports at the root level of module
-- there are 2 exceptions:
    - you need to avoid circle dependency (usually it means there is a problem in code structure)
    - you want to avoid loading the module on startup (e.g. don't import all CLI command handlers if only one is needed for current CLI call)
  
### Exports

- explicitly export public module members using `__all__`
-- it may not contain dynamic elements, only literal strings
