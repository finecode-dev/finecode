import collections.abc
import dataclasses
import enum
from pathlib import Path

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class Position:
    line: int
    character: int


@dataclasses.dataclass
class Range:
    start: Position
    end: Position


class LintMessageSeverity(enum.IntEnum):
    # use IntEnum to get json serialization out of the box
    ERROR = 1
    WARNING = 2
    INFO = 3
    HINT = 4


@dataclasses.dataclass
class LintMessage:
    range: Range
    message: str
    code: str | None = None
    code_description: str | None = None
    source: str | None = None
    severity: LintMessageSeverity | None = None


@dataclasses.dataclass
class LintRunPayload(code_action.RunActionPayload, collections.abc.AsyncIterable):
    file_paths: list[Path]

    def __aiter__(self) -> collections.abc.AsyncIterator[Path]:
        return LintRunPayloadIterator(self)


@dataclasses.dataclass
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


@dataclasses.dataclass
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

    def to_text(self) -> str | textstyler.StyledText:
        text: textstyler.StyledText = textstyler.StyledText()
        for file_path_str, file_messages in self.messages.items():
            if len(file_messages) > 0:
                for message in file_messages:
                    # TODO: relative file path?
                    source_str = ""
                    if message.source is not None:
                        source_str = f" ({message.source})"
                    text.append_styled(file_path_str, bold=True)
                    text.append(f":{message.range.start.line}")
                    text.append(f":{message.range.start.character}: ")
                    text.append_styled(message.code, foreground=textstyler.Color.RED)
                    text.append(f" {message.message}{source_str}\n")
            else:
                text.append_styled(file_path_str, bold=True)
                text.append(": OK\n")

        return text

    @property
    def return_code(self) -> code_action.RunReturnCode:
        for lint_messages in self.messages.values():
            if len(lint_messages) > 0:
                return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class LintAction(code_action.Action):
    PAYLOAD_TYPE = LintRunPayload
    RUN_CONTEXT_TYPE = code_action.RunActionWithPartialResultsContext
    RESULT_TYPE = LintRunResult
