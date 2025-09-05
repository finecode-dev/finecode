from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions import lint as lint_action
from finecode_extension_api.interfaces import icache, icommandrunner, ilogger, ifilemanager


@dataclasses.dataclass
class RuffLintHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    target_version: str = "py38"
    select: list[str] | None = None  # Rules to enable
    ignore: list[str] | None = None  # Rules to disable
    preview: bool = False


class RuffLintHandler(
    code_action.ActionHandler[lint_action.LintAction, RuffLintHandlerConfig]
):
    CACHE_KEY = "RuffLinter"

    def __init__(
        self,
        config: RuffLintHandlerConfig,
        cache: icache.ICache,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        command_runner: icommandrunner.ICommandRunner,
    ) -> None:
        self.config = config
        self.cache = cache
        self.logger = logger
        self.file_manager = file_manager
        self.command_runner = command_runner
        
        self.ruff_bin_path = Path(sys.executable).parent / "ruff"

    async def run_on_single_file(
        self, file_path: Path
    ) -> lint_action.LintRunResult:
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
        file_content = await self.file_manager.get_content(file_path)
        lint_messages = await self.run_ruff_lint_on_single_file(file_path, file_content)
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

    async def run_ruff_lint_on_single_file(
        self,
        file_path: Path,
        file_content: str,
    ) -> list[lint_action.LintMessage]:
        """Run ruff linting on a single file"""
        lint_messages: list[lint_action.LintMessage] = []

        # Build ruff check command
        cmd = [
            str(self.ruff_bin_path),
            "check",
            "--output-format",
            "json",
            "--line-length",
            str(self.config.line_length),
            "--target-version",
            self.config.target_version,
            "--stdin-filename",
            str(file_path),
        ]

        if self.config.select:
            cmd.extend(["--select", ",".join(self.config.select)])
        if self.config.ignore:
            cmd.extend(["--ignore", ",".join(self.config.ignore)])
        if self.config.preview:
            cmd.append("--preview")

        cmd_str = " ".join(cmd)
        ruff_process = await self.command_runner.run(
            cmd_str,
        )
        
        ruff_process.write_to_stdin(file_content)
        ruff_process.close_stdin()  # Signal EOF
        
        await ruff_process.wait_for_end()
        
        output = ruff_process.get_output()
        try:
            ruff_results = json.loads(output)
            for violation in ruff_results:
                lint_message = map_ruff_violation_to_lint_message(violation)
                lint_messages.append(lint_message)
        except json.JSONDecodeError:
            raise code_action.ActionFailedException(f'Output of ruff is not json: {output}')

        return lint_messages


def map_ruff_violation_to_lint_message(violation: dict) -> lint_action.LintMessage:
    """Map a ruff violation to a lint message"""
    location = violation.get("location", {})
    end_location = violation.get("end_location", {})

    # Extract line/column info (ruff uses 1-based indexing)
    start_line = max(1, location.get("row", 1))
    start_column = max(0, location.get("column", 0))
    end_line = max(1, end_location.get("row", start_line + 1)) - 1  # Convert to 0-based
    end_column = max(0, end_location.get("column", start_column))

    # Determine severity based on rule code
    code = violation.get("code", "")
    code_description = violation.get("url", "")
    if code.startswith(("E", "F")):  # Error codes
        severity = lint_action.LintMessageSeverity.ERROR
    elif code.startswith("W"):  # Warning codes
        severity = lint_action.LintMessageSeverity.WARNING
    else:
        severity = lint_action.LintMessageSeverity.INFO

    return lint_action.LintMessage(
        range=lint_action.Range(
            start=lint_action.Position(line=start_line, character=start_column),
            end=lint_action.Position(line=end_line, character=end_column),
        ),
        message=violation.get("message", ""),
        code=code,
        code_description=code_description,
        source="ruff",
        severity=severity,
    )
