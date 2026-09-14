import dataclasses
import pathlib
import shlex

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_git.get_git_status_action import (
    FileStatus,
    GetGitStatusAction,
    GetGitStatusRunContext,
    GetGitStatusRunPayload,
    GetGitStatusRunResult,
)
from fine_git.git_types import GitChangeKind, change_kind_from_status_char


@dataclasses.dataclass
class GitGetGitStatusHandlerConfig(code_action.ActionHandlerConfig): ...


class GitGetGitStatusHandler(
    code_action.ActionHandler[GetGitStatusAction, GitGetGitStatusHandlerConfig]
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

    def _parse_porcelain_status(
        self, stdout: str, repo_root: pathlib.Path
    ) -> list[FileStatus]:
        records = stdout.split("\0")
        if records and records[-1] == "":
            records = records[:-1]

        changes: list[FileStatus] = []
        index = 0
        while index < len(records):
            record = records[index]
            index_char = record[0]
            worktree_char = record[1]
            rel_path = record[3:]

            original_path: ResourceUri | None = None
            if index_char in ("R", "C"):
                index += 1
                original_rel_path = records[index]
                original_path = path_to_resource_uri(repo_root / original_rel_path)

            changes.append(
                FileStatus(
                    path=path_to_resource_uri(repo_root / rel_path),
                    index_status=change_kind_from_status_char(index_char),
                    worktree_status=change_kind_from_status_char(worktree_char),
                    original_path=original_path,
                )
            )
            index += 1

        return changes

    async def run(
        self,
        payload: GetGitStatusRunPayload,
        run_context: GetGitStatusRunContext,
    ) -> GetGitStatusRunResult:
        cwd = self.project_info_provider.get_current_project_dir_path()

        try:
            toplevel_exit_code, toplevel_stdout, _ = await self._run_git(
                shlex.join(["git", "rev-parse", "--show-toplevel"]), cwd=cwd
            )
            if toplevel_exit_code != 0:
                self.logger.debug(f"{cwd} is not a git repository")
                return GetGitStatusRunResult(repo_root=None, changes=[], error=None)

            repo_root = pathlib.Path(toplevel_stdout.strip())
            repo_root_uri = path_to_resource_uri(repo_root)

            if payload.paths == []:
                return GetGitStatusRunResult(
                    repo_root=repo_root_uri, changes=[], error=None
                )

            # git ties the two together: `--ignored` reports nothing unless
            # untracked reporting is on (`-uno --ignored=matching` is rejected
            # outright, `-uno --ignored=traditional` silently returns an empty
            # answer). The payload documents them as independent, so an
            # "ignored but not untracked" request is asked of git with `-uall`
            # and the untracked entries are dropped from the answer below.
            include_untracked_in_command = (
                payload.include_untracked or payload.include_ignored
            )
            status_args = [
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain=v1",
                "-z",
                "-uall" if include_untracked_in_command else "-uno",
                "--ignored=matching" if payload.include_ignored else "--ignored=no",
            ]
            # A pathspec is always passed: `git status` reports the whole
            # repository regardless of cwd, so `paths=None` (meaning "the whole
            # project directory") needs the project directory named explicitly.
            status_args.append("--")
            if payload.paths is None:
                status_args.append(str(cwd))
            else:
                status_args.extend(
                    str(resource_uri_to_path(uri)) for uri in payload.paths
                )

            status_exit_code, status_stdout, status_stderr = await self._run_git(
                shlex.join(status_args), cwd=cwd
            )
            if status_exit_code != 0:
                return GetGitStatusRunResult(
                    repo_root=repo_root_uri, changes=[], error=status_stderr.strip()
                )

            changes = self._parse_porcelain_status(status_stdout, repo_root)
            if not payload.include_untracked:
                # Only ever non-empty when the command was widened to `-uall`
                # above to make `--ignored` report anything at all.
                changes = [
                    change
                    for change in changes
                    if change.index_status is not GitChangeKind.UNTRACKED
                ]
            self.logger.debug(f"git status found {len(changes)} change(s)")

            return GetGitStatusRunResult(
                repo_root=repo_root_uri, changes=changes, error=None
            )
        except Exception as exception:
            self.logger.debug(f"Getting git status raised: {exception}")
            return GetGitStatusRunResult(
                repo_root=None, changes=[], error=str(exception)
            )
