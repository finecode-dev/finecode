from __future__ import annotations

import enum
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

if sys.version_info >= (3, 12):
    from typing import TypedDict, override
else:
    from typing_extensions import TypedDict, override

from pydantic import BaseModel


class CodeActionConfig(BaseModel): ...


class RunActionPayload(BaseModel): ...


class RunActionResult(BaseModel):
    def update(self, other: RunActionResult) -> None:
        raise NotImplementedError()

    def to_next_payload(self, original_request: RunActionPayload) -> RunActionPayload:
        raise NotImplementedError()


CodeActionConfigType = TypeVar("CodeActionConfigType")
RunPayloadType = TypeVar("RunPayloadType", bound=RunActionPayload)
RunResultType = TypeVar("RunResultType", bound=RunActionResult)


@dataclass
class ActionContext:
    project_dir: Path
    # runner-specific cache dir
    cache_dir: Path


class CodeAction(Generic[CodeActionConfigType, RunPayloadType, RunResultType]):
    """
    **Action config**
    Configuration can be set in following places by priority:
    - project definition, e.g. pyproject.toml
    - workspace definition (if action is enabled in workspace definition)
    - preset or composable action, it depends where action comes from

    In action implementation there is no action config as such, because config definition includes
    default values.
    """

    LANGUAGE: str = "python"
    IS_BACKGROUND: bool = False

    def __init__(self, config: CodeActionConfigType, context: ActionContext) -> None:
        self.config = config
        self.context = context

    async def run(self, payload: RunPayloadType) -> RunResultType:
        raise NotImplementedError()

    async def stop(self):
        raise NotImplementedError()


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


class CodeLintAction(CodeAction[CodeActionConfigType, LintRunPayload, LintRunResult]):
    # lint actions only analyses code, they don't modify it. This allows to run them in parallel.
    APPLIES_ONLY_ON_FILE: bool = False
    NEEDS_WHOLE_PROJECT: bool = False


class FormatRunPayload(RunActionPayload):
    apply_on: Path


class FormatRunResult(RunActionResult):
    # create additional more basic RunResult like ChangingCodeRunResult to cover other cases like
    # code transformations?
    changed: bool
    # if formatter supports, it should return the result of formatting in `code`. Otherwise set it
    # to None, it will be interpreted as in-place formatting.
    code: str | None

    @override
    def update(self, other: RunActionResult) -> None:
        if not isinstance(other, FormatRunResult):
            return
        if other.changed is True and other.code is not None:
            self.code = other.code

    @override
    def to_next_payload(self, original_request: FormatRunPayload) -> FormatRunPayload:
        if self.changed is True and self.code is not None:
            return FormatRunPayload(
                apply_on=original_request.apply_on,
                apply_on_text=self.code,
            )
        return original_request


class CodeFormatAction(CodeAction[CodeActionConfigType, FormatRunPayload, FormatRunResult]):
    # format actions can both analyse and modify the code. Analysis is required for example to
    # report errors that cannot be fixed automatically.
    ...


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
