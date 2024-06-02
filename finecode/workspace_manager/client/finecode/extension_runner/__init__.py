from modapp.client import BaseChannel

import finecode.extension_runner.schemas as schemas


class ExtensionRunnerServiceCls:
    async def run_action(
        self, channel: BaseChannel, request: schemas.RunActionRequest
    ) -> schemas.RunActionResponse:
        return await channel.send_unary_unary(
            "/finecode.extension_runner.ExtensionRunnerService/RunAction",
            request,
            schemas.RunActionResponse,
        )

    async def update_config(
        self, channel: BaseChannel, request: schemas.UpdateConfigRequest
    ) -> schemas.UpdateConfigResponse:
        return await channel.send_unary_unary(
            "/finecode.extension_runner.ExtensionRunnerService/UpdateConfig",
            request,
            schemas.UpdateConfigResponse,
        )


ExtensionRunnerService = ExtensionRunnerServiceCls()
