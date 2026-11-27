import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_envs import dump_config_action

# The dump is a project definition in pyproject.toml format; tools that discover
# the definition from their working directory (uv) find it only under this name.
DUMP_FILE_NAME = "pyproject.toml"


async def write_project_config_dump(
    *,
    project_def_path: pathlib.Path,
    target_file: pathlib.Path,
    action_runner: iprojectactionrunner.IProjectActionRunner,
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    logger: ilogger.ILogger,
    meta: code_action.RunActionMeta,
) -> None:
    """Render the resolved config of ``project_def_path`` into ``target_file``.

    Makes exactly one ``get_project_raw_config`` fetch and one configured
    ``dump_config`` dispatch writing ``target_file`` with
    ``format_output=False`` (machine input to tools, never formatted). The
    caller owns ``target_file`` and its directory: this function neither
    creates nor removes them and does not catch exceptions.
    """
    logger.debug(f"Dumping config for {project_def_path} to {target_file}")
    project_raw_config = await project_info_provider.get_project_raw_config(
        project_def_path
    )
    await action_runner.run_action(
        action_type=iprojectactionrunner.ActionRef.from_type(
            dump_config_action.DumpConfigAction
        ),
        payload=dump_config_action.DumpConfigRunPayload(
            source_file_path=path_to_resource_uri(project_def_path),
            project_raw_config=project_raw_config,
            target_file_path=path_to_resource_uri(target_file),
            format_output=False,
        ),
        meta=meta,
    )
