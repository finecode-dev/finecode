import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    lock_dependencies as lock_dependencies_action,
)
from finecode_extension_api.interfaces import icommandrunner, ilogger


class PipLockDependenciesHandler(
    code_action.ActionHandler[
        lock_dependencies_action.LockDependenciesAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(
        self,
        config: code_action.ActionHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger

    async def run(
        self,
        payload: lock_dependencies_action.LockDependenciesRunPayload,
        run_context: lock_dependencies_action.LockDependenciesRunContext,
    ) -> lock_dependencies_action.LockDependenciesRunResult:
        src_artifact_def_path = payload.src_artifact_def_path
        output_path = payload.output_path
        project_dir_path = src_artifact_def_path.parent

        cmd = (
            f"pip lock"
            f" -o {output_path}"
        )

        process = await self.command_runner.run(cmd, cwd=project_dir_path)
        await process.wait_for_end()

        if process.get_exit_code() != 0:
            error_output = process.get_error_output() or process.get_output()
            raise code_action.ActionFailedException(
                f"pip lock failed for {src_artifact_def_path}:\n{error_output}"
            )

        return lock_dependencies_action.LockDependenciesRunResult(
            lock_file_path=pathlib.Path(output_path),
        )
