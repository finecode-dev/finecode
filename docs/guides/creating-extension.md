# Creating an Extension

An extension is a Python package that implements one or more **ActionHandlers**. Each handler provides the logic for executing a specific action (e.g. running a linter, formatter, or build tool).

## 1. Create the package

```
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

## Sequential handlers: using `current_result`

If your handler runs in sequential mode and depends on the result of a previous handler, read it from the context:

```python
async def run(self, payload, context):
    previous: MyActionResult = context.current_result
    # extend or modify the previous result
    ...
```

!!! warning
    `context.current_result` raises `RuntimeError` in concurrent handler mode. Only use it when `run_handlers_concurrently` is `false` (the default).

## Available actions to handle

See the [Built-in Actions reference](../reference/actions.md) for the full list of action classes, payload types, and result types you can implement handlers for.

If you are defining a new action (not just a handler), see [Designing Actions](designing-actions.md) for principles on inter-language design, language-specific subactions, and where to place parameters.
