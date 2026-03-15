import dataclasses
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions.create_envs import EnvInfo
from finecode_extension_api.actions.prepare_handler_envs import (
    PrepareHandlerEnvsRunResult,
)


@dataclasses.dataclass
class PrepareHandlerEnvRunPayload(code_action.RunActionPayload):
    env: EnvInfo


class PrepareHandlerEnvRunContext(
    code_action.RunActionContext[PrepareHandlerEnvRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: PrepareHandlerEnvRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )

        self.project_def: dict[str, typing.Any] | None = None


class PrepareHandlerEnvAction(
    code_action.Action[
        PrepareHandlerEnvRunPayload,
        PrepareHandlerEnvRunContext,
        PrepareHandlerEnvsRunResult,
    ]
):
    PAYLOAD_TYPE = PrepareHandlerEnvRunPayload
    RUN_CONTEXT_TYPE = PrepareHandlerEnvRunContext
    RESULT_TYPE = PrepareHandlerEnvsRunResult
