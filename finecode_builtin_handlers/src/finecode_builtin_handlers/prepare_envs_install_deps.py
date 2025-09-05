import asyncio
import dataclasses
import itertools
import shutil

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iprojectinfoprovider,
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

        install_deps_tasks: list[asyncio.Task] = []
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

                    task = tg.create_task(
                        self.action_runner.run_action(
                            name="install_deps_in_env",
                            payload={
                                "env_name": env.name,
                                "venv_dir_path": env.venv_dir_path,
                                "project_dir_path": env.project_def_path.parent,
                                "dependencies": dependencies,
                            },
                        )
                    )
                    install_deps_tasks.append(task)
        except ExceptionGroup as eg:
            error_str = ". ".join([str(exception) for exception in eg.exceptions])
            raise code_action.ActionFailedException(error_str)

        install_deps_results = [task.result() for task in install_deps_tasks]
        errors: list[str] = list(
            itertools.chain.from_iterable(
                [result["errors"] for result in install_deps_results]
            )
        )

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
