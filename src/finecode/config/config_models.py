from typing import Any

from pydantic import BaseModel, ValidationError


class FinecodePresetDefinition(BaseModel):
    source: str


class FinecodeActionDefinition(BaseModel):
    name: str
    source: str | None = None


class FinecodeViewDefinition(BaseModel):
    name: str
    source: str


class PresetDefinition(BaseModel):
    extends: list[FinecodePresetDefinition] = []


class ActionHandlerDefinition(BaseModel):
    name: str
    source: str
    env: str
    dependencies: list[str] = []
    config: dict[str, Any] | None = None


class ActionDefinition(BaseModel):
    source: str
    handlers: list[ActionHandlerDefinition] = []
    config: dict[str, Any] | None = None


class ViewDefinition(BaseModel):
    name: str
    source: str


class ConfigurationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
