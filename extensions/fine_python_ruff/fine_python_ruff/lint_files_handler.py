from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from fine_lint.diagnostic_types import (
    Diagnostic,
    DiagnosticFilesRunContext,
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunResult,
    DiagnosticSeverity,
    Position,
    Range,
)
from fine_lint.lint_files_action import LintFilesAction
from fine_python_lang import support_range
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icache,
    icommandrunner,
    ifileeditor,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_ruff import target_version as target_version_utils
from fine_python_ruff.ruff_lsp_service import RuffLspService


@dataclasses.dataclass
class RuffLintFilesHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    target_version: str | None = None
    """Language level to lint against, e.g. ``"py311"``.

    None derives it from the project's declared support range
    (``get_src_artifact_toolchain_range``), which is where it should come from: a
    universal preset cannot know a project's floor, and a hardcoded one silently
    decides which upgrade suggestions and syntax errors every project sees."""
    select: list[str] | None = None  # Rules to enable
    ignore: list[str] | None = None
    """Rules to disable, *replacing* the project's own ``[tool.ruff.lint] ignore``.

    A preset wanting to turn rules off without discarding what the project turned off
    wants ``extend_ignore``."""
    extend_ignore: list[str] | None = None
    """Rules to disable *in addition to* the project's own ``[tool.ruff.lint] ignore``.

    This is what a universal preset wants: it cannot know which rules a project has
    already excused itself from, and ``ignore`` would drop them all.

    The distinction only reaches ruff on the CLI path (``--extend-ignore``). Over LSP
    ruff has no extending spelling, and editor-provided lint settings supersede the
    project's ``[tool.ruff.lint]`` section whichever spelling is used, so the two are
    sent as one list."""
    extend_select: list[str] | None = None
    preview: bool = False
    use_cli: bool = False


class RuffLintFilesHandler(
    code_action.ActionHandler[LintFilesAction, RuffLintFilesHandlerConfig]
):
    CACHE_KEY = "RuffLinter"
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="RuffLinterAstProvider")

    def __init__(
        self,
        config: RuffLintFilesHandlerConfig,
        cache: icache.ICache,
        logger: ilogger.ILogger,
        file_editor: ifileeditor.IFileEditor,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        lsp_service: RuffLspService,
    ) -> None:
        self.config = config
        self.cache = cache
        self.logger = logger
        self.file_editor = file_editor
        self.command_runner = command_runner
        self.project_info_provider: iprojectinfoprovider.IProjectInfoProvider = (
            project_info_provider
        )
        self.lsp_service: RuffLspService = lsp_service

        self.ruff_bin_path = Path(sys.executable).parent / "ruff"

        self._support_range_resolver = support_range.PythonSupportRangeResolver(
            action_runner=action_runner, logger=logger
        )
        # the target version is derived on first use rather than here: deriving it runs
        # another action, and a handler is constructed before the event loop is doing
        # any of the action's work. On the LSP path that derivation belongs to the
        # provider below, which the shared service runs before it starts ruff.
        self._target_version_resolved = False
        self._target_version_lock = asyncio.Lock()
        self._target_version: str | None = None

        if not self.config.use_cli:
            self.lsp_service.add_settings_provider(self._provide_lsp_settings)

    async def _ensure_target_version(self, meta: code_action.RunActionMeta) -> None:
        if self._target_version_resolved:
            return

        async with self._target_version_lock:
            if self._target_version_resolved:
                return

            self._target_version = await target_version_utils.resolve_target_version(
                configured=self.config.target_version,
                resolver=self._support_range_resolver,
                meta=meta,
                logger=self.logger,
            )
            self._target_version_resolved = True

    async def _provide_lsp_settings(
        self, meta: code_action.RunActionMeta
    ) -> dict[str, object]:
        await self._ensure_target_version(meta)

        # reference: https://docs.astral.sh/ruff/editors/settings/
        lint_settings: dict[str, object] = {"enable": True}
        if self.config.select is not None:
            lint_settings["select"] = self.config.select
        if self.config.extend_select is not None:
            lint_settings["extendSelect"] = self.config.extend_select
        ignore = self._lsp_ignore()
        if ignore is not None:
            lint_settings["ignore"] = ignore
        if self.config.preview:
            lint_settings["preview"] = True

        settings: dict[str, object] = {
            "lint": lint_settings,
            "showSyntaxErrors": True,
            "lineLength": self.config.line_length,
        }
        # `targetVersion` is not one of ruff's client settings -- ruff drops unknown
        # fields silently, so sending it there configures nothing. Inline `configuration`
        # is a ruff config table by another name and does carry it.
        if self._target_version is not None:
            settings["configuration"] = {"target-version": self._target_version}
        return settings

    def _lsp_ignore(self) -> list[str] | None:
        """The two ignore lists as the one list the LSP path can express."""
        if self.config.ignore is None and self.config.extend_ignore is None:
            return None
        return [*(self.config.ignore or []), *(self.config.extend_ignore or [])]

    async def run_on_single_file(
        self, file_uri: ResourceUri, meta: code_action.RunActionMeta
    ) -> DiagnosticFilesRunResult:
        file_path = resource_uri_to_path(file_uri)
        messages: dict[ResourceUri, list[Diagnostic]] = {}
        try:
            cached_lint_messages = await self.cache.get_file_cache(
                file_path, self.CACHE_KEY
            )
            messages[file_uri] = cached_lint_messages
            return DiagnosticFilesRunResult(messages=messages)
        except icache.CacheMissException:
            pass

        async with (
            self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session,
            session.read_file(file_path=file_path) as file_info,
        ):
            file_content: str = file_info.content
            file_version: str = file_info.version

        if self.config.use_cli:
            lint_messages = await self.run_ruff_lint_on_single_file(
                file_path, file_content
            )
        else:
            root_uri = (
                self.project_info_provider.get_current_project_dir_path().as_uri()
            )
            await self.lsp_service.ensure_started(root_uri, meta)

            lint_messages = await self.lsp_service.check_file(file_path)
        messages[file_uri] = lint_messages
        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, lint_messages
        )

        return DiagnosticFilesRunResult(messages=messages)

    async def run(
        self,
        payload: DiagnosticFilesRunPayload,
        run_context: DiagnosticFilesRunContext,
    ) -> None:
        if self.config.use_cli:
            # the LSP path derives it inside the settings provider instead, so that it
            # is in place before the shared server starts
            await self._ensure_target_version(run_context.meta)

        file_uris = [file_uri async for file_uri in payload]

        for file_uri in file_uris:
            run_context.partial_result_scheduler.schedule(
                file_uri,
                self.run_on_single_file(file_uri, run_context.meta),
            )

    async def run_ruff_lint_on_single_file(
        self,
        file_path: Path,
        file_content: str,
    ) -> list[Diagnostic]:
        """Run ruff linting on a single file"""
        lint_messages: list[Diagnostic] = []

        # Build ruff check command
        cmd = [
            str(self.ruff_bin_path),
            "check",
            "--output-format",
            "json",
            "--line-length",
            str(self.config.line_length),
            "--stdin-filename",
            str(file_path),
        ]

        # omitted when unknown so ruff infers it from requires-python, as on the LSP path
        if self._target_version is not None:
            cmd += ["--target-version", self._target_version]

        if self.config.select is not None:
            cmd.append("--select=" + ",".join(self.config.select))
        if self.config.extend_select is not None:
            cmd.append("--extend-select=" + ",".join(self.config.extend_select))
        if self.config.ignore is not None:
            cmd.append("--ignore=" + ",".join(self.config.ignore))
        if self.config.extend_ignore is not None:
            cmd.append("--extend-ignore=" + ",".join(self.config.extend_ignore))
        if self.config.preview is True:
            cmd.append("--preview")

        ruff_process = await self.command_runner.run(cmd)

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
            raise code_action.ActionFailedException(
                f"Output of ruff is not json: {output}"
            )

        return lint_messages


def map_ruff_violation_to_lint_message(
    violation: dict,
) -> Diagnostic:
    """Map a ruff violation to a lint message"""
    location = violation.get("location", {})
    end_location = violation.get("end_location", {})

    # Ruff counts rows and columns from 1, LSP from 0 -- and the columns need the
    # shift as much as the rows do: without it every CLI-path diagnostic sits one
    # column to the right of where the same violation lands on the LSP path, so an
    # editor underlines from one character into the offending name.
    start_line = max(1, location.get("row", 1))
    start_column = max(1, location.get("column", 1))
    end_line = max(1, end_location.get("row", start_line + 1))
    end_column = max(1, end_location.get("column", start_column + 1))

    # Determine severity based on rule code
    code = violation.get("code", "")
    code_description = violation.get("url", "")
    if code.startswith(("E", "F")):  # Error codes
        severity = DiagnosticSeverity.ERROR
    elif code.startswith("W"):  # Warning codes
        severity = DiagnosticSeverity.WARNING
    else:
        severity = DiagnosticSeverity.INFO

    return Diagnostic(
        range=Range(
            start=Position(line=start_line - 1, character=start_column - 1),
            end=Position(line=end_line - 1, character=end_column - 1),
        ),
        message=violation.get("message", ""),
        code=code,
        code_description=code_description,
        source="ruff",
        severity=severity,
        # ruff reports the fix inline with the violation, so fixability is known for
        # every violation here -- False means "ruff has no fix", not "unknown".  An
        # unsafe fix is still a fix; applicability travels with the fix itself and is
        # what apply_lint_fixes filters on.
        fixable=violation.get("fix") is not None,
    )
