from __future__ import annotations

import typing
from enum import Enum, auto
from pathlib import Path

import ordered_set


class Preset:
    def __init__(self, source: str) -> None:
        self.source = source


class ActionHandler:
    def __init__(
        self,
        name: str,
        source: str,
        config: dict[str, typing.Any],
        env: str,
        dependencies: list[str],
    ):
        self.name: str = name
        self.source: str = source
        self.config: dict[str, typing.Any] = config
        self.env: str = env
        self.dependencies: list[str] = dependencies


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
        env_configs: dict[str, EnvConfig],
        actions: list[Action] | None = None,
    ) -> None:
        self.name = name
        self.dir_path = dir_path
        self.def_path = def_path
        self.status = status
        # None means actions were not collected yet
        # if project.status is RUNNING, then actions are not None
        self.actions = actions
        # config by handler source
        self.action_handler_configs: dict[str, dict[str, typing.Any]] = {}
        # config by env name
        # it always contains configs for all environments, even if user hasn't provided
        # one explicitly(=there is a default config)
        self.env_configs: dict[str, EnvConfig] = env_configs

    def __str__(self) -> str:
        return (
            f'Project(name="{self.name}", path="{self.dir_path}", status={self.status})'
        )

    def __repr__(self) -> str:
        return str(self)

    @property
    def envs(self) -> list[str]:
        if self.actions is None:
            raise ValueError("Actions are not collected yet")

        all_envs_set = ordered_set.OrderedSet([])
        for action in self.actions:
            action_envs = [handler.env for handler in action.handlers]
            all_envs_set |= ordered_set.OrderedSet(action_envs)

        return list(all_envs_set)


class ProjectStatus(Enum):
    CONFIG_INVALID = auto()
    # config valid, but no finecode in project
    NO_FINECODE = auto()
    # config valid and finecode is used in project
    CONFIG_VALID = auto()


class RunnerConfig:
    def __init__(self, debug: bool) -> None:
        self.debug = debug


class EnvConfig:
    def __init__(self, runner_config: RunnerConfig) -> None:
        self.runner_config = runner_config


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
    "RunnerConfig",
    "EnvConfig",
]
