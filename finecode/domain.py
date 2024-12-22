from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import Any


class Preset:
    def __init__(self, source: str) -> None:
        self.source = source


class Action:
    # action is (collected) meta information about action in a project
    def __init__(self, name: str, subactions: list[str] | None = None, source: str | None = None):
        self.name: str = name
        self.subactions: list[str] = subactions if subactions is not None else []
        self.source: str | None = source


class Project:
    def __init__(
        self,
        name: str,
        path: Path,
        status: ProjectStatus,
        subprojects: list[Project] | None = None,
        actions: list[Action] | None = None,
        views: list[View] | None = None,
        # <action_name:config>
        actions_configs: dict[str, dict[str, Any]] | None = None,
        root_actions: list[str] | None = None,
    ) -> None:
        self.name = name
        self.path = path
        self.status = status
        if subprojects is not None:
            self.subprojects = subprojects
        else:
            self.subprojects: list[Project] = []
        # None means actions were not collected yet
        self.actions = actions
        if root_actions is not None:
            self.root_actions = root_actions
        else:
            self.root_actions = []

        if views is not None:
            self.views = views
        else:
            self.views: list[View] = []

        if actions_configs is not None:
            self.actions_configs = actions_configs
        else:
            self.actions_configs: dict[str, dict[str, Any]] = {}

    def __str__(self) -> str:
        return f'Project(name="{self.name}", path="{self.path}")'


class ProjectStatus(Enum):
    READY = auto()
    NO_FINECODE = auto()
    NO_FINECODE_SH = auto()


RootActions = list[str]
ActionsDict = dict[str, Action]
AllActions = ActionsDict


class View:
    def __init__(self, name: str, source: str) -> None:
        self.name = name
        self.source = source


__all__ = ["RootActions", "ActionsDict", "AllActions", "Action", "Project"]
