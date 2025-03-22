import argparse
import ast
import collections.abc
import operator
from pathlib import Path

from fine_python_ast import iast_provider
from flake8 import checker, processor, style_guide, violation
from flake8.api import legacy as flake8
from flake8.plugins import finder

from finecode_extension_api import code_action
from finecode_extension_api.actions import lint as lint_action
from finecode_extension_api.interfaces import icache, ifilemanager, ilogger


def map_flake8_check_result_to_lint_message(result: tuple) -> lint_action.LintMessage:
    error_code, line_number, column, text, physical_line = result
    return lint_action.LintMessage(
        range=lint_action.Range(
            start=lint_action.Position(line=line_number, character=column),
            end=lint_action.Position(
                line=line_number,
                character=len(physical_line) if physical_line is not None else column,
            ),
        ),
        message=text,
        code=error_code,
        source="flake8",
        severity=(
            lint_action.LintMessageSeverity.WARNING
            if error_code.startswith("W")
            else lint_action.LintMessageSeverity.ERROR
        ),
    )


def run_flake8_on_single_file(
    file_path: Path,
    file_content: str,
    file_ast: ast.Module,
    guide: flake8.StyleGuide,
    decider: style_guide.DecisionEngine,
) -> list[lint_action.LintMessage]:
    lint_messages: list[lint_action.LintMessage] = []
    # flake8 expects lines with newline at the end
    file_lines = [line + "\n" for line in file_content.split("\n")]

    file_checker = CustomFlake8FileChecker(
        filename=str(file_path),
        plugins=guide._application.plugins.checkers,
        options=guide.options,
        file_lines=file_lines,
        file_ast=file_ast,
    )
    _, file_results, _ = file_checker.run_checks()

    file_results.sort(key=operator.itemgetter(1, 2))
    for result in file_results:
        error_code, line_number, column_number, text, physical_line = result
        # flake8 first collects all errors and then checks whether they are
        # valid for the file
        #
        # flake8 uses multiple styleguides and StyleGuideManager selects
        # the right one for the file being processed. We have currently
        # only one styleguide, so no selecting is needed.
        #
        # Check in the same way as `StyleGuide.handle_error` does,
        # just skip formatting part.
        disable_noqa = guide.options.disable_noqa
        # NOTE(sigmavirus24): Apparently we're provided with 0-indexed column
        # numbers so we have to offset that here.
        if not column_number:
            column_number = 0
        error = violation.Violation(
            error_code,
            str(file_path),
            line_number,
            column_number + 1,
            text,
            physical_line,
        )
        # run decider as `flake8.style_guide.StyleGuide.should_report_error` does
        error_is_selected = (
            decider.decision_for(error.code) is style_guide.Decision.Selected
        )
        is_not_inline_ignored = error.is_inline_ignored(disable_noqa) is False
        if error_is_selected and is_not_inline_ignored:
            lint_message = map_flake8_check_result_to_lint_message(result)
            lint_messages.append(lint_message)

    return lint_messages


class Flake8ManyCodeActionConfig(code_action.CodeActionConfig):
    max_line_length: int = 79
    extend_select: list[str] | None = None
    extend_ignore: list[str] | None = None


class Flake8ManyCodeAction(lint_action.LintManyCodeAction[Flake8ManyCodeActionConfig]):
    CACHE_KEY = "flake8"

    def __init__(
        self,
        config: Flake8ManyCodeActionConfig,
        context: code_action.ActionContext,
        cache: icache.ICache,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        ast_provider: iast_provider.IPythonSingleAstProvider,
    ) -> None:
        super().__init__(config, context=context)
        self.cache = cache
        self.logger = logger
        self.file_manager = file_manager
        self.ast_provider = ast_provider

        self.logger.disable("flake8.options.manager")
        self.flake8_style_guide = flake8.get_style_guide(
            max_line_length=self.config.max_line_length,
            extend_select=self.config.extend_select,
            extend_ignore=self.config.extend_ignore,
        )
        self.flake8_decider = style_guide.DecisionEngine(
            self.flake8_style_guide.options
        )

        self.logger.disable("flake8.checker")
        self.logger.disable("flake8.violation")
        self.logger.disable("bugbear")

    async def run(
        self, payload: lint_action.LintManyRunPayload
    ) -> collections.abc.AsyncIterator[lint_action.LintManyRunResult]:
        file_paths = payload.file_paths
        self.flake8_style_guide._application.options.filenames = [
            str(file_path) for file_path in file_paths
        ]
        # TODO: multiprocess pool
        for file_path in file_paths:
            messages = {}
            try:
                cached_lint_messages = await self.cache.get_file_cache(
                    file_path, self.CACHE_KEY
                )
                messages[str(file_path)] = cached_lint_messages
                yield lint_action.LintManyRunResult(messages=messages)
                continue
            except icache.CacheMissException:
                pass

            file_content = await self.file_manager.get_content(file_path)
            file_version = await self.file_manager.get_file_version(file_path)
            try:
                file_ast = await self.ast_provider.get_file_ast(file_path=file_path)
            except SyntaxError:
                continue

            lint_messages = run_flake8_on_single_file(
                file_path=file_path,
                file_content=file_content,
                file_ast=file_ast,
                guide=self.flake8_style_guide,
                decider=self.flake8_decider,
            )
            messages[str(file_path)] = lint_messages
            await self.cache.save_file_cache(
                file_path, file_version, self.CACHE_KEY, lint_messages
            )

            yield lint_action.LintManyRunResult(messages=messages)


class CustomFlake8FileChecker(checker.FileChecker):
    """
    Standard implementation creates FileProcessor without lines argument
    that causes reading file from file system. Overwrite initialisation
    of FileProcessor and provide lines to get file content from FineCode
    FileManager.
    """

    def __init__(
        self,
        *,
        filename: str,
        plugins: finder.Checkers,
        options: argparse.Namespace,
        file_lines: list[str],
        file_ast: ast.Module,
    ):
        self.file_lines = file_lines
        self.file_ast = file_ast
        super().__init__(filename=filename, plugins=plugins, options=options)

    def _make_processor(self) -> processor.FileProcessor | None:
        try:
            return CustomFlake8FileProcessor(
                self.filename,
                self.options,
                file_ast=self.file_ast,
                lines=self.file_lines,
            )
        except OSError as e:
            # If we can not read the file due to an IOError (e.g., the file
            # does not exist or we do not have the permissions to open it)
            # then we need to format that exception for the user.
            # NOTE(sigmavirus24): Historically, pep8 has always reported this
            # as an E902. We probably *want* a better error code for this
            # going forward.
            self.report("E902", 0, 0, f"{type(e).__name__}: {e}")
            return None


class CustomFlake8FileProcessor(processor.FileProcessor):
    """
    Custom file processor to cache AST.
    """

    def __init__(
        self,
        filename: str,
        options: argparse.Namespace,
        file_ast: ast.Module,
        lines=None,
    ):
        self.file_ast = file_ast
        super().__init__(filename, options, lines)

    def build_ast(self) -> ast.AST:
        return self.file_ast
