import dataclasses

from finecode_extension_api import code_action
from fine_envs import install_envs_action
from fine_envs.create_envs_action import EnvInfo
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri


@dataclasses.dataclass
class InstallEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


def _compute_env_overrides(project_raw_config: dict, env_name: str) -> list[str]:
    """Compute the combined dependencies_override list for an env.

    Extension-level overrides are applied first, handler-level overrides last
    (later entries win at apply time by canonical package name).
    """
    finecode_config = project_raw_config.get("tool", {}).get("finecode", {})
    extension_configs = finecode_config.get("extension", {})
    actions_dict = finecode_config.get("action", {})

    handlers_in_env: list[dict] = []
    for action_info in actions_dict.values():
        for handler in action_info.get("handlers", []):
            if handler.get("env") == env_name:
                handlers_in_env.append(handler)

    extension_overrides: list[str] = []
    seen_extensions: set[str] = set()
    for handler in handlers_in_env:
        source = handler.get("source", "")
        ext_name = source.split(".")[0]
        if ext_name and ext_name not in seen_extensions:
            seen_extensions.add(ext_name)
            ext_config = extension_configs.get(ext_name, {})
            extension_overrides.extend(ext_config.get("dependencies_override", []))

    handler_overrides: list[str] = []
    for handler in handlers_in_env:
        handler_overrides.extend(handler.get("dependencies_override", []))

    return extension_overrides + handler_overrides


class InstallEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        install_envs_action.InstallEnvsAction,
        InstallEnvsDiscoverEnvsHandlerConfig,
    ]
):
    """Discover and populate run_context.envs from the current project's config.

    If payload.envs is provided (explicit caller), those envs are
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
        payload: install_envs_action.InstallEnvsRunPayload,
        run_context: install_envs_action.InstallEnvsRunContext,
    ) -> install_envs_action.InstallEnvsRunResult:
        if payload.envs is not None:
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
                    venv_dir_path=path_to_resource_uri(
                        self.runner_info_provider.get_venv_dir_path_of_env(env_name)
                    ),
                    project_def_path=path_to_resource_uri(project_def_path),
                    dependencies_override=_compute_env_overrides(project_raw_config, env_name),
                )
                for env_name in deps_groups
            ]

            if payload.env_names is not None:
                envs = [e for e in envs if e.name in payload.env_names]

        self.logger.debug(f"Discovered handler envs: {[e.name for e in envs]}")
        run_context.envs = envs
        return install_envs_action.InstallEnvsRunResult(errors=[])
