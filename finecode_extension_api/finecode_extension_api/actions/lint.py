import collections.abc
import enum
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from finecode_extension_api import code_action


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


class LintRunPayload(code_action.RunActionWithPartialResult):
    file_paths: list[Path]


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


type LintAction = code_action.Action[LintRunPayload,
        code_action.RunActionContext,
        collections.abc.AsyncIterator[LintRunResult]]
