import dataclasses
import pathlib
import re
import shlex

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_git.get_git_diff_action import (
    FileDiff,
    GetGitDiffAction,
    GetGitDiffRunContext,
    GetGitDiffRunPayload,
    GetGitDiffRunResult,
    GitDiffSource,
)
from fine_git.git_types import GitChangeKind

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
_BINARY_FILES_RE = re.compile(r"^Binary files .* differ$")


@dataclasses.dataclass
class GitGetGitDiffHandlerConfig(code_action.ActionHandlerConfig): ...


def _strip_diff_path_prefix(line: str, prefix: str) -> str | None:
    rest = line[4:].rstrip("\n")
    if rest == "/dev/null":
        return None
    if rest.startswith(prefix):
        return rest[len(prefix) :]
    return rest


class GitGetGitDiffHandler(
    code_action.ActionHandler[GetGitDiffAction, GitGetGitDiffHandlerConfig]
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

    def _split_sections(self, stdout: str) -> list[str]:
        sections: list[str] = []
        current: list[str] = []
        for line in stdout.splitlines(keepends=True):
            if line.startswith("diff --git ") and current:
                sections.append("".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("".join(current))
        return sections

    def _parse_change_kind(self, lines: list[str]) -> GitChangeKind:
        has_rename_from = any(line.startswith("rename from ") for line in lines)
        has_rename_to = any(line.startswith("rename to ") for line in lines)
        has_copy_from = any(line.startswith("copy from ") for line in lines)
        has_copy_to = any(line.startswith("copy to ") for line in lines)

        if any(line.startswith("new file mode") for line in lines):
            return GitChangeKind.ADDED
        if any(line.startswith("deleted file mode") for line in lines):
            return GitChangeKind.DELETED
        if has_rename_from and has_rename_to:
            return GitChangeKind.RENAMED
        if has_copy_from and has_copy_to:
            return GitChangeKind.COPIED
        return GitChangeKind.MODIFIED

    def _parse_paths(self, lines: list[str]) -> tuple[str | None, str | None]:
        dash_line = next((line for line in lines if line.startswith("--- ")), None)
        plus_line = next((line for line in lines if line.startswith("+++ ")), None)

        if dash_line is not None or plus_line is not None:
            old_path = (
                _strip_diff_path_prefix(dash_line, "a/")
                if dash_line is not None
                else None
            )
            new_path = (
                _strip_diff_path_prefix(plus_line, "b/")
                if plus_line is not None
                else None
            )
            return old_path, new_path

        # Binary diffs carry no ---/+++ lines; fall back to the diff --git header.
        match = _DIFF_GIT_HEADER_RE.match(lines[0].rstrip("\n")) if lines else None
        if match is not None:
            return match.group(1), match.group(2)
        return None, None

    def _parse_added_removed_lines(
        self, lines: list[str]
    ) -> tuple[list[str], list[str]]:
        # `in_hunk` is the only gate needed, and it has to be the only one: the
        # `---`/`+++` file headers sit before the first `@@`, and a section is
        # one file (`_split_sections`), so nothing else can reach here. Excluding
        # `+++`/`---` prefixes as well would drop real content -- a removed `---`
        # arrives as `----`, an added `+++more` as `++++more` -- and silently
        # disagree with `patch`, which is the same answer in another projection.
        #
        # Single-column prefixes only. Combined diffs (`diff --cc`, from a merge)
        # carry two columns and would misparse; the commands this handler builds
        # never produce them.
        added_lines: list[str] = []
        removed_lines: list[str] = []
        in_hunk = False
        for line in lines:
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("+"):
                added_lines.append(line[1:].rstrip("\n"))
            elif line.startswith("-"):
                removed_lines.append(line[1:].rstrip("\n"))
        return added_lines, removed_lines

    def _parse_file_diff(
        self, section: str, repo_root: pathlib.Path
    ) -> FileDiff | None:
        """Parse one file's section, or `None` if it names no path.

        `None` rather than raising: a section whose header this parser does not
        understand is one file's worth of missing answer, and reporting the
        other files beats failing the whole diff over it. The caller logs it, so
        it stays diagnosable rather than merely quiet.
        """
        lines = section.splitlines()

        change_kind = self._parse_change_kind(lines)
        old_path, new_path = self._parse_paths(lines)
        is_binary = any(
            _BINARY_FILES_RE.match(line) or line.startswith("GIT binary patch")
            for line in lines
        )

        if change_kind == GitChangeKind.DELETED:
            path_rel = old_path
        elif change_kind in (GitChangeKind.RENAMED, GitChangeKind.COPIED):
            path_rel = new_path
        else:
            path_rel = new_path if new_path is not None else old_path

        original_path_rel = (
            old_path
            if change_kind in (GitChangeKind.RENAMED, GitChangeKind.COPIED)
            else None
        )

        added_lines: list[str] = []
        removed_lines: list[str] = []
        if not is_binary:
            added_lines, removed_lines = self._parse_added_removed_lines(lines)

        if path_rel is None:
            return None

        return FileDiff(
            path=path_to_resource_uri(repo_root / path_rel),
            change_kind=change_kind,
            patch=section,
            added_lines=added_lines,
            removed_lines=removed_lines,
            original_path=(
                path_to_resource_uri(repo_root / original_path_rel)
                if original_path_rel is not None
                else None
            ),
            is_binary=is_binary,
        )

    async def run(
        self,
        payload: GetGitDiffRunPayload,
        run_context: GetGitDiffRunContext,
    ) -> GetGitDiffRunResult:
        cwd = self.project_info_provider.get_current_project_dir_path()

        try:
            toplevel_exit_code, toplevel_stdout, _ = await self._run_git(
                shlex.join(["git", "rev-parse", "--show-toplevel"]), cwd=cwd
            )
            if toplevel_exit_code != 0:
                self.logger.debug(f"{cwd} is not a git repository")
                return GetGitDiffRunResult(repo_root=None, files=[], error=None)

            repo_root = pathlib.Path(toplevel_stdout.strip())
            repo_root_uri = path_to_resource_uri(repo_root)

            if payload.paths == []:
                return GetGitDiffRunResult(
                    repo_root=repo_root_uri, files=[], error=None
                )

            diff_args = [
                "git",
                "-c",
                "core.quotepath=false",
                "diff",
                "--no-color",
                f"-U{payload.context_lines}",
            ]
            if payload.source == GitDiffSource.STAGED:
                diff_args.append("--cached")
            elif payload.source == GitDiffSource.WORKTREE_AND_HEAD:
                diff_args.append("HEAD")

            # A pathspec is always passed: `git diff` covers the whole
            # repository regardless of cwd, so `paths=None` (meaning "the whole
            # project directory") needs the project directory named explicitly.
            diff_args.append("--")
            if payload.paths is None:
                diff_args.append(str(cwd))
            else:
                diff_args.extend(
                    str(resource_uri_to_path(uri)) for uri in payload.paths
                )

            diff_exit_code, diff_stdout, diff_stderr = await self._run_git(
                shlex.join(diff_args), cwd=cwd
            )
            if diff_exit_code != 0:
                return GetGitDiffRunResult(
                    repo_root=repo_root_uri, files=[], error=diff_stderr.strip()
                )

            files: list[FileDiff] = []
            for section in self._split_sections(diff_stdout):
                file_diff = self._parse_file_diff(section, repo_root)
                if file_diff is None:
                    self.logger.warning(
                        "Skipping a diff section that names no path: "
                        f"{section.splitlines()[0] if section.splitlines() else '<empty>'}"
                    )
                    continue
                files.append(file_diff)
            self.logger.debug(f"git diff returned {len(files)} file(s)")

            return GetGitDiffRunResult(repo_root=repo_root_uri, files=files, error=None)
        except Exception as exception:
            self.logger.debug(f"Getting git diff raised: {exception}")
            return GetGitDiffRunResult(repo_root=None, files=[], error=str(exception))
