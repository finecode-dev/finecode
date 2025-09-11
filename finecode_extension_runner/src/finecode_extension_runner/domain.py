from __future__ import annotations

import enum
import dataclasses
import typing
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_runner.impls import process_executor as process_executor_impl


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
        dir_path: Path,
        def_path: Path,
        actions: dict[str, Action],
        action_handler_configs: dict[str, dict[str, typing.Any]],
    ) -> None:
        self.name = name
        self.dir_path = dir_path
        self.def_path = def_path
        self.actions = actions
        self.action_handler_configs = action_handler_configs

    def __str__(self) -> str:
        return f'Project(name="{self.name}", dir_path="{self.dir_path}")'


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


@dataclasses.dataclass
class ActionCache:
    exec_info: ActionExecInfo | None = None
    handler_cache_by_name: dict[str, ActionHandlerCache] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ActionHandlerCache:
    instance: code_action.ActionHandler | None = None
    exec_info: ActionHandlerExecInfo | None = None


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
