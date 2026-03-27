from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from finecode_extension_api import code_action


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
class ServiceDeclaration(BaseSchema):
    interface: str
    source: str


@dataclass
class UpdateConfigRequest(BaseSchema):
    working_dir: Path
    project_name: str
    project_def_path: Path
    actions: dict[str, Action]
    action_handler_configs: dict[str, dict[str, Any]]
    services: list[ServiceDeclaration] = field(default_factory=list)
    # If provided, eagerly instantiate these handlers after config update.
    # Keys are action names, values are lists of handler names within that action.
    # None means no eager initialization (lazy, on first use).
    handlers_to_initialize: dict[str, list[str]] | None = None


@dataclass
class UpdateConfigResponse(BaseSchema): ...


@dataclass
class RunActionRequest(BaseSchema):
    action_name: str
    params: dict[str, Any]


@dataclass
class RunActionOptions(BaseSchema):
    meta: code_action.RunActionMeta
    partial_result_token: int | str | None = None
    progress_token: int | str | None = None
    result_formats: list[Literal["json"] | Literal["string"]] = field(default_factory=lambda: ["json"])


@dataclass
class RunActionResponse(BaseSchema):
    return_code: int
    # result can be empty(=None) e.g. if it was sent as a list of partial results
    result_by_format: dict[str, dict[str, Any] | str] | None
