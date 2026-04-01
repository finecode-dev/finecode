import dataclasses
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments.create_envs_action import EnvInfo
from finecode_extension_api.actions.environments.install_envs_action import (
    InstallEnvsRunResult,
)


@dataclasses.dataclass
class InstallEnvRunPayload(code_action.RunActionPayload):
    env: EnvInfo


class InstallEnvRunContext(code_action.RunActionContext[InstallEnvRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: InstallEnvRunPayload,
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


class InstallEnvAction(
    code_action.Action[
        InstallEnvRunPayload,
        InstallEnvRunContext,
        InstallEnvsRunResult,
    ]
):
    """Install dependencies into environment."""

    PAYLOAD_TYPE = InstallEnvRunPayload
    RUN_CONTEXT_TYPE = InstallEnvRunContext
    RESULT_TYPE = InstallEnvsRunResult
