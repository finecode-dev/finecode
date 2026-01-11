import dataclasses
import enum
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions import lint_files


class LintTarget(enum.StrEnum):
    PROJECT = 'project'
    FILES = 'files'


@dataclasses.dataclass
class LintRunPayload(code_action.RunActionPayload):
    target: LintTarget = LintTarget.PROJECT
    # optional, expected only with `target == LintTarget.FILES`
    file_paths: list[Path] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LintRunResult(lint_files.LintFilesRunResult):
    ...


LintRunContext = code_action.RunActionWithPartialResultsContext

class LintAction(code_action.Action[LintRunPayload, LintRunContext, LintRunResult]):
    PAYLOAD_TYPE = LintRunPayload
    RUN_CONTEXT_TYPE = LintRunContext
    RESULT_TYPE = LintRunResult


# reexport
LintMessage = lint_files.LintMessage
