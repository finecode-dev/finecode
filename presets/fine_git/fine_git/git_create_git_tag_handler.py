import dataclasses
import pathlib
import shlex

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)

from fine_git.create_git_tag_action import (
    CreateGitTagAction,
    CreateGitTagRunContext,
    CreateGitTagRunPayload,
    CreateGitTagRunResult,
)


@dataclasses.dataclass
class GitCreateGitTagHandlerConfig(code_action.ActionHandlerConfig): ...


class GitCreateGitTagHandler(
    code_action.ActionHandler[CreateGitTagAction, GitCreateGitTagHandlerConfig]
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

    async def _run_git(
        self, cmd: str, cwd: pathlib.Path | None
    ) -> tuple[int | None, str, str]:
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

        return exit_code, stdout, stderr

    async def run(
        self,
        payload: CreateGitTagRunPayload,
        run_context: CreateGitTagRunContext,
    ) -> CreateGitTagRunResult:
        cwd = self.project_info_provider.get_current_project_dir_path()

        try:
            check_cmd = shlex.join(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{payload.tag}"]
            )
            check_exit_code, _, _ = await self._run_git(check_cmd, cwd=cwd)
            tag_exists = check_exit_code == 0

            if tag_exists and not payload.force:
                return CreateGitTagRunResult(tag=payload.tag, created=False, error=None)

            create_args = ["git", "tag"]
            if payload.force:
                create_args.append("-f")
            if payload.message is not None:
                create_args += ["-a", payload.tag, "-m", payload.message]
            else:
                create_args.append(payload.tag)
            if payload.ref is not None:
                create_args.append(payload.ref)

            create_exit_code, _, create_stderr = await self._run_git(
                shlex.join(create_args), cwd=cwd
            )

            if create_exit_code == 0:
                return CreateGitTagRunResult(tag=payload.tag, created=True, error=None)

            return CreateGitTagRunResult(
                tag=payload.tag, created=False, error=create_stderr
            )
        except Exception as exception:
            self.logger.debug(f"Creating tag {payload.tag} raised: {exception}")
            return CreateGitTagRunResult(
                tag=payload.tag, created=False, error=str(exception)
            )
