# Configuration

FineCode merges configuration from multiple sources in order of increasing priority:

```
preset.toml  →  pyproject.toml  →  environment variables  →  CLI flags
```

Higher-priority sources override lower-priority ones.

## pyproject.toml

All FineCode configuration lives under `[tool.finecode]`.

### Enabling presets

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" },
    { source = "my_custom_preset" },
]
```

Presets are applied in order. Later presets' handlers are added after earlier ones.

### Declaring actions and handlers

You can declare or extend actions directly in your project:

```toml
[tool.finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers = [
    { name = "ruff", source = "fine_python_ruff.RuffLintFilesHandler", env = "dev_no_runtime", dependencies = ["fine_python_ruff~=0.2.0"] },
    { name = "mypy", source = "fine_python_mypy.MypyLintHandler", env = "dev_no_runtime", dependencies = ["fine_python_mypy~=0.3.0"] },
]
```

### Replacing preset handlers

To completely replace the handlers from presets for an action:

```toml
[tool.finecode.action.lint]
source = "finecode_extension_api.actions.lint.LintAction"
handlers_mode = "replace"
handlers = [
    { name = "mypy", source = "fine_python_mypy.MypyLintHandler", env = "dev_no_runtime", dependencies = ["fine_python_mypy~=0.3.0"] },
]
```

### Disabling a specific handler

```toml
[tool.finecode.action.lint]
handlers = [
    { name = "flake8", enabled = false },
]
```

### Configuring a handler

Use `[[tool.finecode.action_handler]]` to configure a handler by its source path:

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["B", "I"]
config.line_length = 100

[[tool.finecode.action_handler]]
source = "fine_python_flake8.Flake8LintFilesHandler"
config.max_line_length = 88
config.extend_ignore = ["E203", "E501"]
```

### Declaring services

Services are shared, long-lived dependencies used by handlers. Declare service bindings with `[[tool.finecode.service]]` entries:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.ilspclient.ILspClient"
source = "finecode_extension_runner.impls.lsp_client.LspClientImpl"
env = "dev_no_runtime"
dependencies = []
```

Service declarations are merged by `interface`. If a preset declares a service, you can rebind it in your project by declaring the same `interface` with a different `source`:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.ihttpclient.IHttpClient"
source = "my_company_http.MyHttpClient"
env = "dev_no_runtime"
dependencies = ["my_company_http~=1.2.0"]
```

### Environment-specific dependencies

You can pin or override dependencies installed into each env:

```toml
[tool.finecode.env.dev_no_runtime.dependencies]
fine_python_ruff = { path = "./my_local_ruff_fork", editable = true }
```

## Environment variables

Override handler config at runtime without modifying files.

**Format:**

```
FINECODE_CONFIG_<ACTION>__<PARAM>=<json_value>
FINECODE_CONFIG_<ACTION>__<HANDLER>__<PARAM>=<json_value>
```

- `<ACTION>`, `<HANDLER>`, `<PARAM>` are **uppercase**, separated by double underscores (`__`)
- Values are parsed as **JSON** (use `"true"`, `123`, `"string"`, `["a","b"]`, etc.)

**Examples:**

```bash
# Set line_length for all handlers of the lint action
FINECODE_CONFIG_LINT__LINE_LENGTH=100 python -m finecode run lint

# Set line_length only for the ruff handler
FINECODE_CONFIG_LINT__RUFF__LINE_LENGTH=120 python -m finecode run lint

# Pass a JSON array
FINECODE_CONFIG_LINT__RUFF__EXTEND_SELECT='["B","I"]' python -m finecode run lint
```

To disable env var config entirely:

```bash
python -m finecode run --no-env-config lint
```

## CLI config flags

Override config inline on the command line. CLI flags take precedence over env vars.

**Format:**

```
--config.<param>=<value>
--config.<handler>.<param>=<value>
```

**Examples:**

```bash
# Action-level: applies to all handlers
python -m finecode run lint --config.line_length=120

# Handler-specific
python -m finecode run lint --config.ruff.line_length=120

# Combined
python -m finecode run lint --config.ruff.line_length=120 --config.mypy.strict=true

# CLI overrides env vars (line_length will be 120)
FINECODE_CONFIG_LINT__RUFF__LINE_LENGTH=100 python -m finecode run lint --config.ruff.line_length=120
```

## Inspecting resolved configuration

Dump the fully merged configuration for a project to a file:

```bash
python -m finecode dump-config --project=my_project
# Output written to finecode_config_dump/
```

This is useful for debugging config merging from multiple presets.
