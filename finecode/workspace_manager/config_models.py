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


class PresetDefinition(BaseModel):
    extends: list[FinecodePresetDefinition] = []
    actions: list[FinecodeActionDefinition] = []


class SubactionDefinition(BaseModel):
    name: str
    source: str


class ActionDefinition(BaseModel):
    # TODO: validate that one of both is required
    source: str | None = None
    subactions: list[SubactionDefinition] = []
    config: dict[str, Any] | None = None


class ViewDefinition(BaseModel):
    name: str
    source: str
