# Extensions

Extensions provide concrete implementations of actions as handlers. All extensions listed here are maintained in the FineCode repository under `extensions/`.

To use an extension, add its handler to an action in your `pyproject.toml` or use a [preset](../guides/creating-preset.md) that includes it. The extension package is installed automatically into the handler's virtual environment by `prepare-envs`.

---

## Linting

### `fine_python_ruff`

Linting and formatting via [Ruff](https://docs.astral.sh/ruff/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_ruff.RuffLintFilesHandler` | `lint_files` | Lint Python files with Ruff |
| `fine_python_ruff.RuffFormatFilesHandler` | `format_files` | Format Python files with Ruff formatter |

**Example config:**

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["B", "I"]
config.line_length = 88
config.target_version = "py311"
```

---

### `fine_python_flake8`

Linting via [Flake8](https://flake8.pycqa.org/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_flake8.Flake8LintFilesHandler` | `lint_files` | Lint Python files with Flake8 |

**Example config:**

```toml
[[tool.finecode.action_handler]]
source = "fine_python_flake8.Flake8LintFilesHandler"
config.max_line_length = 88
config.extend_ignore = ["E203", "E501"]
config.select = []   # disable all standard rules (use only custom rules)
```

---

### `fine_python_mypy`

Type checking via [Mypy](https://mypy-lang.org/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_mypy.MypyLintHandler` | `lint` / `lint_files` | Type-check Python files |

---

### `fine_python_import_linter`

Architecture validation via [import-linter](https://import-linter.readthedocs.io/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_import_linter.ImportLinterHandler` | `lint` | Validate import contracts |

Reads contract definitions from `[tool.importlinter]` in `pyproject.toml`.

---

### `fine_python_pyrefly`

Type checking and LSP integration via [Pyrefly](https://pyrefly.org/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_pyrefly.PyreflyLintFilesHandler` | `lint_files` | Type-check with Pyrefly |

---

### `fine_python_ast`

AST-based analysis for Python source files.

| Handler | Action | Description |
|---|---|---|
| `fine_python_ast.AstHandler` | Various | AST traversal and analysis |

---

## Formatting

### `fine_python_black`

Code formatting via [Black](https://black.readthedocs.io/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_black.BlackFormatFilesHandler` | `format_files` | Format Python files with Black |

---

### `fine_python_isort`

Import sorting via [isort](https://pycqa.github.io/isort/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_isort.IsortFormatFilesHandler` | `format_files` | Sort Python imports |

**Example config** (compatible with Ruff formatter / Black):

```toml
[[tool.finecode.action_handler]]
source = "fine_python_isort.IsortFormatFilesHandler"
config.multi_line_output = 3
config.include_trailing_comma = true
config.line_length = 88
config.split_on_trailing_comma = true
```

---

## Package & environment management

### `fine_python_pip`

Environment management via pip.

| Handler | Action | Description |
|---|---|---|
| `fine_python_pip.PipInstallDepsInEnvHandler` | `install_deps_in_env` | Install dependencies with pip |

**Example config:**

```toml
[[tool.finecode.action_handler]]
source = "fine_python_pip.PipInstallDepsInEnvHandler"
config.editable_mode = "compat"   # "compat", "strict", or default
```

---

### `fine_python_virtualenv`

Virtual environment creation.

| Handler | Action | Description |
|---|---|---|
| `fine_python_virtualenv.VirtualenvPrepareEnvHandler` | `prepare_envs` | Create virtualenvs |

---

### `fine_python_setuptools_scm`

Version management via [setuptools-scm](https://setuptools-scm.readthedocs.io/).

| Handler | Action | Description |
|---|---|---|
| `fine_python_setuptools_scm.GetSrcArtifactVersionSetuptoolsScmHandler` | `get_src_artifact_version` | Read version from VCS tags |

---

## Analysis

### `fine_python_module_exports`

Analyzes and validates Python module exports.

| Handler | Action | Description |
|---|---|---|
| `fine_python_module_exports.*` | Various | Validate `__all__` and public API |

---

### `fine_python_package_info`

Reads package metadata from `pyproject.toml`.

| Handler | Action | Description |
|---|---|---|
| `fine_python_package_info.*` | Various | Provide package name, version, files |
