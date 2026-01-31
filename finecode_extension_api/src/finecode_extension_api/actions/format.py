import dataclasses
import enum
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions import format_files as format_files_action


class FormatTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class FormatRunPayload(code_action.RunActionPayload):
    save: bool = True
    target: FormatTarget = FormatTarget.PROJECT
    # optional, expected only with `target == FormatTarget.FILES`
    file_paths: list[Path] = dataclasses.field(default_factory=list)


class FormatRunContext(code_action.RunActionContext[FormatRunPayload]): ...


@dataclasses.dataclass
class FormatRunResult(format_files_action.FormatFilesRunResult): ...


class FormatAction(
    code_action.Action[FormatRunPayload, FormatRunContext, FormatRunResult]
):
    PAYLOAD_TYPE = FormatRunPayload
    RUN_CONTEXT_TYPE = FormatRunContext
    RESULT_TYPE = FormatRunResult
