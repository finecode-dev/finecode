# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_files_action
from finecode_extension_api.resource_uri import ResourceUri


class FormatTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class FormatRunPayload(code_action.RunActionPayload):
    save: bool = True
    """Whether to write formatted content back to disk."""
    target: FormatTarget = FormatTarget.PROJECT
    """Scope of formatting: 'project' (default) formats the whole project, 'files' formats only file_paths."""
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files to format (``file://`` URIs). Only used when target is 'files'."""


class FormatRunContext(code_action.RunActionContext[FormatRunPayload]): ...


@dataclasses.dataclass
class FormatRunResult(format_files_action.FormatFilesRunResult): ...


class FormatAction(
    code_action.Action[FormatRunPayload, FormatRunContext, FormatRunResult]
):
    """Format source code in a project or specific files."""

    PAYLOAD_TYPE = FormatRunPayload
    RUN_CONTEXT_TYPE = FormatRunContext
    RESULT_TYPE = FormatRunResult
