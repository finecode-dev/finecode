from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel


class CodeActionConfig(BaseModel): ...


class RunActionPayload(BaseModel): ...


class RunActionResult(BaseModel):
    def update(self, other: RunActionResult) -> None:
        raise NotImplementedError()


RunPayloadType = TypeVar("RunPayloadType", bound=RunActionPayload)


class RunActionContext:
    # data object to save data between action steps(only during one run, after run data
    # is removed). Keep it simple, without business logic, just data storage, but you
    # still may initialize values in constructor using dependency injection if needed
    # to avoid handling in action cases when run context is not initialized and is
    # initialized already.

    async def init(self, initial_payload: RunPayloadType) -> None: ...


CodeActionConfigType = TypeVar("CodeActionConfigType")

RunResultType = TypeVar("RunResultType", bound=RunActionResult)
RunContextType = TypeVar("RunContextType", bound=RunActionContext)


@dataclass
class ActionContext:
    project_dir: Path
    # runner-specific cache dir
    cache_dir: Path


InitializeCallable = Callable[[], None]
ShutdownCallable = Callable[[], None]


class ActionHandlerLifecycle:
    def __init__(self):
        self.on_initialize_callable: InitializeCallable | None = None
        self.on_shutdown_callable: InitializeCallable | None = None

    def on_initialize(self, callable: InitializeCallable) -> None:
        self.on_initialize_callable = callable

    def on_shutdown(self, callable: ShutdownCallable) -> None:
        self.on_shutdown_callable = callable


class CodeAction(
    Generic[CodeActionConfigType, RunPayloadType, RunContextType, RunResultType]
):
    """
    **Action config**
    Configuration can be set in following places by priority:
    - project definition, e.g. pyproject.toml
    - workspace definition (if action is enabled in workspace definition)
    - preset or composable action, it depends where action comes from

    In action implementation there is no action config as such, because config
    definition includes default values.
    """

    LANGUAGE: str = "python"
    IS_BACKGROUND: bool = False

    def __init__(self, config: CodeActionConfigType, context: ActionContext) -> None:
        self.config = config
        self.context = context

    async def run(
        self, payload: RunPayloadType, run_context: RunContextType
    ) -> RunResultType:
        raise NotImplementedError()

    async def stop(self):
        raise NotImplementedError()
