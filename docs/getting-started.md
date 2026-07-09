# Getting Started

This guide walks you through installing FineCode, choosing your features, and running your first actions.

## Prerequisites

- Python 3.11–3.14 **or** [uv](https://docs.astral.sh/uv/) (which can install Python for you)

No Python yet? Install `uv` (a single binary, no Python needed):

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 1. Add FineCode to your project

FineCode is installed into a dedicated `dev_workspace` virtual environment, separate from your project's runtime dependencies. This keeps tooling isolated.

Add the `dev_workspace` dependency group to your `pyproject.toml`:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*"]
```

Bootstrap the `dev_workspace` environment:

```bash
# Recommended — works with pipx (bundled with Python 3.13+) or uv:
pipx run finecode bootstrap
# or
uvx finecode bootstrap
```

This creates `.venvs/dev_workspace/` with FineCode installed, using the exact
versions specified in your `pyproject.toml`.

Activate it before running subsequent `python -m finecode` or `python -m pip` commands:

```bash
source .venvs/dev_workspace/bin/activate   # Windows: .venvs\dev_workspace\Scripts\activate
```

**Manual alternative** (if you prefer not to use pipx/uvx — requires pip 25.1+):

```bash
python -m venv .venvs/dev_workspace
source .venvs/dev_workspace/bin/activate   # Windows: .venvs\dev_workspace\Scripts\activate
python -m pip install --group="dev_workspace"
```

## 2. Choose your features

FineCode organizes tooling into **features**, each packaged as a **preset**. You have two options:

**Option A: Get everything recommended** — start with `fine_python_recommended` for a complete Python tooling setup:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]
```

**Option B: Pick individual features** — install only what you need:

```toml
[dependency-groups]
dev_workspace = [
    "finecode==0.3.*",
    "fine_python_lint==0.3.*",
    "fine_python_format==0.3.*",
]
```

### Feature catalog

| Feature | Preset | What you get |
|---------|--------|--------------|
| **All recommended** | `fine_python_recommended` | Everything below |
| Linting | `fine_python_lint` | Ruff + Flake8 + Pyrefly |
| Formatting | `fine_python_format` | Ruff formatter + isort |
| Testing | `fine_python_test` | pytest integration (run and list tests) |
| IDE language support | `fine_python_symbol_info` | Hover, go-to-definition, references, type definition (via Pyrefly) |
|  | `fine_python_code_hierarchy` | Call hierarchy, type hierarchy (via Pyrefly) |
| TOML support | `fine_toml_recommended` | TOML linting, formatting, semantic tokens (via Tombi) |

*Semantic tokens (enhanced syntax highlighting) are included when using `fine_python_recommended`.*

Reinstall after updating the dependency group:

```bash
python -m pip install --group="dev_workspace"
```

## 3. Enable the preset in config

Tell FineCode which preset(s) to use:

```toml
[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

Or if you picked individual features:

```toml
[tool.finecode]
presets = [
    { source = "fine_python_lint" },
    { source = "fine_python_format" },
]
```

This goes in the project's `pyproject.toml`. You can also put project configuration in a separate `finecode.toml` file at the project root (see [Configuration](configuration.md#finecodetoml)).

**Multiple projects?** Create a `finecode-workspace.toml` at the workspace root to declare workspace-scoped settings for multi-project workspaces, such as which local packages should be installed as editable installs:

```toml
[workspace]
all_workspace_packages_editable = true
```

## 4. Prepare environments

FineCode runs each tool handler in its own virtual environment. Set them up with:

```bash
python -m finecode prepare-envs
```

This creates purpose-specific venvs under `.venvs/` and installs handler dependencies (e.g. ruff, flake8, etc.) into them.

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

## 6. Customize your setup

Presets give you sensible defaults. Override anything in your `pyproject.toml` — no Python code needed.

### Change tool configuration

Add stricter ruff rules or adjust settings for any handler:

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["UP", "SIM", "PTH"]
```

### Disable a tool you don't need

```toml
[[tool.finecode.action_handler]]
source = "fine_python_flake8.Flake8LintFilesHandler"
enabled = false
```

### Pin a tool version

```toml
[tool.finecode.extension.fine_python_ruff]
dependencies_override = ["ruff==0.15.*"]
```

For the full configuration reference, see [Configuration](configuration.md).

## Next steps

- [Supported Development Environments](supported-environments.md) — see what FineCode supports today across VSCode, CLI, CI, git hooks, and AI assistants
- [IDE and MCP Setup](getting-started-ide-mcp.md) — connect FineCode to VSCode and MCP-compatible AI clients
- [Configuration](configuration.md) — full configuration reference
- [Concepts](concepts.md) — understand how Actions, Handlers, and Presets fit together
- [Creating an Extension](guides/creating-extension.md) — write your own tool integration
