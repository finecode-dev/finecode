from __future__ import annotations

from dataclasses import dataclass

from modapp.client import BaseChannel
from modapp.models.dataclass import DataclassModel as BaseModel


class ExtensionRunnerServiceCls:
    async def run_action(
        self, channel: BaseChannel, request: RunActionRequest
    ) -> RunActionResponse:
        return await channel.send_unary_unary(
            "/finecode.extension_runner.ExtensionRunnerService/RunAction",
            request,
            RunActionResponse,
        )

    async def update_config(
        self,
        channel: BaseChannel,
        request: UpdateConfigRequest,
    ) -> UpdateConfigResponse:
        return await channel.send_unary_unary(
            "/finecode.extension_runner.ExtensionRunnerService/UpdateConfig",
            request,
            UpdateConfigResponse,
        )


ExtensionRunnerService = ExtensionRunnerServiceCls()


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


@dataclass
class UpdateConfigRequest(BaseModel):
    working_dir: str  # Path?
    config: dict[str, str]

    __modapp_path__ = "finecode.extension_runner.UpdateConfigRequest"


@dataclass
class UpdateConfigResponse(BaseModel):
    __modapp_path__ = "finecode.extension_runner.UpdateConfigResponse"
