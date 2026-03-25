# docs: docs/reference/actions.md
import collections.abc
import dataclasses
import enum

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class Position:
    """A position in a text document.

    Both ``line`` and ``character`` are **0-based**, matching the LSP specification:
    - ``line``: 0-based line index (line 0 = first line of the file).
    - ``character``: 0-based UTF-16 code unit offset within the line.

    Extension authors note: most CLI linters (ruff, mypy, flake8) report 1-based line
    numbers in their output. You must subtract 1 when building a ``Position`` from such
    output::

        # ruff JSON: location["row"] is 1-based
        Position(line=location["row"] - 1, character=location["column"])

    Extensions that receive diagnostics from an embedded LSP server (via
    ``map_diagnostics_to_lint_messages``) get 0-based values directly from the LSP
    protocol — do NOT subtract 1 in that case.
    """

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
class LintFilesRunPayload(
    code_action.RunActionPayload, collections.abc.AsyncIterable[ResourceUri]
):
    file_paths: list[ResourceUri]

    def __aiter__(self) -> collections.abc.AsyncIterator[ResourceUri]:
        return LintFilesRunPayloadIterator(self)


@dataclasses.dataclass
class LintFilesRunPayloadIterator(collections.abc.AsyncIterator[ResourceUri]):
    def __init__(self, lint_files_run_payload: LintFilesRunPayload):
        self.lint_files_run_payload = lint_files_run_payload
        self.current_file_path_index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> ResourceUri:
        if len(self.lint_files_run_payload.file_paths) <= self.current_file_path_index:
            raise StopAsyncIteration()
        self.current_file_path_index += 1
        return self.lint_files_run_payload.file_paths[self.current_file_path_index - 1]


@dataclasses.dataclass
class LintFilesRunResult(code_action.RunActionResult):
    # messages is a dict to support messages for multiple files because it could be the
    # case that linter checks given file and its dependencies.
    messages: dict[ResourceUri, list[LintMessage]]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, LintFilesRunResult):
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
                    text.append(f":{message.range.start.line + 1}")
                    text.append(f":{message.range.start.character + 1}: ")
                    if message.code is not None:
                        text.append_styled(
                            message.code, foreground=textstyler.Color.RED
                        )
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


class LintFilesRunContext(
    code_action.RunActionWithPartialResultsContext[LintFilesRunPayload]
): ...


class LintFilesAction(
    code_action.Action[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    """Run linters on specific files and report diagnostics. Internal action dispatched by lint."""

    PAYLOAD_TYPE = LintFilesRunPayload
    RUN_CONTEXT_TYPE = LintFilesRunContext
    RESULT_TYPE = LintFilesRunResult
