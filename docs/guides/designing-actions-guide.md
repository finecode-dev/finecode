# Designing Your First Action

Use this guide when you are creating a FineCode action for the first time. It
keeps to the common path: one action, one payload, one result, one run context,
and one handler.

If your action needs language-specific routing, collection processing,
delegation, partial results, progress, or workspace-wide orchestration, finish
the simple version first and then use the
[Designing Actions Reference](designing-actions-reference.md) for the matching
pattern.

Before merging, check the [Designing Actions Rules](designing-actions-rules.md).
The rules page is the normative checklist; this page is the practical walk-through.

## 1. Name the action

Choose a caller-facing verb + noun name. Action names become CLI and MCP tool
names, so they should be understandable without module context.

```text
list_tests
format_file
build_artifact
discover_wal_sources
```

Avoid names that are only clear inside one package, such as `list_services`.
Prefer the full domain name, such as `list_observability_services`.

Create one action module:

```text
fine_my_feature/list_widgets_action.py
```

Use the `{verb}_{noun}_action.py` suffix. Keep one action class, payload, result,
and run context in the module.

## 2. Define the payload

The payload contains caller-provided data. Keep it plain, serializable, and
focused on the action contract.

```python
import dataclasses
from finecode_extension_api import code_action


@dataclasses.dataclass
class ListWidgetsRunPayload(code_action.RunActionPayload):
    include_private: bool = False
    """Whether to include private widgets in the result."""
```

Write field docstrings for values whose meaning is not obvious. These
docstrings are used in generated tool schemas; inline comments are not.

For a first action, avoid optional fields with special semantics unless you
really need them. If `None`, `[]`, or an empty string has a special meaning,
document that meaning in the field docstring.

## 3. Define the result

The result contains structured data the caller can consume.

```python
@dataclasses.dataclass
class WidgetInfo:
    name: str
    source: str


@dataclasses.dataclass
class ListWidgetsRunResult(code_action.RunActionResult):
    widgets: list[WidgetInfo] = dataclasses.field(default_factory=list)
```

If multiple handlers may contribute to the same result later, add an `update()`
method that merges another result of the same type.

```python
def update(self, other: code_action.RunActionResult) -> None:
    if not isinstance(other, ListWidgetsRunResult):
        return
    self.widgets.extend(other.widgets)
```

For the first version, one handler and a simple result is usually enough.

## 4. Define the run context

The run context is the per-run object handlers receive. If you do not need
shared state, use a thin context class.

```python
class ListWidgetsRunContext(
    code_action.RunActionContext[ListWidgetsRunPayload]
):
    pass
```

If multiple handlers need to share plain mutable state, use a `STATE_TYPE`
dataclass. For details, see
[Run context state](designing-actions-reference.md#run-context-state).

## 5. Define the action class

The action class ties the payload, context, and result together.

```python
class ListWidgetsAction(
    code_action.Action[
        ListWidgetsRunPayload,
        ListWidgetsRunContext,
        ListWidgetsRunResult,
    ]
):
    """List widgets available to the current project."""

    DESCRIPTION = "List available widgets."
    PAYLOAD_TYPE = ListWidgetsRunPayload
    RUN_CONTEXT_TYPE = ListWidgetsRunContext
    RESULT_TYPE = ListWidgetsRunResult
```

`DESCRIPTION` is caller-facing and should be one sentence. The class docstring
is for developers and should describe contract details only when the behavior is
not obvious.

For the first action, use the default project scope and default sequential
handler execution. Reach for workspace scope or concurrent handlers only when
the reference guide says the action shape needs them.

## 6. Implement one handler

Handlers do the work for an action. Put the handler in a separate module:

```text
fine_my_feature/list_widgets_handler.py
```

Name the handler with a qualifier that distinguishes it from the action. Use a
tool name for tool-specific handlers, or a step name for generic handlers.

```python
import dataclasses
from finecode_extension_api import code_action

from .list_widgets_action import (
    ListWidgetsAction,
    ListWidgetsRunPayload,
    ListWidgetsRunContext,
    ListWidgetsRunResult,
    WidgetInfo,
)


@dataclasses.dataclass
class LocalListWidgetsHandlerConfig(code_action.ActionHandlerConfig):
    source_name: str = "local"


class LocalListWidgetsHandler(
    code_action.ActionHandler[
        ListWidgetsAction,
        LocalListWidgetsHandlerConfig,
    ]
):
    async def run(
        self,
        payload: ListWidgetsRunPayload,
        run_context: ListWidgetsRunContext,
    ) -> ListWidgetsRunResult:
        widgets = [
            WidgetInfo(name="default", source=self.config.source_name),
        ]
        if payload.include_private:
            widgets.append(WidgetInfo(name="private", source=self.config.source_name))
        return ListWidgetsRunResult(widgets=widgets)
```

The exact services available to your handler depend on your extension. Keep the
handler focused: read the payload and context, use injected services as needed,
and return the action result type.

## 7. Export and register it

Export the action and handler from the package entry points used by your
extension or preset.

Then register the handler in the preset or project configuration:

```toml
[[tool.finecode.action.list_widgets.handlers]]
name = "local"
source = "fine_my_feature.LocalListWidgetsHandler"
env = "dev_workspace"
```

The action contract should live in the feature preset that owns the action. A
tool extension usually contributes handlers for an action rather than redefining
the action contract.

## 8. Run it locally

Run the action from the CLI or through the client path your feature supports.
For a new action, verify at least these cases:

- The action can be discovered.
- The handler runs in the configured environment.
- The result shape matches the action result type.
- Payload field descriptions appear in generated schemas when exposed to MCP.

## 9. Add an automated handler test

FineCode provides a handler testing harness for fast, integration-style tests.
Use it to run the real Extension Runner orchestration in-process, without
starting the Workspace Manager or spawning an extension runner subprocess.

At minimum, write an automated test for the handler behavior with a
representative payload and assert the result object. If the action contract has
skip conditions, defaults, or merge behavior, test those explicitly.

Prefer a small handler test first. Add broader lifecycle or end-to-end coverage
when the action crosses process boundaries, calls other actions, or affects user
files.

See [Testing Action Handlers](testing-handlers.md) for the harness setup and
examples.

## When the simple path is not enough

Use the reference guide for these cases:

- Need language-specific behavior: [Language-specific subactions](designing-actions-reference.md#language-specific-subactions)
- Need to process one item vs many items: [Item actions and collection actions](designing-actions-reference.md#item-actions-and-collection-actions)
- Need multiple handlers: [Handler execution strategy](designing-actions-reference.md#handler-execution-strategy)
- Need shared context state: [Run context state](designing-actions-reference.md#run-context-state)
- Need to call another action: [Bridge handlers](designing-actions-reference.md#bridge-handlers)
- Need streaming or user-visible progress: [Partial results and progress](designing-actions-reference.md#partial-results-and-progress)
- Need workspace-wide behavior: [Choosing between project scope and workspace scope](designing-actions-reference.md#choosing-between-project-scope-and-workspace-scope)

Before merging, read the [Designing Actions Rules](designing-actions-rules.md)
and check that the action follows the relevant rules.
