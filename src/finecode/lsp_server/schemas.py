from __future__ import annotations

from enum import IntEnum
from typing import Any

import pydantic
from pydantic.alias_generators import to_camel


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        alias_generator=pydantic.AliasGenerator(
            serialization_alias=to_camel,
        )
    )


class AddWorkspaceDirRequest(BaseModel):
    dir_path: str


class AddWorkspaceDirResponse(BaseModel): ...


class DeleteWorkspaceDirRequest(BaseModel):
    dir_path: str


class DeleteWorkspaceDirResponse(BaseModel): ...


class ListActionsRequest(BaseModel):
    parent_node_id: str = ""


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
        PRESET = 3


class ListActionsResponse(BaseModel):
    nodes: list[ActionTreeNode]


class RunActionRequest(BaseModel):
    action_node_id: str
    params: dict[str, Any]


class RunActionResponse(BaseModel):
    result: dict[str, Any]
