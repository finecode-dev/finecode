import contextlib
import pathlib
import sys
import tempfile
from collections.abc import AsyncGenerator

from fine_envs import dump_config_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri


def get_uv_executable() -> pathlib.Path:
    scripts_dir = pathlib.Path(sys.executable).parent
    if sys.platform == "win32":
        return scripts_dir / "uv.exe"
    return scripts_dir / "uv"


@contextlib.asynccontextmanager
async def temp_project_config_dump(
    project_def_path: pathlib.Path,
    action_runner: iprojectactionrunner.IProjectActionRunner,
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    logger: ilogger.ILogger,
    meta: code_action.RunActionMeta,
) -> AsyncGenerator[pathlib.Path, None]:
    """Dump the resolved config of ``project_def_path`` into a fresh temp dir for uv to run in.

    The dump is machine input to uv, so it is not formatted: formatting would
    dispatch ``format_file``, whose env may be the very one being created or
    installed. The user-facing dump in ``finecode_config_dump/`` is written
    only by the ``dump_config`` action on request.
    """
    with tempfile.TemporaryDirectory(
        prefix="finecode_uv_config_", ignore_cleanup_errors=True
    ) as tmp:
        dump_dir = pathlib.Path(tmp)
        logger.debug(
            f"Dumping config for {project_def_path} to {dump_dir / 'pyproject.toml'}"
        )
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
                target_file_path=path_to_resource_uri(dump_dir / "pyproject.toml"),
                format_output=False,
            ),
            meta=meta,
        )
        yield dump_dir
