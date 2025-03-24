from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import Any


class Preset:
    def __init__(self, source: str) -> None:
        self.source = source


class Action:
    # action is (collected) meta information about action in a project
    def __init__(
        self, name: str, subactions: list[str] | None = None, source: str | None = None
    ):
        self.name: str = name
        self.subactions: list[str] = subactions if subactions is not None else []
        self.source: str | None = source


class Project:
    def __init__(
        self,
        name: str,
        dir_path: Path,
        def_path: Path,
        status: ProjectStatus,
        subprojects: list[Project] | None = None,
        actions: list[Action] | None = None,
        # views: list[View] | None = None,
        # <action_name:config>
        actions_configs: dict[str, dict[str, Any]] | None = None,
        root_actions: list[str] | None = None,
    ) -> None:
        self.name = name
        self.dir_path = dir_path
        self.def_path = def_path
        self.status = status
        self.subprojects: list[Project] = subprojects if subprojects is not None else []
        # None means actions were not collected yet
        # if project.status is RUNNING, then actions are not None
        self.actions = actions
        self.root_actions: list[str] = root_actions if root_actions is not None else []

        # if views is not None:
        #     self.views = views
        # else:
        #     self.views: list[View] = []

        self.actions_configs: dict[str, dict[str, Any]] = (
            actions_configs if actions_configs is not None else {}
        )

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


__all__ = [
    "RootActions",
    "ActionsDict",
    "AllActions",
    "Action",
    "Project",
    "TextDocumentInfo",
]
