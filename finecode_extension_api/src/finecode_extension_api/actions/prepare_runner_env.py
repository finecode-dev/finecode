import dataclasses
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions.create_envs import EnvInfo
from finecode_extension_api.actions.prepare_runner_envs import PrepareRunnerEnvsRunResult


@dataclasses.dataclass
class PrepareRunnerEnvRunPayload(code_action.RunActionPayload):
    env: EnvInfo


class PrepareRunnerEnvRunContext(
    code_action.RunActionContext[PrepareRunnerEnvRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: PrepareRunnerEnvRunPayload,
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


class PrepareRunnerEnvAction(
    code_action.Action[
        PrepareRunnerEnvRunPayload,
        PrepareRunnerEnvRunContext,
        PrepareRunnerEnvsRunResult,
    ]
):
    """Install finecode_extension_runner in environment."""

    PAYLOAD_TYPE = PrepareRunnerEnvRunPayload
    RUN_CONTEXT_TYPE = PrepareRunnerEnvRunContext
    RESULT_TYPE = PrepareRunnerEnvsRunResult
