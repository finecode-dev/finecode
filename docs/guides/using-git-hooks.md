# Using Git Hooks

Git hooks let you run automation at specific points in the Git workflow. FineCode currently installs a git `pre-commit` hook, which runs the configured `precommit` action on staged files before each commit.

## Installing the hook

```sh
python -m finecode run install_git_hooks
```

This writes `.git/hooks/pre-commit` and makes it executable. The hook is a small Python script that calls `python -m finecode run --dev-env=git_hook precommit` at commit time.

If a pre-commit hook file already exists, the command skips it and warns you. To overwrite it:

```sh
python -m finecode run install_git_hooks --force=true
```

## Configuring what the hook runs

Installing the git hook only connects Git to FineCode. You also need to configure the `precommit` action that the hook will run.

The two common options are:

### Option 1: Use the `fine_precommit` preset

`fine_precommit` is the ready-made option. It registers the `precommit` action and default handlers that:

1. discover staged files
2. run lint checks on those files
3. check formatting without applying autofixes

Add it to your project's `pyproject.toml`:

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" },
    { source = "fine_precommit" },
]
```

Then prepare the environments needed by the preset:

```sh
python -m finecode prepare-envs
```

### Option 2: Declare `precommit` yourself

If you want a custom hook workflow, define the `precommit` action and only the handlers you need:

```toml
[tool.finecode.action.precommit]
source = "finecode_extension_api.actions.PrecommitAction"

[[tool.finecode.action.precommit.handlers]]
name = "staged_files_discovery"
source = "finecode_builtin_handlers.StagedFilesDiscoveryHandler"
env = "dev_workspace"
dependencies = ["finecode_builtin_handlers~=0.2.0a0"]

[[tool.finecode.action.precommit.handlers]]
name = "mypy_precommit_bridge"
source = "fine_python_mypy.MypyPrecommitBridgeHandler"
env = "dev_workspace"
dependencies = ["fine_python_mypy~=0.1.0"]
```

Keep `StagedFilesDiscoveryHandler` first so later handlers receive the staged file list. Bridge handlers run in the order they are declared.

## Adding another bridge handler

If `precommit` is already configured and you just want to extend it, register another bridge handler in `pyproject.toml`:

```toml
[[tool.finecode.action.precommit.handlers]]
name = "mypy_precommit_bridge"
source = "fine_python_mypy.MypyPrecommitBridgeHandler"
env = "dev_workspace"
dependencies = ["fine_python_mypy~=0.1.0"]
```

Handlers execute in the order they are declared. No other configuration changes are needed.

## What the hook checks

Only files that are staged (`git add`ed) are checked. Deleted files are excluded automatically.

> **V1 limitation:** checks run against the working-tree version of each file, not the git index. If you have partially staged files, the full file is checked. Stash/unstash support is planned in future.

## Uninstalling

```sh
python -m finecode run uninstall_git_hooks
```

This removes `.git/hooks/pre-commit` only if it was installed by FineCode (identified by a marker comment inside the file). If the file was created by another tool, FineCode refuses to delete it and warns you.
