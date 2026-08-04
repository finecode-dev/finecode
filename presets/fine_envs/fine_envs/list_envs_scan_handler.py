import dataclasses

from fine_envs import env_inventory, list_envs_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class ListEnvsScanHandlerConfig(code_action.ActionHandlerConfig): ...


class ListEnvsScanHandler(
    code_action.ActionHandler[
        list_envs_action.ListEnvsAction, ListEnvsScanHandlerConfig
    ]
):
    """Combine the project's declared envs with what exists in `.venvs/`."""

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
        payload: list_envs_action.ListEnvsRunPayload,
        run_context: list_envs_action.ListEnvsRunContext,
    ) -> list_envs_action.ListEnvsRunResult:
        project_raw_config = (
            await self.project_info_provider.get_current_project_raw_config()
        )
        declared_names = list(project_raw_config.get("dependency-groups", {}))

        venvs_dir_path = self.runner_info_provider.get_current_venv_dir_path().parent
        existing = env_inventory.read_existing_envs(venvs_dir_path)

        envs = env_inventory.scan_envs(
            declared_names=declared_names,
            venvs_dir_path=venvs_dir_path,
            existing=existing,
        )

        orphaned = [env.name for env in envs if env.orphaned]
        self.logger.debug(
            f"Scanned {len(envs)} envs in {venvs_dir_path}, orphaned: {orphaned}"
        )

        return list_envs_action.ListEnvsRunResult(envs=envs)
