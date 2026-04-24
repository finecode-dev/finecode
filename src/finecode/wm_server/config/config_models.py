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
class ActionHandlerDefinition:
    name: str
    source: str = ""
    env: str = ""
    dependencies: list[str] = field(default_factory=list)
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
    source: str
    handlers: list[ActionHandlerDefinition] = field(default_factory=list)
    handlers_mode: str = "merge"  # "merge" or "replace"
    config: dict[str, Any] | None = None


@dataclass
class ViewDefinition:
    name: str
    source: str


class ConfigurationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
