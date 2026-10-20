# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordDiagnosticsBaselineRunResult,
)

__all__ = [
    "MergeDiagnosticsBaselineAction",
    "MergeDiagnosticsBaselineRunContext",
    "MergeDiagnosticsBaselineRunPayload",
    "MergeDiagnosticsBaselineRunResult",
]


@dataclasses.dataclass
class MergeDiagnosticsBaselineRunPayload(code_action.RunActionPayload):
    results_files: list[ResourceUri] = dataclasses.field(default_factory=list)
    project_paths: list[ResourceUri] | None = None


@dataclasses.dataclass
class MergeDiagnosticsBaselineRunResult(RecordDiagnosticsBaselineRunResult): ...


class MergeDiagnosticsBaselineRunContext(
    code_action.RunActionWithPartialResultsContext[
        MergeDiagnosticsBaselineRunPayload
    ]
): ...


class MergeDiagnosticsBaselineAction(
    code_action.Action[
        MergeDiagnosticsBaselineRunPayload,
        MergeDiagnosticsBaselineRunContext,
        MergeDiagnosticsBaselineRunResult,
    ]
):
    """Merge other platforms' diagnostic counts from CI results files.

    Runs no analysis: it derives each leg's counts from the partition receipts
    inside ``--results-file`` files and replaces, per project, exactly the
    receipted partitions on the receipt's platform.
    """

    DESCRIPTION = "Merge diagnostics baselines from results files of other platforms."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = MergeDiagnosticsBaselineRunPayload
    RUN_CONTEXT_TYPE = MergeDiagnosticsBaselineRunContext
    RESULT_TYPE = MergeDiagnosticsBaselineRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
