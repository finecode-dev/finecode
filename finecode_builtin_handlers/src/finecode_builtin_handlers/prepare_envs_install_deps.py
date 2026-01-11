import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action, install_deps_in_env as install_deps_in_env_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
)
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareEnvsInstallDepsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareEnvsInstallDepsHandler(
    code_action.ActionHandler[
        prepare_envs_action.PrepareEnvsAction, PrepareEnvsInstallDepsHandlerConfig
    ]
):
    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_envs_action.PrepareEnvsRunPayload,
        run_context: prepare_envs_action.PrepareEnvsRunContext,
    ) -> prepare_envs_action.PrepareEnvsRunResult:
        envs = payload.envs

        install_deps_in_env_action_instance = self.action_runner.get_action_by_name(name="install_deps_in_env")
        install_deps_tasks: list[asyncio.Task[install_deps_in_env_action.InstallDepsInEnvRunResult]] = []
        run_meta = run_context.meta
        try:
            async with asyncio.TaskGroup() as tg:
                for env in envs:
                    project_def = run_context.project_def_by_venv_dir_path[
                        env.venv_dir_path
                    ]

                    # straightforward solution for now
                    deps_groups = project_def.get("dependency-groups", {})
                    env_raw_deps = deps_groups.get(env.name, [])
                    env_deps_config = (
                        project_def.get("tool", {})
                        .get("finecode", {})
                        .get("env", {})
                        .get(env.name, {})
                        .get("dependencies", {})
                    )
                    dependencies = []

                    process_raw_deps(
                        env_raw_deps, env_deps_config, dependencies, deps_groups
                    )
                    
                    install_deps_payload = install_deps_in_env_action.InstallDepsInEnvRunPayload(
                        env_name=env.name,
                        venv_dir_path=env.venv_dir_path,
                        project_dir_path=env.project_def_path.parent,
                        dependencies=[install_deps_in_env_action.Dependency(name=dep['name'], version_or_source=dep['version_or_source'], editable=dep['editable']) for dep in dependencies]
                    )

                    task = tg.create_task(
                        self.action_runner.run_action(
                            action=install_deps_in_env_action_instance,
                            payload=install_deps_payload,
                            meta=run_meta
                        )
                    )
                    install_deps_tasks.append(task)
        except ExceptionGroup as eg:
            error_str = ". ".join([str(exception) for exception in eg.exceptions])
            raise code_action.ActionFailedException(error_str)

        install_deps_results = [task.result() for task in install_deps_tasks]
        errors: list[str] = []
        for result in install_deps_results:
            errors += result.errors

        return prepare_envs_action.PrepareEnvsRunResult(errors=errors)


def process_raw_deps(
    raw_deps: list, env_deps_config, dependencies, deps_groups
) -> None:
    for raw_dep in raw_deps:
        if isinstance(raw_dep, str):
            name = dependency_config_utils.get_dependency_name(raw_dep)
            version_or_source = raw_dep[len(name) :]
            editable = env_deps_config.get(name, {}).get("editable", False)
            dependencies.append(
                {
                    "name": name,
                    "version_or_source": version_or_source,
                    "editable": editable,
                }
            )
        elif isinstance(raw_dep, dict) and "include-group" in raw_dep:
            included_group_deps = deps_groups.get(raw_dep["include-group"], [])
            process_raw_deps(
                included_group_deps, env_deps_config, dependencies, deps_groups
            )
