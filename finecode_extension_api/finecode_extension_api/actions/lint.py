import collections.abc
import enum
from pathlib import Path

from finecode_extension_api import code_action


class Position(code_action.BaseModel):
    line: int
    character: int


class Range(code_action.BaseModel):
    start: Position
    end: Position


class LintMessageSeverity(enum.IntEnum):
    # use IntEnum to get json serialization out of the box
    ERROR = 1
    WARNING = 2
    INFO = 3
    HINT = 4


class LintMessage(code_action.BaseModel):
    range: Range
    message: str
    code: str | None = None
    code_description: str | None = None
    source: str | None = None
    severity: LintMessageSeverity | None = None


class LintRunPayload(code_action.RunActionPayload, collections.abc.AsyncIterable):
    file_paths: list[Path]

    def __aiter__(self) -> collections.abc.AsyncIterator[Path]:
        return LintRunPayloadIterator(self)


class LintRunPayloadIterator(collections.abc.AsyncIterator):
    def __init__(self, lint_run_payload: LintRunPayload):
        self.lint_run_payload = lint_run_payload
        self.current_file_path_index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> Path:
        if len(self.lint_run_payload.file_paths) <= self.current_file_path_index:
            raise StopAsyncIteration()
        self.current_file_path_index += 1
        return self.lint_run_payload.file_paths[self.current_file_path_index - 1]


class LintRunResult(code_action.RunActionResult):
    # messages is a dict to support messages for multiple files because it could be the
    # case that linter checks given file and its dependencies.
    #
    # dict key should be Path, but pygls fails to handle slashes in dict keys, use
    # strings with posix representation of path instead until the problem is properly
    # solved
    messages: dict[str, list[LintMessage]]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, LintRunResult):
            return

        for file_path_str, new_messages in other.messages.items():
            if file_path_str not in self.messages:
                self.messages[file_path_str] = []
            self.messages[file_path_str].extend(new_messages)


type LintAction = code_action.Action[
    LintRunPayload, code_action.RunActionWithPartialResultsContext, LintRunResult
]
