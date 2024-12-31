
from pathlib import Path
from typing import Any


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
        actions: dict[str, Action],
        # <action_name:config>
        actions_configs: dict[str, dict[str, Any]],
    ) -> None:
        self.name = name
        self.path = path
        self.actions = actions
        self.actions_configs = actions_configs

    def __str__(self) -> str:
        return f'Project(name="{self.name}", path="{self.path}")'

