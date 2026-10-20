# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

__all__ = [
    "RecordDiagnosticsBaselineAction",
    "RecordDiagnosticsBaselineRunContext",
    "RecordDiagnosticsBaselineRunPayload",
    "RecordDiagnosticsBaselineRunResult",
    "RecordStatus",
    "RecordedSnapshot",
    "Umbrella",
]


class RecordStatus(enum.StrEnum):
    WRITTEN = "written"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


class Umbrella(enum.StrEnum):
    INSPECT_CODE = "inspect_code"
    AUDIT_CODE = "audit_code"


@dataclasses.dataclass
class RecordDiagnosticsBaselineRunPayload(code_action.RunActionPayload):
    project_paths: list[ResourceUri] | None = None


@dataclasses.dataclass
class RecordedSnapshot:
    project: ResourceUri
    umbrella: Umbrella
    snapshot_file: ResourceUri
    platform: str
    status: RecordStatus
    partitions: int


@dataclasses.dataclass
class RecordDiagnosticsBaselineRunResult(code_action.RunActionResult):
    snapshots: list[RecordedSnapshot] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, RecordDiagnosticsBaselineRunResult):
            return
        self.snapshots.extend(other.snapshots)

    def to_text(self) -> str:
        if not self.snapshots:
            return "no snapshots recorded\n"
        lines: list[str] = []
        for project in sorted({str(item.project) for item in self.snapshots}):
            lines.append(f"{project}:")
            lines.extend(
                f"  {item.umbrella.value}: {item.status.value} "
                f"({item.partitions} partitions) [{item.snapshot_file}]"
                for item in self.snapshots
                if str(item.project) == project
            )
        return "\n".join(lines) + "\n"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class RecordDiagnosticsBaselineRunContext(
    code_action.RunActionWithPartialResultsContext[
        RecordDiagnosticsBaselineRunPayload
    ]
): ...


class RecordDiagnosticsBaselineAction(
    code_action.Action[
        RecordDiagnosticsBaselineRunPayload,
        RecordDiagnosticsBaselineRunContext,
        RecordDiagnosticsBaselineRunResult,
    ]
):
    """Record this platform's diagnostic counts into each project's snapshot.

    Runs the umbrellas and replaces, per project, exactly the partitions the
    run holds receipts for. Recording is deliberate: no check run ever writes.
    """

    DESCRIPTION = "Record this platform's diagnostics baseline snapshots."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = RecordDiagnosticsBaselineRunPayload
    RUN_CONTEXT_TYPE = RecordDiagnosticsBaselineRunContext
    RESULT_TYPE = RecordDiagnosticsBaselineRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
