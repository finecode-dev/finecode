from pathlib import Path
from typing import Any, NamedTuple

import ordered_set
from loguru import logger
from tomlkit import loads as toml_loads

from finecode.workspace_manager import context, domain, user_messages
from finecode.workspace_manager.config import config_models
from finecode.workspace_manager.runner import runner_client, runner_info


async def read_projects_in_dir(
    dir_path: Path, ws_context: context.WorkspaceContext
) -> list[domain.Project]:
    # Find all projects in directory
    # `dir_path` expected to be absolute path
    logger.trace(f"Read directories in {dir_path}")
    new_projects: list[domain.Project] = []
    def_files_generator = dir_path.rglob("pyproject.toml")
    for def_file in def_files_generator:
        # ignore definition files in `__testdata__` directory, projects in test data
        # can be started only in tests, not from outside
        # TODO: make configurable?
        # path to definition file relative to workspace directory in which this
        # definition was found
        def_file_rel_dir_path = def_file.relative_to(dir_path)
        if '__testdata__' in def_file_rel_dir_path.parts:
            logger.debug(f"Skip '{def_file}' because it is in test data and it is not a test session")
            continue
        if def_file.parent.name == 'finecode_config_dump':
            logger.debug(f"Skip '{def_file}' because it is config dump, not real project config")
            continue

        status = domain.ProjectStatus.CONFIG_VALID
        actions: list[domain.Action] | None = None

        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read()).value

        if project_def.get("tool", {}).get("finecode", None) is None:
            status = domain.ProjectStatus.NO_FINECODE
            actions = []

        new_project = domain.Project(
            name=def_file.parent.name,
            dir_path=def_file.parent,
            def_path=def_file,
            status=status,
            actions=actions,
        )
        ws_context.ws_projects[def_file.parent] = new_project
        new_projects.append(new_project)
    return new_projects


async def read_project_config(
    project: domain.Project, ws_context: context.WorkspaceContext, resolve_presets: bool = True
) -> None:
    # this function requires running project extension runner to get configuration
    # from it
    if project.def_path.name == "pyproject.toml":
        with open(project.def_path, "rb") as pyproject_file:
            # TODO: handle error if toml is invalid
            project_def = toml_loads(pyproject_file.read()).value
        # TODO: validate that finecode is installed?

        finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
        if finecode_raw_config and resolve_presets:
            finecode_config = config_models.FinecodeConfig(**finecode_raw_config)
            # all presets expected to be in `dev_no_runtime` environment
            project_runners = ws_context.ws_projects_extension_runners[project.dir_path]
            # TODO: can it be the case that there is no such runner?
            dev_no_runtime_runner = project_runners['dev_no_runtime']
            new_config = await collect_config_from_py_presets(
                presets_sources=[preset.source for preset in finecode_config.presets],
                def_path=project.def_path,
                runner=dev_no_runtime_runner,
            )
            _merge_projects_configs(project_def, new_config)

        # add builtins if they are not overwritten
        prepare_envs_action = domain.Action(
            name='prepare_envs',
            source='finecode_extension_api.actions.prepare_envs.PrepareEnvsAction',
            handlers=[
                domain.ActionHandler(name='prepare_envs_venvs', source='fine_python_virtualenv.VirtualenvPrepareEnvHandler', config={}, env='dev_workspace', dependencies=['fine_python_virtualenv==0.1.*']),
                domain.ActionHandler(name='prepare_envs_dump_configs', source='finecode.extension_runner.action_handlers.PrepareEnvsDumpConfigsHandler', config={}, env='dev_workspace', dependencies=[]),
                domain.ActionHandler(name='prepare_envs_pip', source='fine_python_pip.PipPrepareEnvHandler', config={}, env='dev_workspace', dependencies=['fine_python_pip==0.1.*'])
            ],
            config={}
        )
        add_action_to_config_if_new(project_def, prepare_envs_action)
        
        # preparing dev workspaces doesn't need dumping config for two reasons:
        # - depedencies in `dev_workspace` are expected to be simple and installable
        #   without dump
        # - dumping is modifiable as action, so it can be correctly done only in
        #   dev_workspace env of the project and we just create it here, it doesn't
        #   exist yet
        prepare_dev_workspaces_envs_action = domain.Action(
            name='prepare_dev_workspaces_envs',
            source='finecode_extension_api.actions.prepare_envs.PrepareEnvsAction',
            handlers=[
                domain.ActionHandler(name='prepare_venvs', source='fine_python_virtualenv.VirtualenvPrepareEnvHandler', config={}, env='dev_workspace', dependencies=['fine_python_virtualenv==0.1.*']),
                domain.ActionHandler(name='prepare_venvs_pip', source='fine_python_pip.PipPrepareEnvHandler', config={}, env='dev_workspace', dependencies=['fine_python_pip==0.1.*'])
            ],
            config={}
        )
        add_action_to_config_if_new(project_def, prepare_dev_workspaces_envs_action)

        dump_config_action = domain.Action(
            name='dump_config',
            source='finecode_extension_api.actions.dump_config.DumpConfigAction',
            handlers=[
                domain.ActionHandler(name='dump_config', source='finecode.extension_runner.action_handlers.DumpConfigHandler', config={}, env='dev_workspace', dependencies=[]),
                domain.ActionHandler(name='dump_config_save', source='finecode.extension_runner.action_handlers.DumpConfigSaveHandler', config={}, env='dev_workspace', dependencies=[])
            ],
            config={}
        )
        add_action_to_config_if_new(project_def, dump_config_action)
        
        # add runtime dependency group if it's not explicitly declared
        add_runtime_dependency_group_if_new(project_def)
        
        merge_handlers_dependencies_into_groups(project_def)

        ws_context.ws_projects_raw_configs[project.dir_path] = project_def
    else:
        logger.info(
            f"Project definition of type {project.def_path.name} is not supported yet"
        )


class PresetToProcess(NamedTuple):
    source: str
    project_def_path: Path


async def get_preset_project_path(
    preset: PresetToProcess, def_path: Path, runner: runner_info.ExtensionRunnerInfo
) -> Path | None:
    logger.trace(f"Get preset project path: {preset.source}")

    try:
        resolve_path_result = await runner_client.resolve_package_path(
            runner, preset.source
        )
    except runner_client.BaseRunnerRequestException as error:
        await user_messages.error(f"Failed to get preset project path: {error.message}")
        return None
    try:
        preset_project_path = Path(resolve_path_result["packagePath"])
    except KeyError:
        raise ValueError(f"Preset source cannot be resolved: {preset.source}")

    logger.trace(f"Got: {preset.source} -> {preset_project_path}")
    return preset_project_path


def read_preset_config(
    config_path: Path, preset_id: str
) -> tuple[dict[str, Any] | None, config_models.PresetDefinition | None]:
    # preset_id is used only for logs to make them more useful
    logger.trace(f"Read preset config: {preset_id}")
    if not config_path.exists():
        logger.error(f"preset.toml not found in project '{preset_id}'")
        return (None, None)

    with open(config_path, "rb") as preset_toml_file:
        preset_toml = toml_loads(preset_toml_file.read()).value

    try:
        presets = preset_toml["tool"]["finecode"]["presets"]
    except KeyError:
        presets = []

    preset_config = config_models.PresetDefinition(extends=presets)

    logger.trace(f"Reading preset config finished: {preset_id}")
    return (preset_toml, preset_config)


async def collect_config_from_py_presets(
    presets_sources: list[str], def_path: Path, runner: runner_info.ExtensionRunnerInfo
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    processed_presets: set[str] = set()
    presets_to_process: set[PresetToProcess] = set(
        [
            PresetToProcess(source=preset_source, project_def_path=def_path)
            for preset_source in presets_sources
        ]
    )
    while len(presets_to_process) > 0:
        preset = presets_to_process.pop()
        processed_presets.add(preset.source)

        preset_project_path = await get_preset_project_path(
            preset=preset, def_path=def_path, runner=runner
        )
        if preset_project_path is None:
            logger.trace(f"Path of preset {preset.source} not found")
            continue

        preset_toml_path = preset_project_path / "preset.toml"
        preset_toml, preset_config = read_preset_config(preset_toml_path, preset.source)
        if preset_toml is None or preset_config is None:
            continue

        _merge_preset_configs(config, preset_toml)
        new_presets_sources = (
            set([extend.source for extend in preset_config.extends]) - processed_presets
        )
        for new_preset_source in new_presets_sources:
            presets_to_process.add(
                PresetToProcess(
                    source=new_preset_source,
                    project_def_path=def_path,
                )
            )

    return config


def _merge_projects_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 without overwriting
    if "tool" not in config1:
        config1["tool"] = {}
    if "finecode" not in config1["tool"]:
        config1["tool"]["finecode"] = {}

    tool_finecode_config1 = config1["tool"]["finecode"]
    tool_finecode_config2 = config2.get("tool", {}).get("finecode", {})

    for key, value in tool_finecode_config2.items():
        if key == "action" or key == "action_handler":
            # first process actions explicitly to merge correct configs
            assert isinstance(value, dict)
            if key not in tool_finecode_config1:
                tool_finecode_config1[key] = {}
            for action_name, action_info in value.items():
                if action_name not in tool_finecode_config1[key]:
                    # new action, just add as it is
                    tool_finecode_config1[key][action_name] = action_info
                else:
                    # action with the same name, merge
                    if "config" in action_info:
                        if "config" not in tool_finecode_config1[key][action_name]:
                            tool_finecode_config1[key][action_name]["config"] = {}

                        action_config = tool_finecode_config1[key][action_name][
                            "config"
                        ]
                        action_config.update(action_info["config"])
        elif key in config1:
            tool_finecode_config1[key].update(value)
        else:
            tool_finecode_config1[key] = value


def _merge_preset_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 (in-place)
    # config1 is not overwritten by config2
    new_views = config2.get("tool", {}).get("finecode", {}).get("views", None)
    new_actions_defs_and_configs = (
        config2.get("tool", {}).get("finecode", {}).get("action", None)
    )
    new_actions_handlers_configs = (
        config2.get("tool", {}).get("finecode", {}).get("action_handler", None)
    )
    if (
        new_views is not None
        or new_actions_defs_and_configs is not None
        or new_actions_handlers_configs is not None
    ):
        if "tool" not in config1:
            config1["tool"] = {}
        if "finecode" not in config1["tool"]:
            config1["tool"]["finecode"] = {}

        if new_views is not None:
            if "views" not in config1["tool"]["finecode"]:
                config1["tool"]["finecode"]["views"] = []
            config1["tool"]["finecode"]["views"].extend(new_views)
            del config2["tool"]["finecode"]["views"]

        if new_actions_defs_and_configs is not None:
            if "action" not in config1["tool"]["finecode"]:
                config1["tool"]["finecode"]["action"] = {}

            for handler_name, handler_info in new_actions_defs_and_configs.items():
                if handler_name not in config1["tool"]["finecode"]["action"]:
                    config1["tool"]["finecode"]["action"][handler_name] = {}

                action_def = {
                    key: value for key, value in handler_info.items() if key != "config"
                }
                config1["tool"]["finecode"]["action"][handler_name].update(action_def)

                try:
                    handler_config = handler_info["config"]
                except KeyError:
                    continue

                handler_config.update(
                    config1["tool"]["finecode"]["action"][handler_name].get(
                        "config", {}
                    )
                )
                config1["tool"]["finecode"]["action"][handler_name][
                    "config"
                ] = handler_config

            del config2["tool"]["finecode"]["action"]

    if new_actions_handlers_configs is not None:
        if "action_handler" not in config1["tool"]["finecode"]:
            config1["tool"]["finecode"]["action_handler"] = {}

        for handler_name, handler_info in new_actions_handlers_configs.items():
            if handler_name not in config1["tool"]["finecode"]["action_handler"]:
                config1["tool"]["finecode"]["action_handler"][handler_name] = {}

            try:
                handler_config = handler_info["config"]
            except KeyError:
                continue

            handler_config.update(
                config1["tool"]["finecode"]["action_handler"][handler_name].get(
                    "config", {}
                )
            )
            config1["tool"]["finecode"]["action_handler"][handler_name][
                "config"
            ] = handler_config

        del config2["tool"]["finecode"]["action_handler"]

    del config2["tool"]["finecode"]


def add_action_to_config_if_new(raw_config: dict[str, Any], action: domain.Action) -> None:
    # adds action to raw config if it is not defined yet. Existing action will be not
    # overwritten
    tool_config = add_or_get_dict_key_value(raw_config, 'tool', {})
    finecode_config = add_or_get_dict_key_value(tool_config, 'finecode', {})
    action_config = add_or_get_dict_key_value(finecode_config, 'action', {})
    if action.name not in action_config:
        action_raw_dict = {
            "source": action.source,
            "handlers": [handler_to_dict(handler) for handler in action.handlers]
        }
        action_config[action.name] = action_raw_dict

    # example of action definition:
    # [tool.finecode.action.text_document_inlay_hint]
    # source = "finecode_extension_api.actions.ide.text_document_inlay_hint.TextDocumentInlayHintAction"
    # handlers = [
    #     { name = 'module_exports_inlay_hint', source = 'fine_python_module_exports.extension.get_document_inlay_hints', env = "dev_no_runtime", dependencies = [
    #         "fine_python_module_exports @ git+https://github.com/finecode-dev/finecode.git#subdirectory=extensions/fine_python_module_exports",
    #     ] },
    # ]


def add_or_get_dict_key_value(dict_obj: dict[str, Any], key: str, default_value: Any) -> Any:
    if key not in dict_obj:
        value = default_value
        dict_obj[key] = value
    else:
        value = dict_obj[key]
    
    return value


def handler_to_dict(handler: domain.ActionHandler) -> dict[str, str | list[str]]:
    return {
        "name": handler.name,
        "source": handler.source,
        "env": handler.env,
        "dependencies": handler.dependencies
    }


def add_runtime_dependency_group_if_new(project_config: dict[str, Any]) -> None:
    runtime_dependencies = project_config.get('project', {}).get('dependencies', [])
    
    deps_groups = add_or_get_dict_key_value(project_config, 'dependency-groups', {})
    if 'runtime' not in deps_groups:
        deps_groups['runtime'] = runtime_dependencies


def merge_handlers_dependencies_into_groups(project_config: dict[str, Any]) -> None:
    # tool.finecode.action.<action_name>.handlers[x].dependencies
    actions_dict = project_config.get('tool', {}).get('finecode', {}).get('action', {})
    if 'dependency-groups' not in project_config:
        project_config['dependency-groups'] = {}
    deps_groups = project_config['dependency-groups']
    
    for action_info in actions_dict.values():
        action_handlers = action_info.get('handlers', [])
        
        for handler in action_handlers:
            handler_env = handler.get('env', None)
            if handler_env is None:
                logger.warning(f'Handler {handler} has no env, skip it')
                continue
            deps = handler.get('dependencies', [])
            
            if handler_env not in deps_groups:
                deps_groups[handler_env] = []
            
            env_deps = deps_groups[handler_env]
            # should we remove duplicates here?
            env_deps += deps

    # remove duplicates in dependencies because multiple handlers can have the same
    # dependencies / be from the same package
    for group_name in deps_groups.keys():
        deps_list = deps_groups[group_name]
        deps_set = ordered_set.OrderedSet(deps_list)
        new_deps_list = list(deps_set)
        deps_groups[group_name] = new_deps_list
