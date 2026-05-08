# Creating an Extension

An extension is a Python package that implements one or more **ActionHandlers**. Each handler provides the logic for executing a specific action (e.g. running a linter, formatter, or build tool).

## 1. Create the package

```text
my_linter/
    pyproject.toml
    my_linter/
        __init__.py
        handler.py
```

**`pyproject.toml`** — declare `finecode_extension_api` as a dependency:

```toml
[project]
name = "my_linter"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["finecode_extension_api~=0.4.0"]

[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"
```

## 2. Implement a handler

Import the action you want to handle and subclass `ActionHandler`:

```python
# my_linter/handler.py
from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.lint_files_action import (
    LintFilesAction,
    LintFilesRunPayload,
    LintFilesRunContext,
    LintFilesRunResult,
    LintMessage,
)


class MyLinterHandler(
    code_action.ActionHandler[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    action = LintFilesAction

    async def run(
        self, payload: LintFilesRunPayload, context: LintFilesRunContext
    ) -> LintFilesRunResult:
        diagnostics: list[LintMessage] = []

        for file_path in payload.file_paths:
            # run your tool and collect results
            messages = run_my_tool(file_path)
            diagnostics.extend(messages)

        return LintFilesRunResult(diagnostics=diagnostics)
```

## 3. Export from `__init__.py`

```python
# my_linter/__init__.py
from my_linter.handler import MyLinterHandler

__all__ = ["MyLinterHandler"]
```

## 4. Register the handler in a project

Add the handler to the target action in `pyproject.toml`:

```toml
[tool.finecode.action.lint]
source = "finecode_extension_api.actions.LintAction"
handlers = [
    {
        name = "my_linter",
        source = "my_linter.MyLinterHandler",
        env = "dev_no_runtime",
        dependencies = ["my_linter~=0.1.0"]
    }
]
```

Then run `python -m finecode prepare-envs` to install your handler into the venv.

## Source strings

Every `source =` value in TOML config is resolved at runtime as import path.

**Built-in action classes** are all re-exported from `finecode_extension_api.actions`, so
their source strings take the short form `finecode_extension_api.actions.<ClassName>` — you
do not need to know which subgroup (`code_quality/`, `environments/`, etc.) a class lives in.
See the [Built-in Actions reference](../reference/actions.md) for the full list.

**Handler classes** are referenced by their full import path:
`my_linter.MyLinterHandler` (module `my_linter`, member `MyLinterHandler`).

## Handler configuration

To make your handler configurable, define a config model and declare `CONFIG_TYPE`:

```python
import dataclasses
from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.lint_files_action import (
    LintFilesAction, LintFilesRunPayload, LintFilesRunContext, LintFilesRunResult,
)


@dataclasses.dataclass
class MyLinterConfig:
    line_length: int = 88
    extend_ignore: list[str] = dataclasses.field(default_factory=list)


class MyLinterHandler(
    code_action.ActionHandler[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    action = LintFilesAction
    CONFIG_TYPE = MyLinterConfig

    async def run(
        self, payload: LintFilesRunPayload, context: LintFilesRunContext
    ) -> LintFilesRunResult:
        config: MyLinterConfig = context.handler_config
        # use config.line_length, config.extend_ignore, ...
        ...
```

Users can then configure it in `pyproject.toml`:

```toml
[[tool.finecode.action_handler]]
source = "my_linter.MyLinterHandler"
config.line_length = 100
config.extend_ignore = ["E501"]
```

Or via CLI/env vars at runtime (see [Configuration](../configuration.md)).

## Handler lifecycle

For handlers that need to start a background process (e.g. a language server), use the lifecycle hooks:

```python
class MyLspHandler(code_action.ActionHandler[...]):
    action = LintFilesAction

    async def run(self, payload, context):
        ...

    def on_start(self) -> None:
        # called once when the handler is first loaded
        self._process = start_my_server()

    def on_shutdown(self) -> None:
        # called when the Extension Runner shuts down
        self._process.terminate()
```

## Logging in handlers

Handlers receive an `ILogger` instance via dependency injection by declaring `logger: ilogger.ILogger` in their constructor:

```python
from finecode_extension_api.interfaces import ilogger

class MyToolHandler(code_action.ActionHandler[...]):
    def __init__(self, ..., logger: ilogger.ILogger) -> None:
        self.logger = logger
```

### What the framework already logs

The framework logs the full action payload at `TRACE` level before calling your handler. Enable it with `"finecode_extension_runner" = "TRACE"` in your ER log groups config. Do not repeat this in the handler — it creates noise without adding value (R-504).

### What handlers should log

For handlers that spawn an external process, log the following at `DEBUG`:

- exit code — always
- stdout and stderr — when non-empty
- result summary — key metrics from the tool's report (counts, durations)

Emit a `WARNING` when the handler's primary job is to produce results but returns nothing:

```python
process = await self.command_runner.run(cmd, cwd=project_dir)
await process.wait_for_end()

exit_code = process.get_exit_code()
stdout = process.get_output()
stderr = process.get_error_output()

self.logger.debug(f"mytool exit code: {exit_code}")
if stdout:
    self.logger.debug(f"mytool stdout:\n{stdout}")
if stderr:
    self.logger.debug(f"mytool stderr:\n{stderr}")

# ... parse the tool's report into results ...

if not results:
    self.logger.warning(f"No results produced. cmd={cmd!r} exit_code={exit_code}")
```

### Exit code handling

Do not rely on the absence of an output file as the sole error indicator. Check exit codes explicitly and document their meaning for your specific tool (R-501):

```python
# exit codes 0 (ok) and 1 (warnings) are normal for mytool; 2 = usage error
if exit_code not in (0, 1):
    descriptions: dict[int | None, str] = {
        2: "command-line usage error — check your config",
    }
    reason = descriptions.get(exit_code, f"unexpected exit code {exit_code}")
    raise code_action.ActionFailedException(
        f"mytool exited with code {exit_code}: {reason}.\nOutput:\n{stderr or stdout}"
    )
```

See [Designing Actions Rules](designing-actions-rules.md) R-501 through R-504 for the normative statements.

## Sequential handlers: using `current_result`

If your handler runs in sequential mode and depends on the result of a previous handler, read it from the context:

```python
async def run(self, payload, context):
    previous: MyActionResult = context.current_result
    # extend or modify the previous result
    ...
```

!!! warning
    `context.current_result` raises `RuntimeError` in concurrent handler mode. Only use it when the action's `HANDLER_EXECUTION` is `HandlerExecution.SEQUENTIAL` (the default).

## Available actions to handle

See the [Built-in Actions reference](../reference/actions.md) for the full list of action classes, payload types, and result types you can implement handlers for.

If you are defining a new action (not just a handler), start with [Designing Actions](designing-actions.md), then use [Designing Your First Action](designing-actions-guide.md) for the workflow and the [Designing Actions Rules](designing-actions-rules.md) for the normative constraints. Use the [Designing Actions Reference](designing-actions-reference.md) when the action needs advanced patterns.

## Tool versioning

An extension wraps a specific tool and has its own version lifecycle separate from the tool's. Extension authors must declare which tool versions the handler supports.

### Compatibility range

Declare the tool as a versioned dependency in the extension's `pyproject.toml`:

```toml
dependencies = ["ruff (>=0.8.0,<1.0.0)"]
```

This range is a **compatibility guarantee**: every version within it exposes an interface that the handler code can correctly drive. "Interface" means whatever channel the handler uses to communicate with the tool — for example:

- **CLI**: the command flags, invocation format, output format, and exit codes the handler relies on
- **LSP**: the capabilities, request/response shapes, and initialization options of the tool's language server
- **HTTP/JSON API**: the request and response schemas of any programmatic API the tool exposes
- **Python API**: the public classes and functions the handler imports directly from the tool's package

An interface change that breaks the handler — a renamed flag, a changed JSON schema, a removed LSP capability — requires updating the range.

### Extension author responsibilities

- Declare the **widest range you can actually support**. Overly narrow ranges force unnecessary upgrades on users.
- When a new tool version introduces interface changes that require handler updates: update the handler code and widen or shift the range accordingly.
- When a new tool version adds features or changes behavior but leaves the interface the handler uses unchanged: the existing range already covers it — no update needed.
- Annotate significant range changes in the changelog so users can see which tool versions a given extension version supports.

### User's responsibility

Within the extension's declared range, users choose the exact tool version for their project. This is a behavioral decision: a user may prefer a specific version because it produces the results they expect, avoids a known regression in the tool, or matches the version their team has standardized on.

#### Pinning at the extension level (recommended)

In almost all cases, the user wants the same tool version everywhere a given extension is used. Configure that once at the extension level, keyed by the extension's package name:

```toml
[tool.finecode.extension.fine_python_ruff]
dependencies_override = ["ruff==0.9.0", "ruff-plugin-foo==1.2.3"]
```

The override applies to every env that contains a handler from `fine_python_ruff`.

#### Pinning at the handler level

When you genuinely need different versions for different handlers from the same extension (rare), override per handler:

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
dependencies_override = ["ruff==0.9.0"]
```

Handler-level overrides take precedence over extension-level overrides for the targeted handler.

#### How overrides are resolved

FineCode resolves each env's full dependency tree first, then applies overrides by package name in this order (later wins):

1. The extension's declared dependency range (from the extension's `pyproject.toml`)
2. Extension-level `dependencies_override`
3. Handler-level `dependencies_override`

For each package the user targets:

- If the package is **already in the resolved tree** — the user's specifier replaces whatever the extension declared. This lets you pin to a version outside the extension's stated compatibility range if you need to.
- If the package is **not in the resolved tree** — it is added as a new dependency. This covers tool extensions or plugins that the extension itself does not declare.

When you override a package to a version outside the extension's compatibility range, the extension author's range becomes documentation only — you take responsibility for verifying that the handler code works correctly with the chosen version.

#### When two extensions wrap the same tool

Two extensions may legitimately depend on the same tool (e.g. a `lint` extension and a `format` extension both wrapping ruff). Each env resolves a single version of the tool that satisfies the union of every extension's declared range, so when both extensions are used together in the same env you get one consistent tool version automatically.

If you want to pin that shared tool, place the override on **one** of the extensions — typically the one that drives the version choice. If both extensions declare a `dependencies_override` for the same package in the same env with conflicting specifiers, the package manager will fail to install: resolve the conflict by removing one of the overrides or by using a handler-level override only on the handler you want to differ.

If you genuinely need different versions of the same tool for different extensions, place each extension's handlers in a different env.

## Package naming

Extension package names follow the pattern `fine_<lang>_<qualifier>`.
Use a tool name or capability descriptor as the qualifier, and keep bare
`fine_<word>` names reserved for presets.

See [Package Naming](package-naming.md) for the shared extension and preset naming convention.
