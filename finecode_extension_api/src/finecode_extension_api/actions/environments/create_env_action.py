import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments.create_envs_action import (
    CreateEnvsRunResult,
    EnvInfo,
)


@dataclasses.dataclass
class CreateEnvRunPayload(code_action.RunActionPayload):
    env: EnvInfo
    recreate: bool = False


class CreateEnvRunContext(code_action.RunActionContext[CreateEnvRunPayload]):
    pass


class CreateEnvAction(
    code_action.Action[CreateEnvRunPayload, CreateEnvRunContext, CreateEnvsRunResult]
):
    """Create a single environment(without installing dependencies, only environment)."""

    PAYLOAD_TYPE = CreateEnvRunPayload
    RUN_CONTEXT_TYPE = CreateEnvRunContext
    RESULT_TYPE = CreateEnvsRunResult
