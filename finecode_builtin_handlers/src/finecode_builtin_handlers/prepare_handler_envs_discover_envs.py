import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    prepare_handler_envs as prepare_handler_envs_action,
)
from finecode_extension_api.actions.create_envs import EnvInfo
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class PrepareHandlerEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareHandlerEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        prepare_handler_envs_action.PrepareHandlerEnvsAction,
        PrepareHandlerEnvsDiscoverEnvsHandlerConfig,
    ]
):
    """Discover and populate run_context.envs from the current project's config.

    If payload.envs is already non-empty (explicit caller), those envs are
    used as-is — the caller is responsible for any filtering.
    Otherwise envs are discovered from dependency-groups: every dependency group
    defined in the project definition is included as an env.
    payload.env_names filters the discovered list."""

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.runner_info_provider = runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: prepare_handler_envs_action.PrepareHandlerEnvsRunPayload,
        run_context: prepare_handler_envs_action.PrepareHandlerEnvsRunContext,
    ) -> prepare_handler_envs_action.PrepareHandlerEnvsRunResult:
        if payload.envs:
            envs = list(payload.envs)
        else:
            project_def_path = self.project_info_provider.get_current_project_def_path()
            project_raw_config = (
                await self.project_info_provider.get_current_project_raw_config()
            )
            deps_groups = project_raw_config.get("dependency-groups", {})

            envs = [
                EnvInfo(
                    name=env_name,
                    venv_dir_path=self.runner_info_provider.get_venv_dir_path_of_env(
                        env_name
                    ),
                    project_def_path=project_def_path,
                )
                for env_name in deps_groups
            ]
            
            if payload.env_names is not None:
                envs = [e for e in envs if e.name in payload.env_names]

        self.logger.debug(f"Discovered handler envs: {[e.name for e in envs]}")
        run_context.envs = envs
        return prepare_handler_envs_action.PrepareHandlerEnvsRunResult(errors=[])
