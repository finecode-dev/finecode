# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api.resource_uri import ResourceUri


class AuditCodeTarget(enum.StrEnum):
    PROJECT = "project"
    FILES = "files"


@dataclasses.dataclass
class AuditCodeRunPayload(code_action.RunActionPayload):
    target: AuditCodeTarget = AuditCodeTarget.PROJECT
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    project_paths: list[ResourceUri] | None = None


@dataclasses.dataclass
class AuditCodeRunResult(DiagnosticFilesRunResult): ...


class AuditCodeRunContext(
    code_action.RunActionWithPartialResultsContext[AuditCodeRunPayload]
): ...


class AuditCodeAction(
    code_action.Action[AuditCodeRunPayload, AuditCodeRunContext, AuditCodeRunResult]
):
    """Run all registered on-demand code audit tools and aggregate their results.

    Peer of ``InspectCodeAction`` (see ADR-0044): same ``DiagnosticFilesRunResult``
    contract and the same ``target="files"`` existence-check guarantee, but
    distinguished by cadence. Handlers registered here may be whole-project and
    slow (e.g. architectural import-graph analysis), because this action is
    invoked at deliberate checkpoints — explicit CLI/MCP call, precommit, CI —
    never per-keystroke like ``inspect_code``.

    Contract for ``target="files"``:
        Every path in ``file_paths`` MUST appear in the result.
        Paths that do not exist on disk MUST produce a ``WARNING`` diagnostic
        rather than a silent "OK".  The ``FileExistenceValidationHandler``
        enforces this guarantee; bridge handlers (check_imports, …) are not
        expected to validate path existence themselves.
    """

    DESCRIPTION = "Run all thorough, on-demand code diagnostic tools (architecture, security, ...) and aggregate results."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = AuditCodeRunPayload
    RUN_CONTEXT_TYPE = AuditCodeRunContext
    RESULT_TYPE = AuditCodeRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
