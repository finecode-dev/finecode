from __future__ import annotations

import dataclasses
import typing
from enum import Enum, auto
from pathlib import Path

import ordered_set


class Preset:
    def __init__(self, source: str) -> None:
        self.source = source

    def __str__(self) -> str:
        return f'Preset(source="{self.source}")'

    def __repr__(self) -> str:
        return str(self)


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

    def __str__(self) -> str:
        return f'ActionHandler(name="{self.name}", source="{self.source}", env="{self.env}")'

    def __repr__(self) -> str:
        return str(self)

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "name": self.name,
            "source": self.source,
            "config": self.config,
            "env": self.env,
            "dependencies": self.dependencies,
        }


class ServiceDeclaration:
    def __init__(
        self,
        interface: str,
        source: str,
        env: str,
        dependencies: list[str],
    ):
        self.interface = interface
        self.source = source
        self.env = env
        self.dependencies = dependencies

    def __str__(self) -> str:
        return f'ServiceDeclaration(interface="{self.interface}", source="{self.source}", env="{self.env}")'

    def __repr__(self) -> str:
        return str(self)

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "interface": self.interface,
            "source": self.source,
            "env": self.env,
            "dependencies": self.dependencies,
        }


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
        # Canonical (fully qualified) import path resolved by the Extension Runner
        # at startup. May differ from source when source is a re-exported path.
        self.canonical_source: str | None = None
        # True when the action declares CONCURRENT handler execution.
        self.runs_concurrently: bool = False
        self.handlers: list[ActionHandler] = handlers
        self.config = config

    def __str__(self) -> str:
        handler_names = [h.name for h in self.handlers]
        return f'Action(name="{self.name}", handlers={handler_names})'

    def __repr__(self) -> str:
        return str(self)

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "name": self.name,
            "source": self.source,
            "handlers": [handler.to_dict() for handler in self.handlers],
            "config": self.config,
        }


class Project:
    """A project discovered in the workspace.

    This is the initial state: we know the project exists and have read its
    basic identity (name, path, status), but actions and services have not
    been collected yet.

    Transitions:
        Project → CollectedProject  via collect_actions.collect_project()
    """

    def __init__(
        self,
        name: str | None,
        dir_path: Path,
        def_path: Path,
        status: ProjectStatus,
    ) -> None:
        self.name = name
        self.dir_path = dir_path
        self.def_path = def_path
        self.status = status

    def __str__(self) -> str:
        return (
            f'Project(name="{self.name}", path="{self.dir_path}", status={self.status})'
        )

    def __repr__(self) -> str:
        return str(self)


class CollectedProject(Project):
    """A project whose actions and services have been collected from local config.

    Presets are **not** yet resolved. This state is used during the bootstrap
    phase: the dev-workspace Extension Runner is started with the locally
    collected actions so that it can resolve presets.  Once presets are
    resolved, the project is upgraded to :class:`ResolvedProject`.

    Transitions:
        Project          → CollectedProject  via collect_actions.collect_project()
        CollectedProject → ResolvedProject   via ResolvedProject.from_collected()
                                             (after re-reading config with presets)
    """

    def __init__(
        self,
        name: str | None,
        dir_path: Path,
        def_path: Path,
        status: ProjectStatus,
        env_configs: dict[str, EnvConfig],
        actions: list[Action],
        services: list[ServiceDeclaration],
        action_handler_configs: dict[str, dict[str, typing.Any]],
    ) -> None:
        super().__init__(name, dir_path, def_path, status)
        # config by env name — always contains configs for all environments, even if
        # the user hasn't provided one explicitly (there is always a default config)
        self.env_configs: dict[str, EnvConfig] = env_configs
        self.actions: list[Action] = actions
        self.services: list[ServiceDeclaration] = services
        # config by handler source
        self.action_handler_configs: dict[str, dict[str, typing.Any]] = (
            action_handler_configs
        )

    @property
    def envs(self) -> list[str]:
        all_envs_set = ordered_set.OrderedSet([])
        for action in self.actions:
            action_envs = [handler.env for handler in action.handlers]
            all_envs_set |= ordered_set.OrderedSet(action_envs)
        all_envs_set |= ordered_set.OrderedSet([svc.env for svc in self.services])
        return list(all_envs_set)


class ResolvedProject(CollectedProject):
    """A project with fully resolved configuration, including all presets.

    This is the normal operating state of a project.  Actions, services, and
    handler configs include contributions from all presets.

    Use :meth:`from_collected` to upgrade a :class:`CollectedProject` after
    preset resolution.
    """

    @classmethod
    def from_collected(cls, collected: CollectedProject) -> "ResolvedProject":
        """Upgrade a CollectedProject to ResolvedProject after preset resolution."""
        return cls(
            name=collected.name,
            dir_path=collected.dir_path,
            def_path=collected.def_path,
            status=collected.status,
            env_configs=collected.env_configs,
            actions=collected.actions,
            services=collected.services,
            action_handler_configs=collected.action_handler_configs,
        )


class ProjectStatus(Enum):
    CONFIG_INVALID = auto()
    # config valid, but no finecode in project
    NO_FINECODE = auto()
    # config valid and finecode is used in project
    CONFIG_VALID = auto()


class RunnerConfig:
    def __init__(self, debug: bool) -> None:
        self.debug = debug

    def __str__(self) -> str:
        return f"RunnerConfig(debug={self.debug})"

    def __repr__(self) -> str:
        return str(self)


class EnvConfig:
    def __init__(self, runner_config: RunnerConfig) -> None:
        self.runner_config = runner_config

    def __str__(self) -> str:
        return f"EnvConfig(runner_config={self.runner_config})"

    def __repr__(self) -> str:
        return str(self)


RootActions = list[str]
ActionsDict = dict[str, Action]
AllActions = ActionsDict


# class View:
#     def __init__(self, name: str, source: str) -> None:
#         self.name = name
#         self.source = source


class ExtensionRunnerStatus(Enum):
    NO_VENV = auto()
    INITIALIZING = auto()
    FAILED = auto()
    RUNNING = auto()
    EXITED = auto()


@dataclasses.dataclass
class ExtensionRunner:
    working_dir_path: Path
    env_name: str
    status: ExtensionRunnerStatus
    log_file_path: Path | None = None

    @property
    def readable_id(self) -> str:
        return f"{self.working_dir_path} ({self.env_name})"

    @property
    def logs_path(self) -> Path | None:
        return self.log_file_path


class TextDocumentInfo:
    def __init__(self, uri: str, version: str | int, text: str = "") -> None:
        self.uri = uri
        self.version = version
        self.text = text

    def __str__(self) -> str:
        return f'TextDocumentInfo(uri="{self.uri}", version="{self.version}")'


# json object
type PartialResultRawValue = dict[str, typing.Any]


class PartialResult(typing.NamedTuple):
    token: int | str
    value: PartialResultRawValue


# json object with "type" field: "begin", "report", or "end"
type ProgressRawValue = dict[str, typing.Any]


class ProgressNotification(typing.NamedTuple):
    token: int | str
    value: ProgressRawValue


__all__ = [
    "RootActions",
    "ActionsDict",
    "AllActions",
    "Action",
    "ServiceDeclaration",
    "Project",
    "CollectedProject",
    "ResolvedProject",
    "TextDocumentInfo",
    "RunnerConfig",
    "EnvConfig",
    "ExtensionRunnerStatus",
    "ExtensionRunner",
]
