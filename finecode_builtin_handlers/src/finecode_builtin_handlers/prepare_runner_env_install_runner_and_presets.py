import dataclasses
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    install_deps_in_env as install_deps_in_env_action,
    prepare_runner_env as prepare_runner_env_action,
)
from finecode_extension_api.actions.prepare_runner_envs import PrepareRunnerEnvsRunResult
from finecode_extension_api.interfaces import iactionrunner, ilogger
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareRunnerEnvInstallRunnerAndPresetsHandlerConfig(
    code_action.ActionHandlerConfig
): ...


class PrepareRunnerEnvInstallRunnerAndPresetsHandler(
    code_action.ActionHandler[
        prepare_runner_env_action.PrepareRunnerEnvAction,
        PrepareRunnerEnvInstallRunnerAndPresetsHandlerConfig,
    ]
):
    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_runner_env_action.PrepareRunnerEnvRunPayload,
        run_context: prepare_runner_env_action.PrepareRunnerEnvRunContext,
    ) -> PrepareRunnerEnvsRunResult:
        env = payload.env
        project_def = run_context.project_def
        if project_def is None:
            raise code_action.ActionFailedException(
                "project_def must be populated by previous handlers"
            )

        try:
            dependencies = get_dependencies_in_project_raw_config(
                project_def, env.name
            )
        except FailedToGetDependencies as exception:
            raise code_action.ActionFailedException(
                f"Failed to get dependencies of env {env.name} in {env.project_def_path}: {exception.message} (install_runner_and_presets handler)"
            ) from exception

        install_deps_in_env_action_instance = self.action_runner.get_action_by_name(
            name="install_deps_in_env",
            expected_type=install_deps_in_env_action.InstallDepsInEnvAction,
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

        try:
            result = await self.action_runner.run_action(
                action=install_deps_in_env_action_instance,
                payload=install_deps_payload,
                meta=run_context.meta,
            )
        except iactionrunner.BaseRunActionException as exception:
            return PrepareRunnerEnvsRunResult(errors=[exception.message])

        return PrepareRunnerEnvsRunResult(errors=result.errors)


class FailedToGetDependencies(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def get_dependencies_in_project_raw_config(
    project_raw_config: dict[str, typing.Any], env_name: str
) -> list[dict]:
    # returns dependencies: presets and extension runner
    presets_in_config = (
        project_raw_config.get("tool", {}).get("finecode", {}).get("presets", [])
    )
    presets_packages_names: list[str] = []
    for preset_def in presets_in_config:
        try:
            preset_package = preset_def.get("source")
        except KeyError:
            raise FailedToGetDependencies(f"preset has no source: {preset_def}")
        presets_packages_names.append(preset_package)

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
            f"prepare_runner_envs expects finecode_extension_runner dependency in each environment, but it was not found in {env_name}"
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
