from __future__ import annotations

import dataclasses
import enum
from enum import IntEnum
from typing import Any


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _to_camel_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {_to_camel(f.name): _to_camel_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    elif isinstance(obj, list):
        return [_to_camel_dict(item) for item in obj]
    elif isinstance(obj, enum.Enum):
        return obj.value
    return obj


@dataclasses.dataclass
class BaseModel:
    def to_dict(self) -> dict[str, Any]:
        return _to_camel_dict(self)


@dataclasses.dataclass
class ListActionsRequest(BaseModel):
    parent_node_id: str = ""


@dataclasses.dataclass
class ActionTreeNode(BaseModel):
    node_id: str
    name: str
    node_type: NodeType
    subnodes: list[ActionTreeNode]
    status: str

    class NodeType(IntEnum):
        DIRECTORY = 0
        PROJECT = 1
        ACTION = 2
        ACTION_GROUP = 3
        PRESET = 4
        ENV_GROUP = 5
        ENV = 6


@dataclasses.dataclass
class ListActionsResponse(BaseModel):
    nodes: list[ActionTreeNode]


@dataclasses.dataclass
class RunActionRequest(BaseModel):
    action_node_id: str
    params: dict[str, Any]


@dataclasses.dataclass
class RunActionResponse(BaseModel):
    result: dict[str, Any]
