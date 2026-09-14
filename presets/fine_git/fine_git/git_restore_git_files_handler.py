import dataclasses
import pathlib
import shlex

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_git.restore_git_files_action import (
    GitRestoreTarget,
    RestoreGitFilesAction,
    RestoreGitFilesRunContext,
    RestoreGitFilesRunPayload,
    RestoreGitFilesRunResult,
)


@dataclasses.dataclass
class GitRestoreGitFilesHandlerConfig(code_action.ActionHandlerConfig): ...


def _status_lookup_key(path: pathlib.Path) -> pathlib.Path:
    """Normalize a path to the form `git status` entries are keyed by.

    Status entries are built on `git rev-parse --show-toplevel`, which resolves
    symlinks, while `resource_uri_to_path` does not. A project reached through a
    symlinked path -- a dev-container mount, macOS `/tmp` -- would otherwise miss
    every entry and report each requested path as "no changes to restore".

    Only the parent chain is resolved: git reports a symlinked *file* under its
    own path, not its target, so resolving the last component would reintroduce
    the same mismatch for symlinks the repository actually tracks.
    """
    return path.parent.resolve() / path.name


class GitRestoreGitFilesHandler(
    code_action.ActionHandler[RestoreGitFilesAction, GitRestoreGitFilesHandlerConfig]
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

    def _parse_status_chars(
        self, stdout: str, repo_root: pathlib.Path
    ) -> dict[pathlib.Path, tuple[str, str]]:
        records = stdout.split("\0")
        if records and records[-1] == "":
            records = records[:-1]

        result: dict[pathlib.Path, tuple[str, str]] = {}
        index = 0
        while index < len(records):
            record = records[index]
            x_char = record[0]
            y_char = record[1]
            rel_path = record[3:]
            if x_char in ("R", "C"):
                index += 1  # skip the original-path record

            result[repo_root / rel_path] = (x_char, y_char)
            index += 1

        return result

    async def run(
        self,
        payload: RestoreGitFilesRunPayload,
        run_context: RestoreGitFilesRunContext,
    ) -> RestoreGitFilesRunResult:
        cwd = self.project_info_provider.get_current_project_dir_path()
        restored: list[ResourceUri] = []
        removed: list[ResourceUri] = []
        skipped: dict[ResourceUri, str] = {}

        try:
            toplevel_exit_code, toplevel_stdout, _ = await self._run_git(
                shlex.join(["git", "rev-parse", "--show-toplevel"]), cwd=cwd
            )
            if toplevel_exit_code != 0:
                for uri in payload.paths:
                    skipped[uri] = "not in a git repository"
                return RestoreGitFilesRunResult(
                    restored=[], removed=[], skipped=skipped, error=None
                )

            if payload.paths == []:
                return RestoreGitFilesRunResult(
                    restored=[], removed=[], skipped={}, error=None
                )

            repo_root = pathlib.Path(toplevel_stdout.strip())

            uri_to_abs_path: dict[ResourceUri, pathlib.Path] = {}
            for uri in payload.paths:
                abs_path = resource_uri_to_path(uri)
                if not abs_path.is_relative_to(cwd):
                    skipped[uri] = "outside the project directory"
                    continue
                uri_to_abs_path[uri] = abs_path

            status_map: dict[pathlib.Path, tuple[str, str]] = {}
            if uri_to_abs_path:
                status_args = [
                    "git",
                    "-c",
                    "core.quotepath=false",
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "-uall",
                    "--",
                ]
                status_args.extend(str(path) for path in uri_to_abs_path.values())
                status_exit_code, status_stdout, status_stderr = await self._run_git(
                    shlex.join(status_args), cwd=cwd
                )
                if status_exit_code != 0:
                    # Reporting the failure rather than falling through: an
                    # empty status is indistinguishable from "nothing changed",
                    # so a swallowed probe would have this action report a
                    # clean no-op over a question it never got an answer to.
                    return RestoreGitFilesRunResult(
                        restored=[],
                        removed=[],
                        skipped=skipped,
                        error=status_stderr.strip(),
                    )
                status_map = self._parse_status_chars(status_stdout, repo_root)

            restorable: dict[ResourceUri, pathlib.Path] = {}
            for uri, abs_path in uri_to_abs_path.items():
                entry = status_map.get(_status_lookup_key(abs_path))
                if entry is None:
                    skipped[uri] = "no changes to restore"
                    continue

                index_char, worktree_char = entry
                if index_char == "?" and worktree_char == "?":
                    if not payload.remove_untracked:
                        skipped[uri] = "untracked (remove_untracked is false)"
                    elif abs_path.is_dir():
                        skipped[uri] = "untracked directory"
                    else:
                        # Direct filesystem deletion, bypassing the file-editing
                        # service: this removes an untracked working-tree file,
                        # which IFileEditor has no concept of.
                        abs_path.unlink()
                        removed.append(uri)
                    continue

                restorable[uri] = abs_path

            if restorable:
                restore_args = ["git", "restore", f"--source={payload.source_ref}"]
                if payload.target in (GitRestoreTarget.WORKTREE, GitRestoreTarget.BOTH):
                    restore_args.append("--worktree")
                if payload.target in (GitRestoreTarget.INDEX, GitRestoreTarget.BOTH):
                    restore_args.append("--staged")
                restore_args.append("--")
                restore_args.extend(str(path) for path in restorable.values())

                restore_cmd = shlex.join(restore_args)
                self.logger.debug(f"Restoring files: {restore_cmd}")
                restore_exit_code, _, restore_stderr = await self._run_git(
                    restore_cmd, cwd=cwd
                )

                if restore_exit_code != 0:
                    return RestoreGitFilesRunResult(
                        restored=[],
                        removed=removed,
                        skipped=skipped,
                        error=restore_stderr.strip(),
                    )

                restored = list(restorable.keys())

            self.logger.debug(
                f"git restore: restored={len(restored)} removed={len(removed)} "
                f"skipped={len(skipped)}"
            )
            return RestoreGitFilesRunResult(
                restored=restored, removed=removed, skipped=skipped, error=None
            )
        except Exception as exception:
            self.logger.debug(f"Restoring git files raised: {exception}")
            return RestoreGitFilesRunResult(
                restored=[], removed=[], skipped={}, error=str(exception)
            )
