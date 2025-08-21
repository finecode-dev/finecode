import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import dump_config as dump_config_action
from finecode_extension_api.interfaces import ifilemanager


@dataclasses.dataclass
class DumpConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigHandler(
    code_action.ActionHandler[dump_config_action.DumpConfigAction, DumpConfigHandlerConfig]
):
    async def run(
        self, payload: dump_config_action.DumpConfigRunPayload, run_context: dump_config_action.DumpConfigRunContext
    ) -> dump_config_action.DumpConfigRunResult:
        # presets are resolved, remove tool.finecode.presets key to avoid repeating
        # resolving if dump config is processed
        finecode_config = run_context.raw_config_dump.get('tool', {}).get('finecode', {})
        if 'presets' in finecode_config:
            del finecode_config['presets']
            
        # apply changes to dependencies from env configuration to deps groups
        for env_name, env_config in finecode_config.get('env', {}).items():
            if 'dependencies' not in env_config:
                continue
            
            env_deps_group = run_context.raw_config_dump.get('dependency-groups', {}).get(env_name, [])
            dependencies = env_config['dependencies']
            for dep_name, dep_params in dependencies.items():
                # handle 'path'. 'editable' cannot be handled here because dependency
                # specifier doesn't support it. It will read and processed by
                # `install_deps` action
                if 'path' in dep_params:
                    # replace dependency version / source in dependency group to this path
                    try:
                        # check for string because dependency can be also dictionary like '{ "include-group": "runtime"}'
                        dep_idx_in_group = next(idx for idx, dep in enumerate(env_deps_group) if isinstance(dep, str) and get_dependency_name(dep) == dep_name)
                    except StopIteration:
                        continue
                    
                    resolved_path_to_dep = pathlib.Path(dep_params['path'])
                    if not resolved_path_to_dep.is_absolute():
                        # resolve relative to project dir where project def file is
                        resolved_path_to_dep = payload.source_file_path.parent / resolved_path_to_dep
                    new_dep_str_in_group = f"{dep_name} @ file://{resolved_path_to_dep.as_posix()}"
                    env_deps_group[dep_idx_in_group] = new_dep_str_in_group

        return dump_config_action.DumpConfigRunResult(config_dump=run_context.raw_config_dump)


def get_dependency_name(dependency_str: str) -> str:
    # simplified way for now: find the first character which is not allowed in package
    # name
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]

    # dependency can consist also just of package name without version
    return dependency_str
