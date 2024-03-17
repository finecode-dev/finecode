from pydantic import BaseModel


class FinecodePresetConfig(BaseModel):
    source: str


class FinecodeActionConfig(BaseModel):
    name: str
    source: str | None = None


class FinecodeViewConfig(BaseModel):
    name: str
    source: str


class FinecodeConfig(BaseModel):
    presets: list[FinecodePresetConfig] = []
    actions: list[FinecodeActionConfig] = []
    views: list[FinecodeViewConfig] = []


class PresetConfig(BaseModel):
    extends: list[FinecodePresetConfig] = []
    actions: list[FinecodeActionConfig] = []


class SubactionConfig(BaseModel):
    name: str
    source: str


class ActionConfig(BaseModel):
    # TODO: validate that one of both is required
    source: str | None = None
    subactions: list[SubactionConfig] = []


class ViewConfig(BaseModel):
    name: str
    source: str
