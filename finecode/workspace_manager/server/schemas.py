from dataclasses import dataclass
from modapp.models.dataclass import DataclassModel


@dataclass
class AddWorkspaceDirRequest(DataclassModel):
    dir_path: str
    
    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirRequest"


@dataclass
class AddWorkspaceDirResponse(DataclassModel):
    __modapp_path__ = "finecode.workspace_manager.AddWorkspaceDirResponse"


@dataclass
class DeleteWorkspaceDirRequest(DataclassModel):
    dir_path: str

    __modapp_path__ = "finecode.workspace_manager.DeleteWorkspaceDirRequest"


@dataclass
class DeleteWorkspaceDirResponse(DataclassModel):
    __modapp_path__ = "finecode.workspace_manager.DeleteWorkspaceDirResponse"


@dataclass
class ListActionsRequest(DataclassModel):
    __modapp_path__ = "finecode.workspace_manager.ListActionsRequest"


@dataclass
class NormalizedAction:
    name: str
    project_path: str
    subactions: list[str]
    is_package: bool
    
    __modapp_path__ = "finecode.workspace_manager.NormalizedAction"


@dataclass
class ListActionsResponse(DataclassModel):
    root_action: str
    actions_by_path: dict[str, NormalizedAction]
    
    __modapp_path__ = "finecode.workspace_manager.ListActionsResponse"


@dataclass
class RunActionRequest(DataclassModel):
    action_name: str
    apply_on: str # Path?
    
    __modapp_path__ = "finecode.workspace_manager.RunActionRequest"


@dataclass
class RunActionResponse(DataclassModel):
    __modapp_path__ = "finecode.workspace_manager.RunActionResponse"
