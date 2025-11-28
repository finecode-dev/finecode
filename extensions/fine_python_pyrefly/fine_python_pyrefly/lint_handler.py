from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions import lint as lint_action
from finecode_extension_api.interfaces import (
    icache,
    icommandrunner,
    ilogger,
    ifilemanager,
    iprojectfileclassifier,
    iextensionrunnerinfoprovider,
)


@dataclasses.dataclass
class PyreflyLintHandlerConfig(code_action.ActionHandlerConfig):
    python_version: str | None = None


class PyreflyLintHandler(
    code_action.ActionHandler[lint_action.LintAction, PyreflyLintHandlerConfig]
):
    """
    NOTE: pyrefly currently can check only saved files, not file content provided by
    FineCode. In environments like IDE, messages from pyrefly will be updated only after
    save of a file.
    """

    CACHE_KEY = "PyreflyLinter"

    def __init__(
        self,
        config: PyreflyLintHandlerConfig,
        cache: icache.ICache,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        command_runner: icommandrunner.ICommandRunner,
        project_file_classifier: iprojectfileclassifier.IProjectFileClassifier,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
    ) -> None:
        self.config = config
        self.cache = cache
        self.logger = logger
        self.file_manager = file_manager
        self.command_runner = command_runner
        self.project_file_classifier = project_file_classifier
        self.extension_runner_info_provider = extension_runner_info_provider

        self.pyrefly_bin_path = Path(sys.executable).parent / "pyrefly"

    async def run_on_single_file(self, file_path: Path) -> lint_action.LintRunResult:
        messages = {}
        try:
            cached_lint_messages = await self.cache.get_file_cache(
                file_path, self.CACHE_KEY
            )
            messages[str(file_path)] = cached_lint_messages
            return lint_action.LintRunResult(messages=messages)
        except icache.CacheMissException:
            pass

        file_version = await self.file_manager.get_file_version(file_path)
        lint_messages = await self.run_pyrefly_lint_on_single_file(file_path)
        messages[str(file_path)] = lint_messages
        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, lint_messages
        )

        return lint_action.LintRunResult(messages=messages)

    async def run(
        self,
        payload: lint_action.LintRunPayload,
        run_context: code_action.RunActionWithPartialResultsContext,
    ) -> None:
        file_paths = [file_path async for file_path in payload]

        for file_path in file_paths:
            run_context.partial_result_scheduler.schedule(
                file_path,
                self.run_on_single_file(file_path),
            )

    async def run_pyrefly_lint_on_single_file(
        self,
        file_path: Path,
    ) -> list[lint_action.LintMessage]:
        """Run pyrefly type checking on a single file"""
        lint_messages: list[lint_action.LintMessage] = []

        try:
            # project file classifier caches result, we can just get it each time again
            file_type = self.project_file_classifier.get_project_file_type(
                file_path=file_path
            )
            file_env = self.project_file_classifier.get_env_for_file_type(
                file_type=file_type
            )
        except NotImplementedError:
            self.logger.warning(
                f"Skip {file_path} because file type or env for it could be determined"
            )
            return lint_messages

        venv_dir_path = self.extension_runner_info_provider.get_venv_dir_path_of_env(
            env_name=file_env
        )
        site_package_pathes = (
            self.extension_runner_info_provider.get_venv_site_packages(
                venv_dir_path=venv_dir_path
            )
        )
        interpreter_path = (
            self.extension_runner_info_provider.get_venv_python_interpreter(
                venv_dir_path=venv_dir_path
            )
        )

        # --skip-interpreter-query isn't used because it is not compatible
        # with --python-interpreter parameter
        # --disable-search-path-heuristics=true isn't used because pyrefly doesn't
        # recognize some imports without it. For example, it cannot resolve relative
        # imports in root __init__.py . Needs to be investigated
        cmd = [
            str(self.pyrefly_bin_path),
            "check",
            "--output-format=json",
            # path to python interpreter because pyrefly resolves .pth files only if
            # it is provided
            f"--python-interpreter='{str(interpreter_path)}'",
        ]

        if self.config.python_version is not None:
            cmd.append(f"--python-version='{self.config.python_version}'")

        for path in site_package_pathes:
            cmd.append(f"--site-package-path={str(path)}")
        cmd.append(str(file_path))

        cmd_str = " ".join(cmd)
        pyrefly_process = await self.command_runner.run(cmd_str)

        await pyrefly_process.wait_for_end()

        output = pyrefly_process.get_output()
        try:
            pyrefly_results = json.loads(output)
            for error in pyrefly_results["errors"]:
                lint_message = map_pyrefly_error_to_lint_message(error)
                lint_messages.append(lint_message)
        except json.JSONDecodeError:
            raise code_action.ActionFailedException(
                f"Output of pyrefly is not json: {output}"
            )

        return lint_messages


def map_pyrefly_error_to_lint_message(error: dict) -> lint_action.LintMessage:
    """Map a pyrefly error to a lint message"""
    # Extract line/column info (pyrefly uses 1-based indexing)
    start_line = error["line"]
    start_column = error["column"]
    end_line = error["stop_line"]
    end_column = error["stop_column"]

    # Determine severity based on error type
    error_code = str(error.get("code", ""))
    code_description = error.get("name", "")
    severity = lint_action.LintMessageSeverity.ERROR

    return lint_action.LintMessage(
        range=lint_action.Range(
            start=lint_action.Position(line=start_line, character=start_column),
            end=lint_action.Position(line=end_line, character=end_column),
        ),
        message=error.get("description", ""),
        code=error_code,
        code_description=code_description,
        source="pyrefly",
        severity=severity,
    )
