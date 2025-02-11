import enum
from pathlib import Path
import sys
from dataclasses import dataclass
if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from finecode.extension_runner.code_action import CodeAction, CodeActionConfigType, RunActionContext, RunActionPayload, RunActionResult

class Position(TypedDict):
    line: int
    character: int


class Range(TypedDict):
    start: Position
    end: Position


class LintMessageSeverity(enum.IntEnum):
    # use IntEnum to get json serialization out of the box
    ERROR = 1
    WARNING = 2
    INFO = 3
    HINT = 4


@dataclass(frozen=True)
class LintMessage:
    range: Range
    message: str
    code: str | None = None
    code_description: str | None = None
    source: str | None = None
    severity: LintMessageSeverity | None = None




class LintRunPayload(RunActionPayload):
    apply_on: Path


class LintRunResult(RunActionResult):
    # dict key should be Path, but pygls fails to handle slashes in dict keys, use strings with
    # posix representation of path instead until the problem is properly solved
    messages: dict[str, list[LintMessage]]

    def update(self, other: RunActionResult) -> None:
        if not isinstance(other, RunActionResult):
            return
        self.messages.update(other.messages)


class CodeLintAction(CodeAction[CodeActionConfigType, LintRunPayload, RunActionContext, LintRunResult]):
    # lint actions only analyses code, they don't modify it. This allows to run them in parallel.
    APPLIES_ONLY_ON_FILE: bool = False
    NEEDS_WHOLE_PROJECT: bool = False
