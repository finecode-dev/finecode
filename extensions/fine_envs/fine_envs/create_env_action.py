import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

from fine_envs.create_envs_action import (
    CreateEnvsRunResult,
    EnvInfo,
)


@dataclasses.dataclass
class CreateEnvRunPayload(code_action.RunActionPayload):
    env: EnvInfo
    recreate: bool = False
    resolved_project_def_path: ResourceUri | None = None
    """Borrowed, resolved project definition file in ``pyproject.toml`` format.

    The value is null or a file URI naming a borrowed, resolved project
    definition file in ``pyproject.toml`` format, and ``env.project_def_path``
    still names the env's project. A handler that needs a definition reads only
    this file, never the file at ``env.project_def_path``, and never prepares
    one. When the value is null or unusable, such a handler fails that creation
    with one error naming the env and saying which: no definition was supplied,
    or the value. A handler that needs
    no definition ignores the field. Borrowers must not modify or remove the file, its directory or its
    input. The caller keeps the file for the call.
    """


class CreateEnvRunContext(code_action.RunActionContext[CreateEnvRunPayload]):
    pass


class CreateEnvAction(
    code_action.Action[CreateEnvRunPayload, CreateEnvRunContext, CreateEnvsRunResult]
):
    """Create a single environment(without installing dependencies, only environment)."""

    DESCRIPTION = "Create a single environment (without installing dependencies, only environment)."
    PAYLOAD_TYPE = CreateEnvRunPayload
    RUN_CONTEXT_TYPE = CreateEnvRunContext
    RESULT_TYPE = CreateEnvsRunResult
