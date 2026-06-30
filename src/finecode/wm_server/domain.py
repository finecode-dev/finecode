"""Core domain model for the Workspace Manager (WM).

This module is **pure data** — no I/O, no side effects, no async.  Every other
module in the WM imports from here; nothing here imports from the rest of the WM.

Project lifecycle
-----------------
The three-stage lifecycle is the central pattern in the domain:

    Project → CollectedProject → ResolvedProject

``Project``
    Discovered from the filesystem.  We know the project exists and have read
    its basic identity (name, path, status).  Actions and services have not
    been collected yet.

``CollectedProject``
    Actions and services have been read from the local definition file.
    Presets are *not* yet resolved.  Used during the bootstrap phase so that
    the dev-workspace Extension Runner can resolve presets.

``ResolvedProject``
    Fully resolved, including all contributions from presets.  This is the
    normal operating state of a project.

Actions and handlers
--------------------
An ``Action`` is a named operation (e.g. ``lint``, ``format``).  It holds a
list of ``ActionHandler`` instances, each describing one concrete implementation
and the execution environment it runs in.

Services
--------
A ``ServiceDeclaration`` maps a service interface to a concrete implementation
that the Extension Runner's DI container will inject into handler constructors.

Extension Runners
-----------------
An ``ExtensionRunner`` is a subprocess that loads and executes handler code in
an isolated execution environment.  The WM tracks each ER through its
``ExtensionRunnerStatus`` lifecycle.
"""

from __future__ import annotations

import dataclasses
import typing
from enum import Enum, StrEnum, auto
from pathlib import Path

import ordered_set

from finecode.wm_server.config.config_models import ErLoggingConfig


class ActionScope(StrEnum):
    """Dispatch scope declared by an Action.

    Attributes:
        PROJECT: The WM dispatches the action once per project that declares
            it.  This is the default and the right choice for most actions.
        WORKSPACE: The WM dispatches the action exactly once, routing it to
            the workspace-root project.  The handler is responsible for any
            per-project fan-out.  Use this when an action must reason about
            all projects together.
    """

    PROJECT = "project"
    WORKSPACE = "workspace"


class Preset:
    """A reference to an installed preset package.

    A preset is a distributable package that bundles action and handler
    declarations.  This class stores only the source path of the preset; the
    actual preset content (``preset.toml``) is loaded by the Extension Runner.

    Attributes:
        source: Source path identifying the preset
            (e.g. ``"fine_python_recommended"``).
    """

    def __init__(self, source: str) -> None:
        self.source = source

    def __str__(self) -> str:
        return f'Preset(source="{self.source}")'

    def __repr__(self) -> str:
        return str(self)


class ActionHandler:
    """A single handler registered for an action.

    Each handler is one concrete implementation of an action.  Multiple
    handlers may be registered for the same action; they run either
    sequentially (the default) or concurrently, depending on the action's
    ``runs_concurrently`` flag.

    Attributes:
        name: Human-readable identifier, unique within an action's handler
            list (e.g. ``"ruff"``).
        source: Source path identifying the handler implementation.
            For Python handlers this is the fully-qualified class path
            (e.g. ``"fine_python_ruff.RuffLintFilesHandler"``).
        config: Handler-specific configuration dict merged from the
            definition file.  Empty dict if none was provided.
        env: Execution environment name the handler runs in (e.g.
            ``"dev_no_runtime"``).  The WM creates one isolated environment
            per unique name.
        dependencies: Dependencies to install into ``env``
            (e.g. ``["fine_python_ruff~=0.2.0"]``).
    """

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
    """A service binding declaration for the Extension Runner's DI container.

    Maps a service interface to a concrete implementation.
    Services are singletons per Extension Runner and are injected into handler
    constructors by type annotation.  A project can override a preset's service
    by declaring the same ``interface`` in its definition file.

    Attributes:
        interface: Source path of the service protocol/interface.
            For Python services this is the fully-qualified class path
            (e.g.
            ``"finecode_extension_api.interfaces.ihttpclient.IHttpClient"``).
        source: Source path of the implementation.
            For Python services this is the fully-qualified class path
            (e.g. ``"finecode_httpclient.HttpClient"``).
        env: Execution environment name the service implementation runs in.
        dependencies: Dependencies to install into ``env`` for this service.
    """

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
    """A named operation that the WM can dispatch to Extension Runners.

    Actions are identified externally by their fully-qualified import path
    (``source``), not by their human-readable config ``name``. The name is the
    alias used in the definition file (e.g. ``lint``).

    Post-construction fields
    ------------------------
    Five fields are intentionally left unset at construction time and are
    populated later during config collection and ER startup:

    * ``canonical_source`` — set by the ER when it resolves the action class.
      It may differ from ``source`` when ``source`` is a re-exported alias.
      Remains ``None`` until the ER resolves it.
    * ``scope`` — set by the config collector after it reads the action class's
      ``SCOPE`` attribute from the ER.  ``None`` until the ER that hosts the
      action class resolves it (the ER that has the action's package installed).
    * ``runs_concurrently`` — set by the config collector after it reads the
      action class's ``HANDLER_EXECUTION`` attribute.  Defaults to ``False``.
    * ``parent_action_source`` — set by the ER's ``resolveActionMeta`` response.
      Canonical source of the parent action class, or ``None`` for top-level
      actions.  Remains ``None`` until the ER resolves it.
    * ``language`` — set by the ER's ``resolveActionMeta`` response.
      Language tag this action is specific to (e.g. ``"python"``), or ``None``
      for language-agnostic actions.  Remains ``None`` until the ER resolves it.

    Attributes:
        name: Config alias (e.g. ``"lint"``).
        source: Source path of the Action class as written in the definition
            file.  For Python actions this is the fully-qualified class path
            (e.g. ``"finecode_extension_api.actions.lint.LintAction"``).
        canonical_source: Canonical source path resolved by the ER.
            ``None`` until the ER has started and resolved the class.
        scope: Whether the action runs once per project or once per workspace.
        runs_concurrently: ``True`` when all handlers run in parallel and
            results are merged; ``False`` for sequential execution where each
            handler receives the accumulated result of the previous one.
        handlers: Registered handler implementations, in declaration order.
        config: Action-level configuration dict.  Empty dict if none provided.
    """

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
        # None until the ER that hosts the action class resolves it.
        self.scope: ActionScope | None = None
        # True when the action declares CONCURRENT handler execution.
        self.runs_concurrently: bool = False
        self.parent_action_source: str | None = None
        self.language: str | None = None
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

    Attributes:
        name: Human-readable project name read from the definition file.
            ``None`` when the name field is absent or when the definition
            file does not follow the expected layout.
        dir_path: Absolute path to the project's root directory.
        def_path: Absolute path to the project's definition file
            (e.g. ``pyproject.toml`` for Python projects).
        status: Discovery and configuration status.
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

    Invariant: ``status`` is always ``ProjectStatus.CONFIG_VALID``.

    Attributes:
        env_configs: Runner configuration keyed by env name.  Always contains
            an entry for every env referenced by the project's actions and
            services, even when the user has not provided explicit config
            (a default ``EnvConfig`` is used in that case).
        actions: Actions declared in local config, in declaration order.
            Does not include preset contributions yet.
        services: Service bindings declared in local config.
            Does not include preset contributions yet.
        action_handler_configs: Handler-level config overrides keyed by
            handler source path.  Populated from handler config entries in
            the definition file.
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
        if status != ProjectStatus.CONFIG_VALID:
            raise ValueError(
                f"CollectedProject requires status CONFIG_VALID, got {status!r}"
            )
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
        # Action sources for which WM metadata resolution has permanently failed
        # (all handler envs tried, including auto-repair).  Persists across ER
        # reconnections so repeated requests return null without re-running auto-repair.
        self.unresolvable_metadata_sources: set[str] = set()
        # Envs for which auto-repair (install + runner restart) already ran and failed.
        # Prevents re-triggering the same expensive install+restart for every subsequent
        # action that lives in the same broken env.
        self.failed_repair_envs: set[str] = set()

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
    handler configs include contributions from all presets, merged on top of
    the project's own declarations.

    Use :meth:`from_collected` to upgrade a :class:`CollectedProject` after
    preset resolution.  Do not construct directly.
    """

    @classmethod
    def from_collected(cls, collected: CollectedProject) -> ResolvedProject:
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
    """Discovery and configuration status of a project.

    Attributes:
        CONFIG_INVALID: The definition file (e.g. ``pyproject.toml``) could
            not be parsed, or required fields are missing or malformed.
        NO_FINECODE: The definition file is valid but contains no
            ``[tool.finecode]`` section.  FineCode ignores these projects for
            action dispatch but may still list them.
        CONFIG_VALID: The definition file is valid and contains FineCode
            configuration.  Only projects with this status are collected and
            resolved.
    """

    CONFIG_INVALID = auto()
    NO_FINECODE = auto()
    CONFIG_VALID = auto()


class RunnerConfig:
    """Runtime configuration passed to an Extension Runner at startup.

    Attributes:
        debug: Whether to start the ER in debug mode
        logging: Log level configuration for the ER process.  Defaults to
            ``ErLoggingConfig()`` (INFO level, no per-group overrides).
    """

    def __init__(self, debug: bool, logging: ErLoggingConfig | None = None) -> None:
        self.debug = debug
        self.logging: ErLoggingConfig = logging if logging is not None else ErLoggingConfig()

    def __str__(self) -> str:
        return f"RunnerConfig(debug={self.debug})"

    def __repr__(self) -> str:
        return str(self)


class EnvConfig:
    """Per-environment configuration for an Extension Runner.

    One ``EnvConfig`` exists for every environment (virtualenv) name that
    appears in a project's actions or services.  Even if the user has not
    explicitly provided configuration for an env, a default ``EnvConfig`` is
    always present.

    Attributes:
        runner_config: Startup configuration for the ER that runs in this env.
    """

    def __init__(self, runner_config: RunnerConfig) -> None:
        self.runner_config = runner_config

    def __str__(self) -> str:
        return f"EnvConfig(runner_config={self.runner_config})"

    def __repr__(self) -> str:
        return str(self)


# Maps action name → Action for a single project or the full workspace.
ActionsDict = dict[str, Action]


class ExtensionRunnerStatus(Enum):
    """Lifecycle status of an Extension Runner process.

    Attributes:
        NO_VENV: The execution environment for this env does not exist yet.
            Run ``prepare-envs`` to create it.
        INITIALIZING: The ER process has been started and the WM is waiting
            for it to signal readiness.
        REPAIRING: ``install_env_for_project`` is running for this env before a
            runner restart.  ``get_or_start_runner`` waits on
            ``ExtensionRunnerInfo.repair_complete_event`` when it sees this
            status, preventing concurrent duplicate repairs.
        FAILED: The ER process failed to start or crashed during
            initialization.
        RUNNING: The ER process is up and accepting requests.
        EXITED: The ER process exited cleanly (e.g. after a shutdown request).
    """

    NO_VENV = auto()
    INITIALIZING = auto()
    REPAIRING = auto()
    FAILED = auto()
    RUNNING = auto()
    EXITED = auto()


@dataclasses.dataclass
class ExtensionRunner:
    """A tracked Extension Runner process instance.

    Represents the WM's view of a single ER subprocess — its location,
    environment, current lifecycle status, and log file.

    Attributes:
        working_dir_path: Absolute path to the project directory this ER was
            started for.
        env_name: Execution environment name this ER manages (e.g. ``"dev_no_runtime"``).
        status: Current lifecycle status.
        log_file_path: Path to the ER's log file, or ``None`` if logging to
            a file was not configured.
    """

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
    """In-memory state of an open text document tracked by the WM.

    The WM keeps a copy of each opened document so it can re-supply the
    content to Extension Runners that restart after a crash.  The WM's own
    protocol mirrors the LSP open/change/close notification model to populate
    this state.

    Attributes:
        uri: Document URI (e.g. ``"file:///home/user/myproject/main.py"``).
        version: Document version counter from the open/change notification.
            Both ``int`` and ``string`` values are allowed to match the
            protocol's flexibility.
        text: Full document text.  Empty string if no content has been
            supplied yet (before the first change notification).
    """

    def __init__(self, uri: str, version: str | int, text: str = "") -> None:
        self.uri = uri
        self.version = version
        self.text = text

    def __str__(self) -> str:
        return f'TextDocumentInfo(uri="{self.uri}", version="{self.version}")'


# Raw JSON object carrying a partial-result value in the WM protocol.
type PartialResultRawValue = dict[str, typing.Any]


class PartialResult(typing.NamedTuple):
    """A partial-result notification value in the WM protocol.

    Used to forward incremental action results from an ER to the WM client
    before the action completes.

    Attributes:
        token: Partial-result token agreed on with the client.
        value: Raw JSON object (action-specific schema).
    """

    token: int | str
    value: PartialResultRawValue


# Raw JSON object carrying a progress value in the WM protocol.
# The ``"type"`` field is one of ``"begin"``, ``"report"``, or ``"end"``.
type ProgressRawValue = dict[str, typing.Any]


class ProgressNotification(typing.NamedTuple):
    """A work-done progress notification value in the WM protocol.

    Used to report action execution progress from an ER to the WM client.

    Attributes:
        token: Work-done progress token agreed on with the client.
        value: Raw JSON object with a ``"type"`` field: ``"begin"``,
            ``"report"``, or ``"end"``.
    """

    token: int | str
    value: ProgressRawValue


__all__ = [
    "ActionsDict",
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
