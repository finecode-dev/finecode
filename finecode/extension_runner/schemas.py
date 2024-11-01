from dataclasses import dataclass

from modapp.models.dataclass import DataclassModel


@dataclass
class BaseModel(DataclassModel): ...


@dataclass
class UpdateConfigRequest(BaseModel):
    working_dir: str  # Path?
    config: dict[str, str]

    __modapp_path__ = "finecode.extension_runner.UpdateConfigRequest"


@dataclass
class UpdateConfigResponse(BaseModel):
    __modapp_path__ = "finecode.extension_runner.UpdateConfigResponse"


@dataclass
class RunActionRequest(BaseModel):
    action_name: str
    apply_on: str  # Path?
    apply_on_text: str

    __modapp_path__ = "finecode.extension_runner.RunActionRequest"


@dataclass
class RunActionResponse(BaseModel):
    result_text: str

    __modapp_path__ = "finecode.extension_runner.RunActionResponse"
