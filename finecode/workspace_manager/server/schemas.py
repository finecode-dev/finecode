from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from modapp.models.dataclass import DataclassModel


@dataclass
class BaseModel(DataclassModel):
    __model_config__ = {**DataclassModel.__model_config__, "camelCase": True}


@dataclass
class AddWorkspaceDirRequest(BaseModel):
    dir_path: str
    
    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirRequest"


@dataclass
class AddWorkspaceDirResponse(BaseModel):
    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirResponse"


@dataclass
class DeleteWorkspaceDirRequest(BaseModel):
    dir_path: str

    __modapp_path__ = "finecode.workspace_manager.DeleteWorkspaceDirRequest"


@dataclass
class DeleteWorkspaceDirResponse(BaseModel):
    __modapp_path__ = "finecode.workspace_manager.DeleteWorkspaceDirResponse"


@dataclass
class ListActionsRequest(BaseModel):
    parent_node_id: str = ""
    
    __modapp_path__ = "finecode.workspace_manager.ListActionsRequest"


@dataclass
class ActionTreeNode(BaseModel):
    node_id: str
    name: str
    node_type: NodeType
    subnodes: list[ActionTreeNode]
    
    class NodeType(IntEnum):
        DIRECTORY = 0
        PACKAGE = 1
        ACTION = 2
        PRESET = 3

    __modapp_path__ = "finecode.workspace_manager.ActionTreeNode"


@dataclass
class ListActionsResponse(BaseModel):
    nodes: list[ActionTreeNode]
    
    __modapp_path__ = "finecode.workspace_manager.ListActionsResponse"


@dataclass
class RunActionRequest(BaseModel):
    action_node_id: str
    apply_on: str # Path?
    
    __modapp_path__ = "finecode.workspace_manager.RunActionRequest"


@dataclass
class RunActionResponse(BaseModel):
    __modapp_path__ = "finecode.workspace_manager.RunActionResponse"
