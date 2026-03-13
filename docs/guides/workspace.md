# Multi-Project Workspace

FineCode natively supports workspaces containing multiple source artifacts. This is common in monorepos where each package is a separate source artifact.

## Structure

A workspace is a set of related source artifacts. Often this is a single directory root, but IDEs and clients can provide multiple workspace roots. Each source artifact has its own `pyproject.toml` with `[tool.finecode]`:

!!! note
    The CLI uses a single workspace root (`cwd` or `--workdir`). IDEs/LSP clients can provide multiple roots, and the Workspace Manager treats them as one workspace.

```
my_workspace/
    pyproject.toml             ← workspace-level (optional)
    package_a/
        pyproject.toml         ← source artifact A
        src/package_a/
    package_b/
        pyproject.toml         ← source artifact B
        src/package_b/
    common_preset/             ← shared preset package
        pyproject.toml
        common_preset/
            preset.toml
```

## Running actions across all projects

Run from the workspace root to target all source artifacts:

```bash
python -m finecode run lint
```

FineCode discovers all `pyproject.toml` files under the workspace root, finds those with `[tool.finecode]`, and runs the action in each source artifact.

To run concurrently across projects:

```bash
python -m finecode run --concurrently lint check_formatting
```

## Filtering to specific source artifacts

```bash
# Single source artifact
python -m finecode run --project=package_a lint

# Multiple source artifacts
python -m finecode run --project=package_a --project=package_b lint
```

When `--project` is specified, the action must exist in all listed source artifacts.

## Sharing configuration across source artifacts

The recommended approach for sharing config is a **local preset package** in the workspace. Each source artifact installs it as a dependency and references it in `pyproject.toml`.

**Why a package, not hierarchical config?**

- Source artifacts don't depend on workspace directory structure — they can be moved or extracted without changing tool config
- Configuration is fully explicit: the complete config is visible inside each subproject
- No implicit workspace-root lookup needed

**Example — shared lint configuration:**

```
my_workspace/
    my_lint_config/
        pyproject.toml
        my_lint_config/
            preset.toml    ← declares ruff, mypy handlers with shared settings
    package_a/
        pyproject.toml     ← references my_lint_config as a preset
    package_b/
        pyproject.toml     ← references my_lint_config as a preset
```

```toml
# package_a/pyproject.toml
[dependency-groups]
dev_workspace = [
    "finecode==0.3.*",
    "my_lint_config",     # local package
]

[tool.finecode.env.dev_workspace.dependencies]
my_lint_config = { path = "../my_lint_config", editable = true }

[tool.finecode]
presets = [{ source = "my_lint_config" }]
```

## Saving and reading action results

Results of actions are saved to `<venv>/cache/finecode/results/<action>.json`, keyed by source artifact path. This makes it easy to collect results from all source artifacts in CI:

```bash
python -m finecode run --concurrently lint check_formatting
cat .venvs/dev_workspace/cache/finecode/results/lint.json
```

To opt out of saving results:

```bash
python -m finecode run --no-save-results lint
```

## CI usage

```bash
# Run lint and formatting check in all source artifacts, fail if any fails
python -m finecode run --concurrently lint check_formatting

# Save results for later processing
python -m finecode run lint
cat .venvs/dev_workspace/cache/finecode/results/lint.json
```

To pass results between CI steps via environment variables (legacy approach):

```bash
python -m finecode run --save-results-to-env build_artifact
# Result is available as FINECODE_RESULT__BUILD_ARTIFACT
```
