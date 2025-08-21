import asyncio
import dataclasses
import itertools
import shutil

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_runners as prepare_runners_action
from finecode_extension_api.interfaces import iactionrunner, iprojectinfoprovider, ilogger
from .dump_config import get_dependency_name
from .prepare_envs_install_deps import raw_dep_to_dep_dict


@dataclasses.dataclass
class PrepareRunnersInstallRunnerAndPresetsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnersInstallRunnerAndPresetsHandler(
    code_action.ActionHandler[prepare_runners_action.PrepareRunnersAction, PrepareRunnersInstallRunnerAndPresetsHandlerConfig]
):
    def __init__(self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger) -> None:
        self.action_runner = action_runner
        self.logger = logger
    
    async def run(
        self, payload: prepare_runners_action.PrepareRunnersRunPayload, run_context: prepare_runners_action.PrepareRunnersRunContext
    ) -> prepare_runners_action.PrepareRunnersRunResult:
        # find finecode_extension_runner in deps
        # find presets in config and their version in deps
        # install all these packages
        envs = payload.envs

        install_deps_tasks: list[asyncio.Task] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for env in envs:
                    project_def = run_context.project_def_by_venv_dir_path[env.venv_dir_path]
                    
                    presets_in_config = project_def.get('tool', {}).get('finecode', {}).get('presets', [])
                    presets_packages_names: list[str] = []
                    for preset_def in presets_in_config:
                        try:
                            preset_package = preset_def.get('source')
                        except KeyError:
                            # workspace manager validates configuration and source should
                            # always exist, but still handle
                            raise code_action.ActionFailedException(f"preset has no source: {preset_def} in {run_context.project_def_path_by_venv_dir_path[env.venv_dir_path]}")
                        presets_packages_names.append(preset_package)

                    # straightforward solution for now
                    deps_groups = project_def.get('dependency-groups', {})
                    env_raw_deps = deps_groups.get(env.name, [])
                    env_deps_config = project_def.get('tool', {}).get('finecode', {}).get('env', {}).get(env.name, {}).get('dependencies', {})
                    dependencies = []
                    
                    try:
                        runner_dep = next(dep for dep in env_raw_deps if isinstance(dep, str) and get_dependency_name(dep) == 'finecode_extension_runner')
                    except StopIteration:
                        raise code_action.ActionFailedException(f"prepare_runners expects finecode_extension_runner dependency in each environment, but it was not found in {env.name} (install_runner_and_presets handler)")
                    
                    runner_dep_dict = raw_dep_to_dep_dict(raw_dep=runner_dep, env_deps_config=env_deps_config)
                    dependencies.append(runner_dep_dict)
                    
                    for preset_package in presets_packages_names:
                        try:
                            preset_dep = next(dep for dep in env_raw_deps if isinstance(dep, str) and get_dependency_name(dep) == preset_package)
                        except StopIteration:
                            if env.name == 'dev_no_runtime':
                                # all preset packages must be in 'dev_no_runtime' env
                                raise code_action.ActionFailedException(f"'{preset_package}' is used as preset source, but not declared in 'dev_no_runtime' dependency group")
                            else:
                                continue
                        
                        preset_dep_dict = raw_dep_to_dep_dict(raw_dep=preset_dep, env_deps_config=env_deps_config)
                        dependencies.append(preset_dep_dict)

                    task = tg.create_task(self.action_runner.run_action(name='install_deps_in_env', payload={
                        "env_name": env.name,
                        "venv_dir_path": env.venv_dir_path,
                        "project_dir_path": env.project_def_path.parent,
                        "dependencies": dependencies
                    }))
                    install_deps_tasks.append(task)
        except ExceptionGroup as eg:
            error_str = '. '.join([str(exception) for exception in eg.exceptions])
            raise code_action.ActionFailedException(error_str)

        install_deps_results = [task.result() for task in install_deps_tasks]
        errors: list[str] = list(itertools.chain.from_iterable([result['errors'] for result in install_deps_results]))

        return prepare_runners_action.PrepareRunnersRunResult(errors=errors)
