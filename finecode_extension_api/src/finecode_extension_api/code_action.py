from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import enum
import typing
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from finecode_extension_api import partialresultscheduler, textstyler


@dataclasses.dataclass
class ActionHandlerConfig: ...


@dataclasses.dataclass
class RunActionPayload: ...


class RunReturnCode(enum.IntEnum):
    SUCCESS = 0
    ERROR = 1


@dataclasses.dataclass
class RunActionResult:
    def update(self, other: RunActionResult) -> None:
        raise NotImplementedError()

    def to_text(self) -> str | textstyler.StyledText:
        return str(self)

    @property
    def return_code(self) -> RunReturnCode:
        return RunReturnCode.SUCCESS


RunPayloadType = TypeVar(
    "RunPayloadType", bound=RunActionPayload
)  # | AsyncIterator[RunActionPayload]
RunIterablePayloadType = TypeVar(
    "RunIterablePayloadType", bound=collections.abc.AsyncIterator[RunPayloadType]
)
RunResultType = TypeVar(
    "RunResultType", bound=RunActionResult
)  # | AsyncIterator[RunActionResult]
RunIterableResultType = TypeVar(
    "RunResultType", bound=collections.abc.AsyncIterator[RunResultType]
)


class RunActionContext:
    # data object to save data between action steps(only during one run, after run data
    # is removed). Keep it simple, without business logic, just data storage, but you
    # still may initialize values in constructor using dependency injection if needed
    # to avoid handling in action cases when run context is not initialized and is
    # initialized already.

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id

    async def init(self, initial_payload: RunPayloadType) -> None: ...


RunContextType = TypeVar("RunContextType", bound=RunActionContext)


class RunActionWithPartialResultsContext(RunActionContext):
    def __init__(self, run_id: int) -> None:
        super().__init__(run_id=run_id)
        self.partial_result_scheduler = partialresultscheduler.PartialResultScheduler()


class ActionContext:
    def __init__(self, project_dir: Path, cache_dir: Path) -> None:
        self.project_dir = project_dir
        # runner-specific cache dir
        self.cache_dir = cache_dir


@dataclasses.dataclass
class ActionConfig:
    run_handlers_concurrently: bool = False


class Action(Generic[RunPayloadType, RunContextType, RunResultType]):
    PAYLOAD_TYPE: typing.Type[RunActionPayload] = RunActionPayload
    RUN_CONTEXT_TYPE: typing.Type[RunActionContext] = RunActionContext
    RESULT_TYPE: typing.Type[RunActionResult] = RunActionResult
    CONFIG_TYPE: typing.Type[ActionConfig] = ActionConfig


class StopActionRunWithResult(Exception):
    def __init__(self, result: RunActionResult) -> None:
        self.result = result


class ActionFailedException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


InitializeCallable = collections.abc.Callable[[], None]
ShutdownCallable = collections.abc.Callable[[], None]
ExitCallable = collections.abc.Callable[[], None]


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


ActionHandlerConfigType = TypeVar(
    "ActionHandlerConfigType", bound=ActionHandlerConfig, covariant=True
)
ActionType = TypeVar(
    "ActionType",
    bound=Action[
        RunPayloadType | RunIterablePayloadType,
        RunContextType,
        RunResultType | RunIterableResultType,
    ],
    covariant=True,
)


IterableType = TypeVar("IterableType")


class ActionHandler(Protocol[ActionType, ActionHandlerConfigType]):
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
        self, payload: RunPayloadType, run_context: RunContextType
    ) -> (
        RunResultType
        | collections.abc.Mapping[IterableType, asyncio.Task[RunResultType]]
    ):
        raise NotImplementedError()

    async def stop(self):
        raise NotImplementedError()
