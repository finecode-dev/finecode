from __future__ import annotations

import enum
import typing
from pathlib import Path

from finecode.extension_runner.impls import process_executor as process_executor_impl
from finecode_extension_api import code_action


class Action:
    def __init__(
        self,
        name: str,
        config: dict[str, typing.Any],
        handlers: list[ActionHandler],
        source: str,
    ) -> None:
        self.name: str = name
        self.config: dict[str, typing.Any] = config
        self.handlers: list[ActionHandler] = handlers
        self.source: str = source


class ActionHandler:
    def __init__(self, name: str, source: str, config: dict[str, typing.Any]) -> None:
        self.name = name
        self.source = source
        self.config = config


class Project:
    def __init__(
        self,
        name: str,
        path: Path,
        actions: dict[str, Action],
    ) -> None:
        self.name = name
        self.path = path
        self.actions = actions

    def __str__(self) -> str:
        return f'Project(name="{self.name}", path="{self.path}")'


class ActionExecInfo:
    def __init__(
        self,
        payload_type: typing.Type[code_action.RunActionPayload] | None,
        run_context_type: typing.Type[code_action.RunActionContext] | None,
    ) -> None:
        self.payload_type: typing.Type[code_action.RunActionPayload] | None = (
            payload_type
        )
        self.run_context_type: typing.Type[code_action.RunActionContext] | None = (
            run_context_type
        )
        # instantiation of process executor impl is cheap. To avoid analyzing all
        # action handlers and checking whether they need process executor, just
        # instantiate here. It will be started only if handlers need it.
        self.process_executor = process_executor_impl.ProcessExecutor()


class ActionHandlerExecInfo:
    def __init__(self) -> None:
        self.lifecycle: code_action.ActionHandlerLifecycle | None = None
        self.status: ActionHandlerExecInfoStatus = ActionHandlerExecInfoStatus.CREATED


class ActionHandlerExecInfoStatus(enum.Enum):
    CREATED = enum.auto()
    INITIALIZED = enum.auto()
    SHUTDOWN = enum.auto()


class TextDocumentInfo:
    def __init__(self, uri: str, version: str, text: str) -> None:
        self.uri = uri
        self.version = version
        self.text = text

    def __str__(self) -> str:
        return (
            f'TextDocumentInfo(uri="{self.uri}", version="{self.version}",'
            f' text="{self.text}")'
        )


class TextDocumentNotOpened(Exception): ...


class PartialResult(typing.NamedTuple):
    token: int | str
    value: typing.Any
