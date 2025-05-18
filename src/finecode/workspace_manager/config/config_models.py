from typing import Any

from pydantic import BaseModel


class FinecodePresetDefinition(BaseModel):
    source: str


class FinecodeActionDefinition(BaseModel):
    name: str
    source: str | None = None


class FinecodeViewDefinition(BaseModel):
    name: str
    source: str


class FinecodeConfig(BaseModel):
    presets: list[FinecodePresetDefinition] = []
    actions: list[FinecodeActionDefinition] = []
    views: list[FinecodeViewDefinition] = []
    action: dict[str, dict[str, Any]] = {}
    action_handler: dict[str, dict[str, Any]] = {}


class PresetDefinition(BaseModel):
    extends: list[FinecodePresetDefinition] = []


class ActionHandlerDefinition(BaseModel):
    name: str
    source: str


class ActionDefinition(BaseModel):
    source: str | None = None
    handlers: list[ActionHandlerDefinition] = []
    config: dict[str, Any] | None = None


class ViewDefinition(BaseModel):
    name: str
    source: str
