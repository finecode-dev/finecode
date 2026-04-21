# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class CleanServiceLogsRunPayload(code_action.RunActionPayload):
    service_id: str
    """Service whose logs should be deleted, as returned by list_observability_services."""


class CleanServiceLogsRunContext(
    code_action.RunActionContext[CleanServiceLogsRunPayload]
): ...


@dataclasses.dataclass
class CleanServiceLogsRunResult(code_action.RunActionResult):
    errors: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CleanServiceLogsRunResult):
            return
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        if self.errors:
            return "\n".join(self.errors)
        return "Logs cleaned."

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return (
            code_action.RunReturnCode.ERROR
            if self.errors
            else code_action.RunReturnCode.SUCCESS
        )


class CleanServiceLogsAction(
    code_action.Action[
        CleanServiceLogsRunPayload,
        CleanServiceLogsRunContext,
        CleanServiceLogsRunResult,
    ]
):
    """Delete all logs for a specific observability service."""

    PAYLOAD_TYPE = CleanServiceLogsRunPayload
    RUN_CONTEXT_TYPE = CleanServiceLogsRunContext
    RESULT_TYPE = CleanServiceLogsRunResult
