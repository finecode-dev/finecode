from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class BaseSchema:
    def to_dict(self):
        return asdict(self)


@dataclass
class Action(BaseSchema):
    name: str
    actions: list[str]
    source: str | None = None


@dataclass
class UpdateConfigRequest(BaseSchema):
    working_dir: Path
    project_name: str
    actions: dict[str, Action]
    actions_configs: dict[str, dict[str, Any]]


@dataclass
class UpdateConfigResponse(BaseSchema):
    ...


@dataclass
class RunActionRequest(BaseSchema):
    action_name: str
    apply_on_text: str
    apply_on: Path | None = None


@dataclass
class RunActionResponse(BaseSchema):
    result_text: str
