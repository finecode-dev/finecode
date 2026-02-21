# Creating a Preset

A **Preset** is a Python package that bundles action and handler declarations into a reusable, distributable configuration. Teams use presets to standardize tooling across projects without duplicating config.

## 1. Create the package

```
my_preset/
    pyproject.toml
    my_preset/
        __init__.py
        preset.toml
```

**`pyproject.toml`**:

```toml
[project]
name = "my_preset"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []   # no runtime dependencies needed for a preset-only package

[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"
```

**`my_preset/__init__.py`** — can be empty:

```python
```

## 2. Declare actions in `preset.toml`

The `preset.toml` file lives inside the package directory (next to `__init__.py`). It uses the same `[tool.finecode.*]` syntax as `pyproject.toml`.

```toml
# my_preset/my_preset/preset.toml

[tool.finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffLintFilesHandler", env = "dev_no_runtime", dependencies = [
        "fine_python_ruff~=0.2.0",
    ] },
    { name = "mypy", source = "fine_python_mypy.MypyLintHandler", env = "dev_no_runtime", dependencies = [
        "fine_python_mypy~=0.3.0",
    ] },
]

[tool.finecode.action.format]
source = "finecode_extension_api.actions.format.FormatAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffFormatFilesHandler", env = "dev_no_runtime", dependencies = [
        "fine_python_ruff~=0.2.0",
    ] },
    { name = "save", source = "finecode_builtin_handlers.SaveFormatFilesHandler", env = "dev_no_runtime", dependencies = [
        "finecode_builtin_handlers~=0.2.0",
    ] },
]

# Set default handler configs
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["B", "I"]
config.line_length = 88
```

## 3. Use the preset in a project

Install the preset package (e.g. from PyPI or a local path) into the `dev_workspace` dependency group:

```toml
# User's pyproject.toml
[dependency-groups]
dev_workspace = [
    "finecode==0.3.*",
    "my_preset==0.1.*",
]

[tool.finecode]
presets = [{ source = "my_preset" }]
```

Then run:

```bash
python -m pip install --group="dev_workspace"
python -m finecode prepare-envs
```

## 4. Allow users to override your defaults

Users can add `[[tool.finecode.action_handler]]` entries in their own `pyproject.toml` to override any config you set in `preset.toml`. Your preset's values are the baseline; user config always wins.

Users can also:

- Add more handlers to actions you declared
- Replace all handlers with `handlers_mode = "replace"`
- Disable specific handlers with `disabled = true`

## Composing multiple presets

A project can activate multiple presets. They are applied in order, and later preset handlers are added after earlier ones:

```toml
[tool.finecode]
presets = [
    { source = "my_lint_preset" },
    { source = "my_format_preset" },
]
```

A preset can itself reference other presets in its `preset.toml` if needed.

## Publishing

Presets are regular Python packages — publish them to PyPI with any standard build tool:

```bash
python -m build
python -m twine upload dist/*
```
