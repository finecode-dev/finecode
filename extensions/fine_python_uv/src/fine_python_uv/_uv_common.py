import pathlib
import sys

from finecode_extension_api import code_action
from finecode_extension_api.actions.system import dump_config_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner, iprojectinfoprovider
from finecode_extension_api.resource_uri import path_to_resource_uri


def get_uv_executable() -> pathlib.Path:
    scripts_dir = pathlib.Path(sys.executable).parent
    if sys.platform == "win32":
        return scripts_dir / "uv.exe"
    return scripts_dir / "uv"


async def dump_project_config(
    project_def_path: pathlib.Path,
    action_runner: iprojectactionrunner.IProjectActionRunner,
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    logger: ilogger.ILogger,
    meta: code_action.RunActionMeta,
) -> pathlib.Path:
    """Dump resolved project config and return the dump directory path."""
    dump_target_path = project_def_path.parent / "finecode_config_dump" / "pyproject.toml"
    logger.debug(f"Dumping config for {project_def_path} to {dump_target_path}")
    project_raw_config = await project_info_provider.get_project_raw_config(project_def_path)
    await action_runner.run_action(
        action_type=dump_config_action.DumpConfigAction,
        payload=dump_config_action.DumpConfigRunPayload(
            source_file_path=path_to_resource_uri(project_def_path),
            project_raw_config=project_raw_config,
            target_file_path=path_to_resource_uri(dump_target_path),
        ),
        meta=meta,
    )
    return dump_target_path.parent
