# docs: docs/concepts.md, docs/guides/creating-extension.md
from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import enum
import typing
from typing import ClassVar, Generic, Protocol, TypeVar

from finecode_extension_api import partialresultscheduler, textstyler


@dataclasses.dataclass
class ActionHandlerConfig: ...


@dataclasses.dataclass
class RunActionPayload: ...


@dataclasses.dataclass
class CallerRunContextKwargs:
    """Base class for caller-provided run context parameters.

    When a parent action delegates to a child action and needs to pass runtime
    state (e.g. a shared file editor session), it defines a concrete subclass
    of ``CallerRunContextKwargs`` with typed fields for those values.  The child
    action's ``RunActionContext`` declares a constructor parameter
    ``caller_kwargs: MyCallerRunContextKwargs | None = None`` to receive them.

    This separates **caller-provided** parameters (passed explicitly through
    ``IActionRunner.run_action(caller_kwargs=...)``) from
    **DI-resolved** parameters (injected automatically by the framework).

    See :doc:`docs/guides/designing-actions` for the full pattern.
    """


class RunActionTrigger(enum.StrEnum):
    USER = "user"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class DevEnv(enum.StrEnum):
    IDE = "ide"
    CLI = "cli"
    AI = "ai"
    PRECOMMIT = "precommit"
    CI = "ci"


@dataclasses.dataclass
class RunActionMeta:
    trigger: RunActionTrigger
    dev_env: DevEnv
    wal_run_id: str = ""
    orchestration_depth: int = 0  # incremented at each cross-boundary hop


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


RunPayloadType = TypeVar("RunPayloadType", bound=RunActionPayload, covariant=True)
RunResultType = TypeVar("RunResultType", bound=RunActionResult, covariant=True)


class RunContextInfoProvider:
    """
    Owned by the action runner, passed to RunActionContext.
    """

    def __init__(self, is_concurrent_execution: bool) -> None:
        self._current_result: RunActionResult | None = None
        self.is_concurrent_execution: bool = is_concurrent_execution

    @property
    def current_result(self) -> RunActionResult | None:
        """
        Access accumulated result from previously completed handlers.
        Only available in sequential execution mode.

        NOTE: it's highly discouraged to change the object, use it as readonly object.
        """
        return self._current_result

    def update(self, result: RunActionResult) -> None:
        """Called by action runner after each handler completes."""
        if self._current_result is None:
            self._current_result = result
        else:
            self._current_result.update(result)


class PartialResultSender(typing.Protocol):
    """Handler-facing interface for sending partial results to the client."""

    async def send(self, result: RunActionResult) -> None: ...


class _NoOpPartialResultSender(PartialResultSender):
    async def send(self, result: RunActionResult) -> None:
        pass


_NOOP_SENDER = _NoOpPartialResultSender()


class ProgressSender(typing.Protocol):
    """Framework-internal interface for sending progress notifications."""

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None: ...

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None: ...

    async def end(self, message: str | None = None) -> None: ...


class _NoOpProgressSender(ProgressSender):
    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        pass

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        pass

    async def end(self, message: str | None = None) -> None:
        pass


_NOOP_PROGRESS_SENDER = _NoOpProgressSender()


class ProgressContext:
    """Async context manager for reporting progress from handlers.

    Two methods serve different use cases:
    - ``advance(steps, message)`` — the common "N of M" pattern; auto-calculates percentage.
    - ``report(message, percentage)`` — freeform; for indeterminate progress or custom logic.
    """

    def __init__(
        self,
        sender: ProgressSender,
        title: str,
        *,
        total: int | None = None,
        cancellable: bool = False,
    ) -> None:
        self._sender = sender
        self._title = title
        self._total = total
        self._cancellable = cancellable
        self._completed = 0

    async def __aenter__(self) -> ProgressContext:
        if self._total is None:
            percentage = None
        elif self._total <= 0:
            percentage = 100
        else:
            percentage = 0
        await self._sender.begin(
            self._title,
            percentage=percentage,
            cancellable=self._cancellable,
            total=self._total,
        )
        return self

    async def __aexit__(self, *exc) -> bool:
        await self._sender.end()
        return False

    async def advance(self, steps: int = 1, message: str | None = None) -> None:
        """Step-based progress. Auto-calculates percentage from total."""
        self._completed += steps
        percentage = None
        if self._total is not None:
            if self._total <= 0:
                percentage = 100
            else:
                percentage = min(int(self._completed / self._total * 100), 100)
        await self._sender.report(message=message, percentage=percentage)

    async def report(
        self, message: str | None = None, percentage: int | None = None
    ) -> None:
        """Freeform progress. Caller controls the percentage directly."""
        await self._sender.report(message=message, percentage=percentage)


class RunActionContext(typing.Generic[RunPayloadType]):
    # data object to save data between action steps(only during one run, after run data
    # is removed). Keep it simple, without business logic, just data storage, but you
    # still may initialize values in constructor using dependency injection if needed
    # to avoid handling in action cases when run context is not initialized and is
    # initialized already.

    def __init__(
        self,
        run_id: int,
        initial_payload: RunPayloadType,
        meta: RunActionMeta,
        info_provider: RunContextInfoProvider,
        partial_result_sender: PartialResultSender = _NOOP_SENDER,
        progress_sender: ProgressSender = _NOOP_PROGRESS_SENDER,
    ) -> None:
        self.run_id = run_id
        self.initial_payload = initial_payload
        self.meta = meta
        self.exit_stack = contextlib.AsyncExitStack()
        self._info_provider = info_provider
        self.partial_result_sender = partial_result_sender
        self._progress_sender = progress_sender

    def progress(
        self,
        title: str,
        *,
        total: int | None = None,
        cancellable: bool = False,
    ) -> ProgressContext:
        """Create a progress context manager for reporting progress to the client."""
        return ProgressContext(
            self._progress_sender, title, total=total, cancellable=cancellable
        )

    @property
    def current_result(self) -> RunActionResult | None:
        """
        Access accumulated result from previously completed handlers.
        Only available in sequential execution mode.

        NOTE: it's highly discouraged to change the object, use it as readonly object.
        """
        if self._info_provider.is_concurrent_execution:
            raise RuntimeError(
                "Cannot access current_result during concurrent handler execution. "
                "Results from other handlers are not reliably available in concurrent mode."
            )
        return self._info_provider.current_result

    async def init(self) -> None: ...

    async def __aenter__(self):
        await self.exit_stack.__aenter__()

        await self.init()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)


RunContextType = TypeVar(
    "RunContextType", bound=RunActionContext[RunActionPayload], covariant=True
)


class RunActionWithPartialResultsContext(RunActionContext[RunPayloadType]):
    def __init__(
        self,
        run_id: int,
        initial_payload: RunPayloadType,
        meta: RunActionMeta,
        info_provider: RunContextInfoProvider,
        partial_result_sender: PartialResultSender = _NOOP_SENDER,
        progress_sender: ProgressSender = _NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            partial_result_sender=partial_result_sender,
            progress_sender=progress_sender,
        )
        self.partial_result_scheduler = partialresultscheduler.PartialResultScheduler()


class HandlerExecution(enum.Enum):
    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"


@dataclasses.dataclass
class ActionConfig: ...


class Action(Generic[RunPayloadType, RunContextType, RunResultType]):
    PAYLOAD_TYPE: type[RunActionPayload] = RunActionPayload
    RUN_CONTEXT_TYPE: type[RunActionContext[RunPayloadType]] = RunActionContext
    RESULT_TYPE: type[RunActionResult] = RunActionResult
    CONFIG_TYPE: type[ActionConfig] = ActionConfig
    LANGUAGE: ClassVar[str | None] = None
    PARENT_ACTION: ClassVar[type[Action] | None] = None
    HANDLER_EXECUTION: ClassVar[HandlerExecution] = HandlerExecution.SEQUENTIAL


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
    bound=Action[RunActionPayload, RunActionContext[RunActionPayload], RunActionResult],
    covariant=True,
)


PayloadTypeVar = TypeVar("PayloadTypeVar", bound=RunActionPayload)


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
        self, payload: PayloadTypeVar, run_context: RunActionContext[PayloadTypeVar]
    ) -> RunActionResult:
        raise NotImplementedError()
