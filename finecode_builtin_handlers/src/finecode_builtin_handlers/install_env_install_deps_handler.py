import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import (
    install_deps_in_env_action,
    install_env_action,
)
from finecode_extension_api.actions.environments.install_envs_action import (
    InstallEnvsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri, resource_uri_to_path
from finecode_builtin_handlers.dependency_config_utils import (
    collect_transitive_deps,
    process_raw_deps,
)


@dataclasses.dataclass
class InstallEnvInstallDepsHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallEnvInstallDepsHandler(
    code_action.ActionHandler[
        install_env_action.InstallEnvAction,
        InstallEnvInstallDepsHandlerConfig,
    ]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: install_env_action.InstallEnvRunPayload,
        run_context: install_env_action.InstallEnvRunContext,
    ) -> InstallEnvsRunResult:
        env = payload.env
        project_def = run_context.project_def
        if project_def is None:
            raise code_action.ActionFailedException(
                "project_def must be set by InstallEnvReadConfigHandler"
            )

        async with run_context.progress(f"Installing {env.name}") as progress:
            await progress.report("Reading configuration")
            deps_groups = project_def.get("dependency-groups", {})
            env_raw_deps = deps_groups.get(env.name, [])
            env_deps_config = (
                project_def.get("tool", {})
                .get("finecode", {})
                .get("env", {})
                .get(env.name, {})
                .get("dependencies", {})
            )
            project_def_path = resource_uri_to_path(env.project_def_path)
            dependencies: list[dict] = []
            process_raw_deps(
                env_raw_deps,
                env_deps_config,
                dependencies,
                deps_groups,
                project_def_path=project_def_path,
            )
            dependencies.extend(collect_transitive_deps(dependencies))

            install_deps_payload = install_deps_in_env_action.InstallDepsInEnvRunPayload(
                env_name=env.name,
                venv_dir_path=env.venv_dir_path,
                project_dir_path=path_to_resource_uri(project_def_path.parent),
                dependencies=[
                    install_deps_in_env_action.Dependency(
                        name=dep["name"],
                        version_or_source=dep["version_or_source"],
                        editable=dep["editable"],
                    )
                    for dep in dependencies
                ],
            )

            await progress.report("Installing dependencies")
            result = await self.action_runner.run_action(
                action_type=install_deps_in_env_action.InstallDepsInEnvAction,
                payload=install_deps_payload,
                meta=run_context.meta,
            )
            return InstallEnvsRunResult(errors=result.errors)
