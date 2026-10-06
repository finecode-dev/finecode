import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class InitFalkorDBRunPayload(code_action.RunActionPayload):
    pass


class InitFalkorDBRunContext(code_action.RunActionContext[InitFalkorDBRunPayload]): ...


@dataclasses.dataclass
class InitFalkorDBRunResult(code_action.RunActionResult):
    connected: bool = False

    def update(self, other: "InitFalkorDBRunResult") -> None:
        self.connected = self.connected or other.connected

    def to_text(self) -> str:
        return "FalkorDB connected." if self.connected else "FalkorDB not connected."

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return (
            code_action.RunReturnCode.SUCCESS
            if self.connected
            else code_action.RunReturnCode.ERROR
        )


class InitFalkorDBAction(
    code_action.Action[
        InitFalkorDBRunPayload,
        InitFalkorDBRunContext,
        InitFalkorDBRunResult,
    ]
):
    """Validate and establish the FalkorDB connection.

    Must be run before any handler that reads from or writes to FalkorDB.
    """

    DESCRIPTION = "Initialize the FalkorDB connection for dep-graph handlers."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = InitFalkorDBRunPayload
    RUN_CONTEXT_TYPE = InitFalkorDBRunContext
    RESULT_TYPE = InitFalkorDBRunResult
