from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class BaseSchema:
    def to_dict(self):
        return asdict(self)


@dataclass
class ActionHandler(BaseSchema):
    name: str
    source: str | None = None
    config: dict[str, Any] | None = None


@dataclass
class Action(BaseSchema):
    name: str
    handlers: list[ActionHandler]
    source: str | None = None
    config: dict[str, Any] | None = None


@dataclass
class UpdateConfigRequest(BaseSchema):
    working_dir: Path
    project_name: str
    actions: dict[str, Action]


@dataclass
class UpdateConfigResponse(BaseSchema): ...


@dataclass
class RunActionRequest(BaseSchema):
    action_name: str
    params: dict[str, Any]


@dataclass
class RunActionOptions(BaseSchema):
    partial_result_token: int | str | None = None


@dataclass
class RunActionResponse(BaseSchema):
    # result can be empty e.g. if it was sent as a list of partial results
    result: dict[str, Any] | None
