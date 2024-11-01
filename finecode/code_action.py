# code actions are implementations of actions
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Sequence, TypeVar

from pydantic import BaseModel


class CodeActionConfig(BaseModel): ...


class RunActionPayload(BaseModel): ...


class RunActionResult(BaseModel): ...


CodeActionConfigType = TypeVar("CodeActionConfigType")
RunPayloadType = TypeVar("RunPayloadType", bound=RunActionPayload)
RunResultType = TypeVar("RunResultType", bound=RunActionResult)
RunOnManyResult = dict[Path, RunResultType]


class RunOnManyPayload(BaseModel, Generic[RunPayloadType]):
    # single payloads are homogeneous, e.g. if one item has only apply_on or apply_on_text, then
    # all items have the same properties
    single_payloads: Sequence[RunPayloadType]
    dir_path: Path


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

    def __init__(self, config: CodeActionConfigType) -> None:
        self.config = config

    async def run(self, payload: RunPayloadType) -> RunResultType:
        raise NotImplementedError()

    async def run_on_many(self, payload: RunOnManyPayload[RunPayloadType]) -> RunOnManyResult:
        raise NotImplementedError()


class LintRunPayload(RunActionPayload):
    apply_on: Path | None
    apply_on_text: str


class LintRunResult(RunActionResult): ...


class CodeLintAction(CodeAction[CodeActionConfigType, LintRunPayload, LintRunResult]):
    # lint actions only analyses code, they don't modify it. This allows to run them in parallel.
    ...


class FormatRunPayload(RunActionPayload):
    apply_on: Path | None
    apply_on_text: str


class FormatRunResult(RunActionResult):
    # create additional more basic RunResult like ChangingCodeRunResult to cover other cases like
    # code transformations?
    changed: bool
    # if formatter supports, it should return the result of formatting in `code`. Otherwise set it
    # to None, it will be interpreted as in-place formatting.
    code: str | None


class CodeFormatAction(CodeAction[CodeActionConfigType, FormatRunPayload, FormatRunResult]):
    # format actions can both analyse and modify the code. Analysis is required for example to
    # report errors that cannot be fixed automatically.
    ...


@dataclass(frozen=True)
class LintMessage:
    filepath: Path
    line: int
    column: int
    code: str
    message: str
