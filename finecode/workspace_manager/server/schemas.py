from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from modapp.models.dataclass import DataclassModel


@dataclass
class BaseModel(DataclassModel):
    __model_config__ = {**DataclassModel.__model_config__, "camelCase": True}


@dataclass
class AddWorkspaceDirRequest(BaseModel):
    dir_path: str


@dataclass
class AddWorkspaceDirResponse(BaseModel):
    ...


@dataclass
class DeleteWorkspaceDirRequest(BaseModel):
    dir_path: str


@dataclass
class DeleteWorkspaceDirResponse(BaseModel):
    ...


@dataclass
class ListActionsRequest(BaseModel):
    parent_node_id: str = ""


@dataclass
class ActionTreeNode(BaseModel):
    node_id: str
    name: str
    node_type: NodeType
    subnodes: list[ActionTreeNode]

    class NodeType(IntEnum):
        DIRECTORY = 0
        PROJECT = 1
        ACTION = 2
        PRESET = 3


@dataclass
class ListActionsResponse(BaseModel):
    nodes: list[ActionTreeNode]


@dataclass
class RunActionRequest(BaseModel):
    action_node_id: str
    apply_on: str  # Path?
    apply_on_text: str


@dataclass
class RunActionResponse(BaseModel):
    result: dict[str, Any]
