import dataclasses
import shlex

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ilogger, iprojectinfoprovider

from fine_git.push_git_refs_action import (
    PushGitRefsAction,
    PushGitRefsRunContext,
    PushGitRefsRunPayload,
    PushGitRefsRunResult,
)


@dataclasses.dataclass
class GitPushGitRefsHandlerConfig(code_action.ActionHandlerConfig): ...


class GitPushGitRefsHandler(
    code_action.ActionHandler[PushGitRefsAction, GitPushGitRefsHandlerConfig]
):
    def __init__(
        self,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: PushGitRefsRunPayload,
        run_context: PushGitRefsRunContext,
    ) -> PushGitRefsRunResult:
        cwd = self.project_info_provider.get_current_project_dir_path()

        try:
            args = ["git", "push"]
            if payload.force:
                args.append("--force")
            args.append(payload.remote)
            args += payload.refs

            cmd = shlex.join(args)
            process = await self.command_runner.run(cmd, cwd=cwd)
            await process.wait_for_end()

            exit_code = process.get_exit_code()
            stdout = process.get_output()
            stderr = process.get_error_output()

            self.logger.debug(f"{cmd!r} exit code: {exit_code}")
            if stdout:
                self.logger.debug(f"{cmd!r} stdout:\n{stdout}")
            if stderr:
                self.logger.debug(f"{cmd!r} stderr:\n{stderr}")

            if exit_code == 0:
                return PushGitRefsRunResult(pushed_refs=payload.refs, error=None)

            return PushGitRefsRunResult(pushed_refs=[], error=stderr)
        except Exception as exception:
            self.logger.debug(f"Pushing refs {payload.refs} raised: {exception}")
            return PushGitRefsRunResult(pushed_refs=[], error=str(exception))
