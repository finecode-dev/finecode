from __future__ import annotations

import typing
from enum import Enum, auto
from pathlib import Path


class Preset:
    def __init__(self, source: str) -> None:
        self.source = source


class ActionHandler:
    def __init__(self, name: str, source: str, config: dict[str, typing.Any]):
        self.name: str = name
        self.source: str = source
        self.config: dict[str, typing.Any] = config


class Action:
    def __init__(
        self,
        name: str,
        source: str,
        handlers: list[ActionHandler],
        config: dict[str, typing.Any],
    ):
        self.name: str = name
        self.source: str = source
        self.handlers: list[ActionHandler] = handlers
        self.config = config


class Project:
    def __init__(
        self,
        name: str,
        dir_path: Path,
        def_path: Path,
        status: ProjectStatus,
        actions: list[Action] | None = None,
    ) -> None:
        self.name = name
        self.dir_path = dir_path
        self.def_path = def_path
        self.status = status
        # None means actions were not collected yet
        # if project.status is RUNNING, then actions are not None
        self.actions = actions

    def __str__(self) -> str:
        return (
            f'Project(name="{self.name}", path="{self.dir_path}", status={self.status})'
        )

    def __repr__(self) -> str:
        return str(self)


class ProjectStatus(Enum):
    READY = auto()
    NO_FINECODE = auto()
    NO_FINECODE_SH = auto()
    RUNNER_FAILED = auto()
    RUNNING = auto()
    EXITED = auto()


RootActions = list[str]
ActionsDict = dict[str, Action]
AllActions = ActionsDict


# class View:
#     def __init__(self, name: str, source: str) -> None:
#         self.name = name
#         self.source = source


class TextDocumentInfo:
    def __init__(self, uri: str, version: str) -> None:
        self.uri = uri
        self.version = version

    def __str__(self) -> str:
        return f'TextDocumentInfo(uri="{self.uri}", version="{self.version}")'


# json object
type PartialResultRawValue = dict[str, typing.Any]


class PartialResult(typing.NamedTuple):
    token: int | str
    value: PartialResultRawValue


__all__ = [
    "RootActions",
    "ActionsDict",
    "AllActions",
    "Action",
    "Project",
    "TextDocumentInfo",
]
