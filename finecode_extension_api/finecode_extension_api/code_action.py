from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import AsyncIterator, Generic, Protocol, TypeVar

from pydantic import BaseModel


class ActionHandlerConfig(BaseModel): ...


class RunActionPayload(BaseModel): ...


class RunActionWithPartialResult(RunActionPayload):
    # `RunActionWithPartialResult` should be interface but to avoid multiple inheritance
    # and problems with pydantic, make it subclass of `RunActionPayload`
    partial_result_token: int | str | None = None


class RunActionResult(BaseModel):
    def update(self, other: RunActionResult) -> None:
        raise NotImplementedError()


RunPayloadType = TypeVar("RunPayloadType", bound=RunActionPayload) #  | AsyncIterator[RunActionPayload]
RunIterablePayloadType = TypeVar("RunIterablePayloadType", bound=AsyncIterator[RunPayloadType])
RunResultType = TypeVar("RunResultType", bound=RunActionResult) #  | AsyncIterator[RunActionResult]
RunIterableResultType = TypeVar("RunResultType", bound=AsyncIterator[RunResultType])


class RunActionContext:
    # data object to save data between action steps(only during one run, after run data
    # is removed). Keep it simple, without business logic, just data storage, but you
    # still may initialize values in constructor using dependency injection if needed
    # to avoid handling in action cases when run context is not initialized and is
    # initialized already.

    async def init(self, initial_payload: RunPayloadType) -> None: ...


RunContextType = TypeVar("RunContextType", bound=RunActionContext)


class ActionContext:
    def __init__(self, project_dir: Path, cache_dir: Path) -> None:
        self.project_dir = project_dir
        # runner-specific cache dir
        self.cache_dir = cache_dir


class Action(Generic[RunPayloadType, RunContextType, RunResultType]):
    ...


InitializeCallable = Callable[[], None]
ShutdownCallable = Callable[[], None]
ExitCallable = Callable[[], None]


class ActionHandlerLifecycle:
    def __init__(self) -> None:
        self.on_initialize_callable: InitializeCallable | None = None
        self.on_shutdown_callable: ShutdownCallable | None = None
        self.on_exit_callable: ExitCallable | None = None

    def on_initialize(self, callable: InitializeCallable) -> None:
        self.on_initialize_callable = callable

    def on_shutdown(self, callable: ShutdownCallable) -> None:
        self.on_shutdown_callable = callable

    def on_exit(self, callable: ExitCallable) -> None:
        self.on_exit_callable = callable


ActionHandlerConfigType = TypeVar("ActionHandlerConfigType", bound=ActionHandlerConfig, covariant=True)
ActionType = TypeVar("ActionType", bound=Action[RunPayloadType | RunIterablePayloadType, RunContextType, RunResultType | RunIterableResultType], covariant=True)


class ActionHandler(
    Protocol[ActionType, ActionHandlerConfigType]
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

    async def run(
        self, payload: RunPayloadType | RunIterablePayloadType, run_context: RunContextType
    ) -> RunResultType | RunIterableResultType:
        raise NotImplementedError()

    async def stop(self):
        raise NotImplementedError()
