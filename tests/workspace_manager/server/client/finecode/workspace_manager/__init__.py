from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from modapp.client import BaseChannel
from modapp.models.dataclass import DataclassModel


@dataclass
class BaseModel(DataclassModel):
    __model_config__ = {**DataclassModel.__model_config__, "camelCase": True}


class WorkspaceManagerServiceCls:
    async def list_actions(
        self, channel: BaseChannel, request: ListActionsRequest
    ) -> ListActionsResponse:
        return await channel.send_unary_unary(
            "/finecode.workspace_manager.WorkspaceManagerService/ListActions",
            request,
            ListActionsResponse,
        )

    async def add_workspace_dir(
        self, channel: BaseChannel, request: AddWorkspaceDirRequest
    ) -> AddWorkspaceDirResponse:
        return await channel.send_unary_unary(
            "/finecode.workspace_manager.WorkspaceManagerService/AddWorkspaceDir",
            request,
            AddWorkspaceDirResponse,
        )

    async def run_action(
        self, channel: BaseChannel, request: RunActionRequest
    ) -> RunActionResponse:
        return await channel.send_unary_unary(
            "/finecode.workspace_manager.WorkspaceManagerService/RunAction",
            request,
            RunActionResponse,
        )


WorkspaceManagerService = WorkspaceManagerServiceCls()


@dataclass
class ListActionsRequest(BaseModel):
    parent_node_id: str

    __modapp_path__ = "finecode.workspace_manager.ListActionsRequest"


@dataclass
class ActionTreeNode(BaseModel):
    node_id: str
    name: str
    subnodes: list[ActionTreeNode]

    class NodeType(IntEnum):
        DIRECTORY = 0
        PROJECT = 1
        ACTION = 2
        PRESET = 3

    __modapp_path__ = "finecode.workspace_manager.ActionTreeNode"


@dataclass
class ListActionsResponse(BaseModel):
    nodes: list[ActionTreeNode]

    __modapp_path__ = "finecode.workspace_manager.ListActionsResponse"


@dataclass
class AddWorkspaceDirRequest(BaseModel):
    dir_path: str

    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirRequest"


@dataclass
class AddWorkspaceDirResponse(BaseModel):
    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirResponse"


@dataclass
class RunActionRequest(BaseModel):
    action_node_id: str
    apply_on: str  # Path?
    apply_on_text: str

    __modapp_path__ = "finecode.workspace_manager.RunActionRequest"


@dataclass
class RunActionResponse(BaseModel):
    result_text: str

    __modapp_path__ = "finecode.workspace_manager.RunActionResponse"
