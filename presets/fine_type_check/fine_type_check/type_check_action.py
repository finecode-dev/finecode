# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api.resource_uri import ResourceUri


class TypeCheckTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class TypeCheckRunPayload(code_action.RunActionPayload):
    target: TypeCheckTarget = TypeCheckTarget.PROJECT
    """Scope of type checking: 'project' (default) checks the whole workspace, 'files' checks only file_paths."""
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files to check (``file://`` URIs). Only used when target is 'files'."""
    project_paths: list[ResourceUri] | None = None
    """Restrict the workspace operation to these project root URIs (``file://`` URIs). None means the whole workspace."""


@dataclasses.dataclass
class TypeCheckRunResult(DiagnosticFilesRunResult): ...


class TypeCheckRunContext(
    code_action.RunActionWithPartialResultsContext[TypeCheckRunPayload]
): ...


class TypeCheckAction(code_action.Action[TypeCheckRunPayload, TypeCheckRunContext, TypeCheckRunResult]):
    DESCRIPTION = "Run type checkers across the workspace and report type errors."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = TypeCheckRunPayload
    RUN_CONTEXT_TYPE = TypeCheckRunContext
    RESULT_TYPE = TypeCheckRunResult
