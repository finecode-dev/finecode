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

## What goes inside a preset package

A preset package contains:

- A `preset.toml` declaring action and handler registrations.
- For **cross-language feature presets** (e.g. `fine_format`, `fine_lint`): the definitions of the inter-language action contracts they register. The action class and its canonical registration ship and version together. See [ADR-0036](../adr/0036-feature-presets-own-their-action-contracts.md).
- **Orchestration handlers** — handler implementations that coordinate or dispatch work to language-specific handlers (e.g. `FormatHandler`, `LintFilesDispatchHandler`). These handlers naturally belong in the preset that defines the action they implement. See [Handlers in presets](#handlers-in-presets) below.
- Optionally, an `__init__.py` re-exporting action classes and handler classes for convenient imports.

Preset packages must stay **lightweight in their runtime dependencies**: only `finecode_extension_api` (for base classes) and other feature presets (when an action contract references types defined elsewhere) are allowed. Heavy tool dependencies belong to extension packages and are pulled in through dependency groups when handlers are activated.

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
    { name = "mypy", source = "fine_python_mypy.MypyLintFilesHandler", env = "dev_no_runtime", dependencies = [
        "fine_python_mypy~=0.3.0",
    ] },
]

[tool.finecode.action.format_python_file]
source = "finecode_extension_api.actions.FormatPythonFileAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffFormatFileHandler", env = "dev_no_runtime", dependencies = [
        "fine_python_ruff~=0.2.0",
    ] },
    { name = "save", source = "fine_format.SaveFormatFileHandler", env = "dev_no_runtime", dependencies = [
        "fine_format~=0.1.0",
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

## 4. Pin tool versions

Universal presets (presets intended for broad adoption across many projects) are responsible for declaring an explicit tool version pin — not just a range. A pin communicates "this version has been validated" and gives all consuming projects a consistent, deterministic tool version automatically.

Declare the pin in `preset.toml` using the extension-level override:

```toml
# my_preset/my_preset/preset.toml

[tool.finecode.extension.fine_python_ruff]
dependencies_override = ["ruff==0.9.0"]
```

**Preset author responsibilities:**

- Pin to the **latest version you have tested** against your preset's handler configurations. Do not pin speculatively.
- When a new tool version requires handler changes, update the extension package version range, update the extension handlers if needed, and bump the pin here in the same preset release.
- When a new tool version is backwards-compatible (no handler changes needed), you may bump the pin in a patch release of the preset.
- Document the tool version history in your changelog so consumers can see which tool versions a given preset version supports.

**Preset author — release cycle note:** By pinning a tool version, you couple the preset's release cadence to the tool's release cadence. This is intentional for a universal preset: upgrades become deliberate decisions rather than silent resolver choices.

Project-specific presets (e.g. a company-internal preset) may override a universal preset's pin by re-declaring the same extension entry. Users can do the same in their project `pyproject.toml`. Later configuration layers always win — see [Creating an Extension — How overrides are resolved](creating-extension.md#how-overrides-are-resolved).

## 5. Handlers in presets

A preset can include **handler implementations** alongside its action definitions. This is the right home for handlers that orchestrate or dispatch work rather than wrapping a specific tool.

### Which handlers belong in a preset vs. an extension

| Belongs in **preset** | Belongs in **extension** |
|---|---|
| Orchestrates calls to language-specific subactions (e.g. `FormatHandler` fans out `FormatFilesAction` per language) | Wraps a specific external tool (e.g. `RuffLintFilesHandler` calls ruff) |
| Dispatches to language-specific handlers (e.g. `LintFilesDispatchHandler`) | Needs heavy third-party dependencies (ruff, mypy, black, etc.) |
| Only depends on `finecode_extension_api` + other presets | Needs tool-specific libraries at import time |

### Dependency constraint

Handlers living in a preset are subject to the same dependency rule as the rest of the preset: **only `finecode_extension_api` and other feature presets are allowed as runtime dependencies**. Handler code is imported by the Extension Runner in the handler's declared `env`, but the preset package is also installed in `dev_workspace` for config resolution. If a handler module imported a heavy library at the top level, that library would need to be present in `dev_workspace` too — which is exactly what presets are designed to avoid.

If a handler needs heavy dependencies, it belongs in a separate **extension package**.

### Referencing handlers from the same preset

When a handler lives inside the preset, the `preset.toml` references it by the preset's own import path. The handler still declares a `dependencies` entry pointing to its own preset package so that `prepare-envs` installs it in the handler's `env`:

```toml
# fine_format/fine_format/preset.toml

[tool.finecode.action.format]
source = "fine_format.FormatAction"
handlers = [
    { name = "format", source = "fine_format.FormatHandler", env = "dev_no_runtime", dependencies = [
        "fine_format~=0.1.0",
    ] },
]
```

### Package layout with handlers

```
fine_format/
    pyproject.toml
    fine_format/
        __init__.py
        preset.toml
        format_action.py          # action definition
        format_handler.py         # orchestration handler
```

## 6. Allow users to override your defaults

Users can add `[[tool.finecode.action_handler]]` entries in their own `pyproject.toml` to override any config you set in `preset.toml`. Your preset's values are the baseline; user config always wins.

Users can also:

- Add more handlers to actions you declared
- Replace all handlers with `handlers_mode = "replace"`
- Disable specific handlers with `disabled = true`

## 7. Composing multiple presets

A project can activate multiple presets. They are applied in order, and later preset handlers are added after earlier ones:

```toml
[tool.finecode]
presets = [
    { source = "my_lint_preset" },
    { source = "my_format_preset" },
]
```

A preset can itself reference other presets in its `preset.toml` if needed.

## Package naming

Preset package names follow the pattern `fine_<lang?>_<role>`, where `<role>` is drawn from the closed set of FineCode role words and the language segment is optional.

See [Package Naming](package-naming.md) for the shared extension and preset naming convention.

## Publishing

Presets are regular Python packages — publish them to PyPI with any standard build tool:

```bash
python -m build
python -m twine upload dist/*
```
