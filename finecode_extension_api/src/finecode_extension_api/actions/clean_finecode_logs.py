import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class CleanFinecodeLogsRunPayload(code_action.RunActionPayload):
    ...


class CleanFinecodeLogsRunContext(code_action.RunActionContext[CleanFinecodeLogsRunPayload]):
    ...


@dataclasses.dataclass
class CleanFinecodeLogsRunResult(code_action.RunActionResult):
    errors: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CleanFinecodeLogsRunResult):
            return
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        return "\n".join(self.errors)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if len(self.errors) == 0:
            return code_action.RunReturnCode.SUCCESS
        else:
            return code_action.RunReturnCode.ERROR


class CleanFinecodeLogsAction(code_action.Action[CleanFinecodeLogsRunPayload, CleanFinecodeLogsRunContext, CleanFinecodeLogsRunResult]):
    PAYLOAD_TYPE = CleanFinecodeLogsRunPayload
    RUN_CONTEXT_TYPE = CleanFinecodeLogsRunContext
    RESULT_TYPE = CleanFinecodeLogsRunResult
