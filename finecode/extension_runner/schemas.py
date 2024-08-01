from dataclasses import dataclass
from modapp.models.dataclass import DataclassModel


@dataclass
class UpdateConfigRequest(DataclassModel):
    working_dir: str # Path?
    config: dict[str, str]

    __modapp_path__ = "finecode.extension_runner.UpdateConfigRequest"


@dataclass
class UpdateConfigResponse(DataclassModel):
    __modapp_path__ = "finecode.extension_runner.UpdateConfigResponse"


@dataclass
class RunActionRequest(DataclassModel):
    action_name: str
    apply_on: str # Path?
    
    __modapp_path__ = "finecode.extension_runner.RunActionRequest"


@dataclass
class RunActionResponse(DataclassModel):
    __modapp_path__ = "finecode.extension_runner.RunActionResponse"
