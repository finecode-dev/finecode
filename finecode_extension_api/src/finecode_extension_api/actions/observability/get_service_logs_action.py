# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class GetServiceLogsRunPayload(code_action.RunActionPayload):
    service_id: str
    """Service to read logs from, as returned by list_observability_services."""
    tail_lines: int | None = 200
    """Most-recent lines to return. None = no limit."""
    offset_lines: int = 0
    """Most-recent lines to skip before applying tail_lines.
    Use to paginate backward: offset_lines=200 skips the last 200 lines and
    returns the 200 lines before them."""
    since_ts_iso: str | None = None
    """Omit lines timestamped before this ISO 8601 UTC value.
    Best-effort: requires parseable timestamps in log lines."""


class GetServiceLogsRunContext(
    code_action.RunActionContext[GetServiceLogsRunPayload]
): ...


@dataclasses.dataclass
class GetServiceLogsRunResult(code_action.RunActionResult):
    service_id: str = ""
    content: str = ""
    truncated: bool = False
    """True if there are more lines before the returned range (i.e. the beginning
    of the log was not reached). Increment offset_lines by tail_lines to paginate."""
    errors: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetServiceLogsRunResult):
            return
        if other.content:
            self.content = (self.content + "\n" + other.content).strip()
        self.truncated = self.truncated or other.truncated
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        return self.content or "\n".join(self.errors)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return (
            code_action.RunReturnCode.ERROR
            if self.errors
            else code_action.RunReturnCode.SUCCESS
        )


class GetServiceLogsAction(
    code_action.Action[
        GetServiceLogsRunPayload,
        GetServiceLogsRunContext,
        GetServiceLogsRunResult,
    ]
):
    """Read recent log output from a specific observability service."""

    PAYLOAD_TYPE = GetServiceLogsRunPayload
    RUN_CONTEXT_TYPE = GetServiceLogsRunContext
    RESULT_TYPE = GetServiceLogsRunResult
