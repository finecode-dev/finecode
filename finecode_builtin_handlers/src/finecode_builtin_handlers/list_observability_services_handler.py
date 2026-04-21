import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.list_observability_services_action import (
    ListObservabilityServicesAction,
    ListObservabilityServicesRunPayload,
    ListObservabilityServicesRunContext,
    ListObservabilityServicesRunResult,
    ServiceInfo,
)
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)

_WM_SERVICE_DESCRIPTIONS: dict[str, str] = {
    "wm_server": "Workspace Manager server",
    "mcp_server": "MCP server",
    "lsp_server": "LSP server",
    "cli": "CLI",
}


@dataclasses.dataclass
class ListObservabilityServicesHandlerConfig(code_action.ActionHandlerConfig): ...


class ListObservabilityServicesHandler(
    code_action.ActionHandler[
        ListObservabilityServicesAction,
        ListObservabilityServicesHandlerConfig,
    ]
):
    """List services by scanning log directories in the dev_workspace venv and each env's venv."""

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
        payload: ListObservabilityServicesRunPayload,
        run_context: ListObservabilityServicesRunContext,
    ) -> ListObservabilityServicesRunResult:
        services: list[ServiceInfo] = []

        project_name = await self.project_info_provider.get_current_project_package_name()

        # WM-side services: each subdirectory under dev_workspace venv's logs/ is a service
        dev_workspace_venv = self.runner_info_provider.get_venv_dir_path_of_env(
            "dev_workspace"
        )
        logs_dir = dev_workspace_venv / "logs"
        if logs_dir.is_dir():
            for subdir in sorted(logs_dir.iterdir()):
                if subdir.is_dir() and subdir.name != 'runner':
                    local_id = subdir.name
                    description = _WM_SERVICE_DESCRIPTIONS.get(local_id, "")
                    services.append(
                        ServiceInfo(
                            service_id=f"{project_name}/{local_id}",
                            description=description,
                        )
                    )

        # ER services: check each env venv's logs/runner/ directory
        raw_config = await self.project_info_provider.get_current_project_raw_config()
        env_names = list(raw_config.get("dependency-groups", {}).keys())
        for env_name in sorted(env_names):
            if env_name == "dev_workspace":
                continue
            venv_dir = self.runner_info_provider.get_venv_dir_path_of_env(env_name)
            runner_logs_dir = venv_dir / "logs" / "runner"
            if runner_logs_dir.is_dir():
                services.append(
                    ServiceInfo(
                        service_id=f"{project_name}/er:{env_name}",
                        description=f"Extension Runner for environment '{env_name}'",
                    )
                )

        self.logger.debug(f"Found observability services: {[s.service_id for s in services]}")
        return ListObservabilityServicesRunResult(services=services)
