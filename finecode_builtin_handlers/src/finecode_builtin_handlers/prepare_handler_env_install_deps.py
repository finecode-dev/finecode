import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    install_deps_in_env as install_deps_in_env_action,
    prepare_handler_env as prepare_handler_env_action,
)
from finecode_extension_api.actions.prepare_handler_envs import (
    PrepareHandlerEnvsRunResult,
)
from finecode_extension_api.interfaces import iactionrunner, ilogger
from finecode_builtin_handlers.dependency_config_utils import process_raw_deps


@dataclasses.dataclass
class PrepareHandlerEnvInstallDepsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareHandlerEnvInstallDepsHandler(
    code_action.ActionHandler[
        prepare_handler_env_action.PrepareHandlerEnvAction,
        PrepareHandlerEnvInstallDepsHandlerConfig,
    ]
):
    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_handler_env_action.PrepareHandlerEnvRunPayload,
        run_context: prepare_handler_env_action.PrepareHandlerEnvRunContext,
    ) -> PrepareHandlerEnvsRunResult:
        env = payload.env
        project_def = run_context.project_def
        if project_def is None:
            raise code_action.ActionFailedException(
                "project_def must be set by PrepareHandlerEnvReadConfigHandler"
            )

        install_deps_in_env_action_instance = self.action_runner.get_action_by_name(
            name="install_deps_in_env",
            expected_type=install_deps_in_env_action.InstallDepsInEnvAction,
        )

        deps_groups = project_def.get("dependency-groups", {})
        env_raw_deps = deps_groups.get(env.name, [])
        env_deps_config = (
            project_def.get("tool", {})
            .get("finecode", {})
            .get("env", {})
            .get(env.name, {})
            .get("dependencies", {})
        )
        dependencies: list[dict] = []
        process_raw_deps(
            env_raw_deps,
            env_deps_config,
            dependencies,
            deps_groups,
            project_def_path=env.project_def_path,
        )

        install_deps_payload = install_deps_in_env_action.InstallDepsInEnvRunPayload(
            env_name=env.name,
            venv_dir_path=env.venv_dir_path,
            project_dir_path=env.project_def_path.parent,
            dependencies=[
                install_deps_in_env_action.Dependency(
                    name=dep["name"],
                    version_or_source=dep["version_or_source"],
                    editable=dep["editable"],
                )
                for dep in dependencies
            ],
        )

        result = await self.action_runner.run_action(
            action=install_deps_in_env_action_instance,
            payload=install_deps_payload,
            meta=run_context.meta,
        )
        return PrepareHandlerEnvsRunResult(errors=result.errors)
