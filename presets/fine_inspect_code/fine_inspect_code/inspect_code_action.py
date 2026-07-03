# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api.resource_uri import ResourceUri


class InspectCodeTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class InspectCodeRunPayload(code_action.RunActionPayload):
    target: InspectCodeTarget = InspectCodeTarget.PROJECT
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    project_paths: list[ResourceUri] | None = None


@dataclasses.dataclass
class InspectCodeRunResult(DiagnosticFilesRunResult): ...


class InspectCodeRunContext(
    code_action.RunActionWithPartialResultsContext[InspectCodeRunPayload]
): ...


class InspectCodeAction(
    code_action.Action[InspectCodeRunPayload, InspectCodeRunContext, InspectCodeRunResult]
):
    """Run all registered diagnostic tools and aggregate their results.

    Contract for ``target="files"``:
        Every path in ``file_paths`` MUST appear in the result.
        Paths that do not exist on disk MUST produce a ``WARNING`` diagnostic
        rather than a silent "OK".  The ``FileExistenceValidationHandler``
        enforces this guarantee; bridge handlers (lint, type_check, …) are
        not expected to validate path existence themselves.
    """

    DESCRIPTION = "Run all code diagnostic tools (linters, type checkers) and aggregate results."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = InspectCodeRunPayload
    RUN_CONTEXT_TYPE = InspectCodeRunContext
    RESULT_TYPE = InspectCodeRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
