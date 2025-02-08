import operator

from flake8.api import legacy as flake8

from finecode import CodeActionConfig, CodeLintAction, LintMessage, LintRunPayload, LintRunResult
from finecode.extension_runner.code_action import (
    ActionContext,
    CodeActionConfigType,
    LintMessageSeverity,
    Position,
    Range,
)
from finecode.extension_runner.interfaces import ilogger


class Flake8CodeActionConfig(CodeActionConfig):
    max_line_length: int = 79


class Flake8CodeAction(CodeLintAction[Flake8CodeActionConfig]):

    def __init__(self, config: CodeActionConfigType, context: ActionContext, logger: ilogger.ILogger) -> None:
        super().__init__(config, context)
        self.logger = logger

    async def run(self, payload: LintRunPayload) -> LintRunResult:
        assert payload.apply_on is not None
        self.logger.debug(f"start flake8 {self.config.max_line_length}")

        # avoid outputting low-level logs of flake8, our goal is to trace finecode, not flake8 itself
        self.logger.disable("flake8")
        # TODO: options
        style_guide = flake8.get_style_guide(max_line_length=self.config.max_line_length)
        report = style_guide.check_files([str(payload.apply_on)])
        self.logger.enable("flake8")
        messages_by_filepath = {}
        all_results = report._application.file_checker_manager.results

        all_results.sort(key=operator.itemgetter(0))
        for filename, results, _ in all_results:
            if filename not in messages_by_filepath:
                messages_by_filepath[filename] = []

            results.sort(key=operator.itemgetter(1, 2))

            for error_code, line_number, column, text, physical_line in results:
                messages_by_filepath[filename].append(
                    LintMessage(
                        range=Range(
                            start=Position(line=line_number, character=column),
                            end=Position(line=line_number, character=len(physical_line) if physical_line is not None else column),
                        ),
                        message=text,
                        code=error_code,
                        source="flake8",
                        severity=(
                            LintMessageSeverity.WARNING if error_code.startswith("W") else LintMessageSeverity.ERROR
                        ),
                    )
                )

        return LintRunResult(messages=messages_by_filepath)
