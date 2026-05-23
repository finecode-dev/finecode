# docs: docs/concepts.md, docs/configuration.md
from dataclasses import dataclass, field
from typing import Any

from cattrs import ClassValidationError as ValidationError


@dataclass
class FinecodePresetDefinition:
    source: str


@dataclass
class FinecodeActionDefinition:
    name: str
    source: str | None = None


@dataclass
class FinecodeViewDefinition:
    name: str
    source: str


@dataclass
class PresetDefinition:
    extends: list[FinecodePresetDefinition] = field(default_factory=list)


@dataclass
class ExtensionDefinition:
    name: str
    dependencies_override: list[str] = field(default_factory=list)


@dataclass
class ActionHandlerDefinition:
    name: str
    source: str = ""
    env: str = ""
    dependencies: list[str] = field(default_factory=list)
    dependencies_override: list[str] = field(default_factory=list)
    config: dict[str, Any] | None = None
    enabled: bool = True


@dataclass
class ServiceDefinition:
    interface: str
    source: str
    env: str
    dependencies: list[str] = field(default_factory=list)


@dataclass
class ActionDefinition:
    # source is optional because a project-level config entry may only provide
    # handler overrides (config, enabled, env, …) for an action whose source is
    # declared by a preset. The complete merged action must have a source; that
    # is enforced when actions are collected for use.
    source: str | None = None
    handlers: list[ActionHandlerDefinition] = field(default_factory=list)
    handlers_mode: str = "merge"  # "merge" or "replace"
    config: dict[str, Any] | None = None


@dataclass
class ViewDefinition:
    name: str
    source: str


@dataclass
class ErLoggingConfig:
    default_level: str = "INFO"
    log_groups: dict[str, str] = field(default_factory=dict)


@dataclass
class WmTelemetryConfig:
    otlp_endpoint: str | None = None


@dataclass
class ErEnvConfig:
    debug: bool = False
    logging: ErLoggingConfig = field(default_factory=ErLoggingConfig)


from finecode.wm_server.errors import ConfigurationError, PresetPackageNotInstalledError

__all__ = ["ConfigurationError", "PresetPackageNotInstalledError"]
