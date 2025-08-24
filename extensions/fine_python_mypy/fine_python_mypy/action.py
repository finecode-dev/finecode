# TODO: what to do with file manager? Mypy would need ability to check module text,
# not only module file
import asyncio
import dataclasses
import hashlib
import sys
from pathlib import Path

import fine_python_mypy.output_parser as output_parser

from finecode_extension_api import code_action
from finecode_extension_api.actions import lint as lint_action
from finecode_extension_api.interfaces import (
    icache,
    icommandrunner,
    ifilemanager,
    ilogger,
)


class DmypyFailedError(Exception): ...


@dataclasses.dataclass
class MypyManyCodeActionConfig(code_action.ActionHandlerConfig): ...


class MypyLintHandler(
    code_action.ActionHandler[lint_action.LintAction, MypyManyCodeActionConfig]
):
    CACHE_KEY = "mypy"

    DMYPY_ARGS = [
        "--no-color-output",
        "--no-error-summary",
        "--show-absolute-path",
        "--show-column-numbers",
        "--show-error-codes",
        "--no-pretty",
    ]
    DMYPY_ENV_VARS = {
        "PYTHONUTF8": "1",
    }

    def __init__(
        self,
        context: code_action.ActionContext,
        cache: icache.ICache,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        lifecycle: code_action.ActionHandlerLifecycle,
        command_runner: icommandrunner.ICommandRunner,
    ) -> None:
        self.context = context
        self.cache = cache
        self.logger = logger
        self.file_manager = file_manager
        self.command_runner = command_runner

        lifecycle.on_shutdown(self.shutdown)
        lifecycle.on_exit(self.exit)

        self._dmypy_active_projects: set[Path] = set([])
        self._process_lock_by_cwd: dict[Path, asyncio.Lock] = {}
        # project that are being checked right now
        self._projects_being_checked_done_events: dict[Path, asyncio.Event] = {}

    async def run_on_single_file(
        self,
        file_path: Path,
        project_path: Path,
        all_project_files: list[Path],
        action_run_id: int,
    ) -> lint_action.LintRunResult:
        # if mypy was run on the file, the result will be found in cache. If result
        # is not in cache, we need additionally to check whether mypy is not running
        # on the file right now, because we run mypy on the whole packages.
        messages: dict[str, list[lint_action.LintMessage]] = {}
        # TODO: right cache with dependencies
        try:
            cached_lint_messages = await self.cache.get_file_cache(
                file_path, self.CACHE_KEY
            )
            messages[str(file_path)] = cached_lint_messages
            return lint_action.LintRunResult(messages=messages)
        except icache.CacheMissException:
            pass

        if project_path in self._projects_being_checked_done_events:
            # use events to know when checking of the project is done. Get results from
            # cache because saving them locally would require more complex data
            # structure and additional synchronization, because we need to to wait on
            # the result, provide it to all waiting tasks and remove after that.
            await self._projects_being_checked_done_events[project_path].wait()
            try:
                cached_lint_messages = await self.cache.get_file_cache(
                    file_path, self.CACHE_KEY
                )
            except icache.CacheMissException:
                # if checking failed, there are no results in cache
                cached_lint_messages = []

            messages[str(file_path)] = cached_lint_messages
            return lint_action.LintRunResult(messages=messages)
        else:
            # save file versions at the beginning because file can be changed during
            # checking and we want to cache result for current version, not for changed
            project_checked_event = asyncio.Event()
            self._projects_being_checked_done_events[project_path] = (
                project_checked_event
            )
            files_versions: dict[Path, str] = {}
            # can we exclude cached files here? Using the right cache(one that handles
            # dependencies as well) should be possible
            for file_path in all_project_files:
                file_version = await self.file_manager.get_file_version(file_path)
                files_versions[file_path] = file_version

            try:
                all_processed_files_with_messages = await self._run_dmypy_on_project(
                    project_path, all_project_files
                )
                messages = {
                    str(file_path): lint_messages
                    for (
                        file_path,
                        lint_messages,
                    ) in all_processed_files_with_messages.items()
                }

                for (
                    file_path,
                    lint_messages,
                ) in all_processed_files_with_messages.items():
                    try:
                        file_version = files_versions[file_path]
                    except KeyError:
                        # mypy can resolve dependencies which are not in `files_to_lint`
                        # and as result also not in `files_versions`
                        file_version = await self.file_manager.get_file_version(
                            file_path
                        )

                    await self.cache.save_file_cache(
                        file_path, file_version, self.CACHE_KEY, lint_messages
                    )
            finally:
                project_checked_event.set()
                del self._projects_being_checked_done_events[project_path]

            return lint_action.LintRunResult(messages=messages)

    async def _run_dmypy_on_project(
        self, project_dir_path: Path, all_project_files: list[Path]
    ) -> dict[Path, list[lint_action.LintMessage]]:
        new_messages: dict[str, list[lint_action.LintMessage]] = {}
        if project_dir_path not in self._process_lock_by_cwd:
            self._process_lock_by_cwd[project_dir_path] = asyncio.Lock()

        project_lock = self._process_lock_by_cwd[project_dir_path]
        async with project_lock:
            try:
                dmypy_run_output = await self._run_dmypy(
                    file_paths=all_project_files, cwd=project_dir_path
                )
            except DmypyFailedError:
                return {}

        project_lint_messages = output_parser.parse_output_using_regex(
            content=dmypy_run_output, severity={}
        )
        new_messages.update(project_lint_messages)
        all_processed_files_with_messages: dict[Path, list[lint_action.LintMessage]] = {
            file_path: [] for file_path in all_project_files
        }
        all_processed_files_with_messages.update(
            {
                Path(file_path_str): lint_messages
                for file_path_str, lint_messages in new_messages.items()
            }
        )
        return all_processed_files_with_messages

    async def run(
        self,
        payload: lint_action.LintRunPayload,
        run_context: code_action.RunActionWithPartialResultsContext,
    ) -> None:
        file_paths = [file_path async for file_path in payload]

        files_by_projects: dict[Path, list[Path]] = self.group_files_by_projects(
            file_paths, self.context.project_dir
        )

        for project_path, project_files in files_by_projects.items():
            for file_path in project_files:
                run_context.partial_result_scheduler.schedule(
                    file_path,
                    self.run_on_single_file(
                        file_path,
                        project_path,
                        project_files,
                        action_run_id=run_context.run_id,
                    ),
                )

    def shutdown(self) -> None:
        for dmypy_process_cwd in self._dmypy_active_projects:
            try:
                self._stop_dmypy(cwd=dmypy_process_cwd)
            except Exception as error:
                self.logger.error(str(error))

    def exit(self) -> None:
        for dmypy_process_cwd in self._dmypy_active_projects:
            self.logger.debug(
                f"Check whether mypy is still running: {dmypy_process_cwd}"
            )
            status_file_path = self._get_status_file_path(dmypy_cwd=dmypy_process_cwd)
            if status_file_path.exists():
                # status file still exists, kill dmypy
                try:
                    self._kill_dmypy(cwd=dmypy_process_cwd)
                except Exception as error:
                    self.logger.error(str(error))

    def _get_status_file_path(self, dmypy_cwd: Path) -> Path:
        file_dir_path = self.context.cache_dir
        # use hash to avoid name conflict if python packages have the same name
        file_dir_path_hash = hashlib.md5(str(dmypy_cwd).encode("utf-8")).hexdigest()
        file_path = (
            file_dir_path / f".{dmypy_cwd.name}_{file_dir_path_hash[:8]}.dmypy_status"
        )
        if not file_dir_path.exists():
            file_dir_path.mkdir(parents=True)
        return file_path

    async def _run_dmypy(self, file_paths: list[Path], cwd: Path) -> str:
        # returns output of dmypy if it was successful, otherwise raises
        # DmypyFailedError
        self.logger.debug(f"run dmypy in {cwd}")
        status_file_path = self._get_status_file_path(dmypy_cwd=cwd)
        runner_python_executable = sys.executable
        file_paths_strs = [str(file_path) for file_path in file_paths]
        cmd_parts = [
            f"{runner_python_executable}",
            "-m",
            "mypy.dmypy",
            f"--status-file='{status_file_path}'",
            "run",
            "--",
            *self.DMYPY_ARGS,
            *file_paths_strs,
        ]
        cmd = " ".join(cmd_parts)
        dmypy_run_process = await self.command_runner.run(
            cmd=cmd,
            cwd=cwd,
            env=self.DMYPY_ENV_VARS,
        )
        self._dmypy_active_projects.add(cwd)
        await dmypy_run_process.wait_for_end()
        self.logger.debug(f"end in {cwd}")
        self._check_dmypy_process_exit_code(dmypy_process=dmypy_run_process)
        dmypy_output = dmypy_run_process.get_output()
        return dmypy_output

    def _stop_dmypy(self, cwd: Path) -> None:
        self.logger.debug(f"stop dmypy in {cwd}")
        status_file_path = self._get_status_file_path(dmypy_cwd=cwd)
        runner_python_executable = sys.executable
        dmypy_stop_process = self.command_runner.run_sync(
            cmd=(
                f"{runner_python_executable} -m mypy.dmypy"
                f" --status-file='{status_file_path}' stop"
            ),
            cwd=cwd,
            # env=self.DMYPY_ENV_VARS,
        )
        dmypy_stop_process.wait_for_end(timeout=1)
        self._check_dmypy_process_exit_code(dmypy_process=dmypy_stop_process)

    def _kill_dmypy(self, cwd: Path) -> None:
        self.logger.debug(f"kill dmypy in {cwd}")
        status_file_path = self._get_status_file_path(dmypy_cwd=cwd)
        runner_python_executable = sys.executable
        dmypy_kill_process = self.command_runner.run_sync(
            cmd=(
                f"{runner_python_executable} -m mypy.dmypy"
                f" --status-file='{status_file_path}' kill"
            ),
            cwd=cwd,
            # env=self.DMYPY_ENV_VARS,
        )
        dmypy_kill_process.wait_for_end(timeout=1)
        self._check_dmypy_process_exit_code(dmypy_process=dmypy_kill_process)

    def _check_dmypy_process_exit_code(
        self, dmypy_process: icommandrunner.IProcess
    ) -> None:
        dmypy_run_exit_code = dmypy_process.get_exit_code()
        if dmypy_run_exit_code != 0 and dmypy_run_exit_code != 1:
            # exit code (experimentally, didn't check mypy codebase, can be not
            # complete):
            # - 0 : successfully checked, no errors found
            # - 1 : successfully checked, errors found
            # - 2 : failed to check
            dmypy_output = dmypy_process.get_output()
            dmypy_error_output = dmypy_process.get_error_output()
            self.logger.error(
                f"Dmypy run failed with return code {dmypy_run_exit_code}"
            )
            self.logger.debug(f"stdout: {dmypy_output}")
            self.logger.debug(f"stderr: {dmypy_error_output}")
            raise DmypyFailedError()

    def group_files_by_projects(
        self, files: list[Path], root_dir: Path
    ) -> dict[Path, list[Path]]:
        # TODO: make this function reusable?
        files_by_projects_dirs: dict[Path, list[Path]] = {}

        projects_defs = list(root_dir.rglob("pyproject.toml"))
        projects_dirs = [project_def.parent for project_def in projects_defs]
        # sort by depth so that child items are first
        # default reverse path sorting works so, that child items are before their
        # parents
        projects_dirs.sort(reverse=True)

        for file_path in files:
            for project_dir in projects_dirs:
                if file_path.is_relative_to(project_dir):
                    if project_dir not in files_by_projects_dirs:
                        files_by_projects_dirs[project_dir] = []
                    files_by_projects_dirs[project_dir].append(file_path)
                    break

        return files_by_projects_dirs
