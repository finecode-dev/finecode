# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import lint_files_action
from finecode_extension_api.resource_uri import ResourceUri


class LintTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class LintRunPayload(code_action.RunActionPayload):
    target: LintTarget = LintTarget.PROJECT
    """Scope of linting: 'project' (default) lints the whole workspace, 'files' lints only file_paths."""
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files to lint (``file://`` URIs). Only used when target is 'files'."""
    project_paths: list[ResourceUri] | None = None
    """Restrict the workspace operation to these project root URIs (``file://`` URIs). None means the whole workspace."""


@dataclasses.dataclass
class LintRunResult(lint_files_action.LintFilesRunResult): ...


class LintRunContext(
    code_action.RunActionWithPartialResultsContext[LintRunPayload]
): ...


class LintAction(code_action.Action[LintRunPayload, LintRunContext, LintRunResult]):
    DESCRIPTION = "Run linters across the workspace and report diagnostics."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = LintRunPayload
    RUN_CONTEXT_TYPE = LintRunContext
    RESULT_TYPE = LintRunResult


# reexport
LintMessage = lint_files_action.LintMessage
