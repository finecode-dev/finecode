# Getting Started

This guide walks you through installing FineCode, applying an existing preset, and running your first actions.

## Prerequisites

- Python 3.11–3.14
- pip 25.1 or newer (for `--group` support)

```bash
python -m pip install --upgrade pip
```

## 1. Add FineCode to your project

FineCode is installed into a dedicated `dev_workspace` virtual environment, separate from your project's runtime dependencies. This keeps tooling isolated.

Add the `dev_workspace` dependency group to your `pyproject.toml`:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*"]
```

Create the venv and install:

```bash
python -m venv .venvs/dev_workspace
source .venvs/dev_workspace/bin/activate   # Windows: .venvs\dev_workspace\Scripts\activate
python -m pip install --group="dev_workspace"
```

## 2. Add a preset

Presets bundle ready-made tool configurations. Add `fine_python_recommended` to get linting and formatting for Python:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]
```

Reinstall after updating the dependency group:

```bash
python -m pip install --group="dev_workspace"
```

### Available presets

| Preset | What it includes |
|---|---|
| `fine_python_recommended` | Ruff + Flake8 linting, Ruff formatter + isort |
| `fine_python_lint` | Ruff, Flake8, Pyrefly linting only |
| `fine_python_format` | Ruff formatter + isort only |

## 3. Enable the preset in config

Tell FineCode which preset to use:

```toml
[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

## 4. Prepare environments

FineCode runs each tool handler in its own virtual environment. Set them up with:

```bash
python -m finecode prepare-envs
```

This installs the handler dependencies (ruff, flake8, etc.) into isolated venvs under `.venvs/`.

## 5. Run actions

```bash
# Lint all projects in the workspace
python -m finecode run lint

# Check formatting (without modifying files)
python -m finecode run check_formatting

# Format all files
python -m finecode run format

# Run lint and check_formatting concurrently
python -m finecode run --concurrently lint check_formatting
```

## Next steps

- [IDE Integration](ide-integration.md) — set up the VSCode extension for real-time feedback
- [Configuration](configuration.md) — customize tool settings and override handler config
- [Concepts](concepts.md) — understand how Actions, Handlers, and Presets fit together
- [Creating an Extension](guides/creating-extension.md) — write your own tool integration
