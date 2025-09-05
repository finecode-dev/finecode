import asyncio
import dataclasses
import itertools
import shutil
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_runners as prepare_runners_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareRunnersInstallRunnerAndPresetsHandlerConfig(
    code_action.ActionHandlerConfig
): ...


class PrepareRunnersInstallRunnerAndPresetsHandler(
    code_action.ActionHandler[
        prepare_runners_action.PrepareRunnersAction,
        PrepareRunnersInstallRunnerAndPresetsHandlerConfig,
    ]
):
    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_runners_action.PrepareRunnersRunPayload,
        run_context: prepare_runners_action.PrepareRunnersRunContext,
    ) -> prepare_runners_action.PrepareRunnersRunResult:
        # find finecode_extension_runner in deps
        # find presets in config and their version in deps
        # install all these packages
        envs = payload.envs

        dependencies_by_env: dict[str, list[dict]] = {}
        for env in envs:
            project_def = run_context.project_def_by_venv_dir_path[env.venv_dir_path]
            project_def_path = run_context.project_def_path_by_venv_dir_path[
                env.venv_dir_path
            ]
            try:
                dependencies = get_dependencies_in_project_raw_config(
                    project_def, env.name
                )
            except FailedToGetDependencies as exception:
                raise code_action.ActionFailedException(
                    f"Failed to get dependencies of env {env.name} in {project_def_path}: {exception.message} (install_runner_and_presets handler)"
                )
            dependencies_by_env[env.name] = dependencies

        install_deps_tasks: list[asyncio.Task] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for env in envs:
                    task = tg.create_task(
                        self.action_runner.run_action(
                            name="install_deps_in_env",
                            payload={
                                "env_name": env.name,
                                "venv_dir_path": env.venv_dir_path,
                                "project_dir_path": env.project_def_path.parent,
                                "dependencies": dependencies_by_env[env.name],
                            },
                        )
                    )
                    install_deps_tasks.append(task)
        except ExceptionGroup as eg:
            errors: list[str] = []
            for exception in eg.exceptions:
                if isinstance(exception, iactionrunner.BaseRunActionException):
                    errors.append(exception.message)
                else:
                    # unexpected exception
                    error_str = ". ".join(
                        [str(exception) for exception in eg.exceptions]
                    )
                    raise code_action.ActionFailedException(error_str)

            result = prepare_runners_action.PrepareRunnersRunResult(errors=errors)
            raise code_action.StopActionRunWithResult(result=result)

        install_deps_results = [task.result() for task in install_deps_tasks]
        errors: list[str] = list(
            itertools.chain.from_iterable(
                [result["errors"] for result in install_deps_results]
            )
        )
        result = prepare_runners_action.PrepareRunnersRunResult(errors=errors)

        return result


class FailedToGetDependencies(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def get_dependencies_in_project_raw_config(
    project_raw_config: dict[str, typing.Any], env_name: str
):
    # returns dependencies: presets and extension runner
    presets_in_config = (
        project_raw_config.get("tool", {}).get("finecode", {}).get("presets", [])
    )
    presets_packages_names: list[str] = []
    for preset_def in presets_in_config:
        try:
            preset_package = preset_def.get("source")
        except KeyError:
            # workspace manager validates configuration and source should
            # always exist, but still handle
            raise FailedToGetDependencies(f"preset has no source: {preset_def}")
        presets_packages_names.append(preset_package)

    # straightforward solution for now
    deps_groups = project_raw_config.get("dependency-groups", {})
    env_raw_deps = deps_groups.get(env_name, [])
    env_deps_config = (
        project_raw_config.get("tool", {})
        .get("finecode", {})
        .get("env", {})
        .get(env_name, {})
        .get("dependencies", {})
    )
    dependencies = []

    try:
        runner_dep = next(
            dep
            for dep in env_raw_deps
            if isinstance(dep, str)
            and dependency_config_utils.get_dependency_name(dep)
            == "finecode_extension_runner"
        )
    except StopIteration:
        raise FailedToGetDependencies(
            f"prepare_runners expects finecode_extension_runner dependency in each environment, but it was not found in {env_name}"
        )

    runner_dep_dict = dependency_config_utils.raw_dep_to_dep_dict(
        raw_dep=runner_dep, env_deps_config=env_deps_config
    )
    dependencies.append(runner_dep_dict)

    for preset_package in presets_packages_names:
        try:
            preset_dep = next(
                dep
                for dep in env_raw_deps
                if isinstance(dep, str)
                and dependency_config_utils.get_dependency_name(dep) == preset_package
            )
        except StopIteration:
            if env_name == "dev_workspace":
                # all preset packages must be in 'dev_workspace' env
                raise FailedToGetDependencies(
                    f"'{preset_package}' is used as preset source, but not declared in 'dev_workspace' dependency group"
                )
            else:
                continue

        preset_dep_dict = dependency_config_utils.raw_dep_to_dep_dict(
            raw_dep=preset_dep, env_deps_config=env_deps_config
        )
        dependencies.append(preset_dep_dict)
    return dependencies
