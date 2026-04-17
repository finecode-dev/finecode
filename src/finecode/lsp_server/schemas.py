from __future__ import annotations

import dataclasses
from enum import IntEnum
from typing import Any

import apischema


@dataclasses.dataclass
class BaseModel:
    def to_dict(self) -> dict[str, Any]:
        return apischema.serialize(
            type(self),
            self,
            aliaser=apischema.utils.to_camel_case,
        )


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
