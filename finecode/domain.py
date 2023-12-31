from __future__ import annotations
from pathlib import Path


class Action:
    # action is (collected) meta information about action in a project
    def __init__(
        self, name: str, subactions: list[str] | None = None, source: str | None = None
    ):
        self.name: str = name
        self.subactions: list[str] = subactions if subactions is not None else []
        self.source: str | None = source


class Package:
    def __init__(
        self,
        name: str,
        path: Path,
        subpackages: list[Package] | None = None,
        actions: list[Action] | None = None,
    ) -> None:
        self.name = name
        self.path = path
        if subpackages is not None:
            self.subpackages = subpackages
        else:
            self.subpackages: list[Package] = []
        if actions is not None:
            self.actions = actions
        else:
            self.actions: list[Action] = []


RootActions = list[str]
ActionsDict = dict[str, Action]
AllActions = ActionsDict


__all__ = ["RootActions", "ActionsDict", "AllActions", "Action", "Package"]
