# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class PushGitRefsRunPayload(code_action.RunActionPayload):
    refs: list[str]
    remote: str = "origin"
    force: bool = False


class PushGitRefsRunContext(code_action.RunActionContext[PushGitRefsRunPayload]): ...


@dataclasses.dataclass
class PushGitRefsRunResult(code_action.RunActionResult):
    pushed_refs: list[str]
    error: str | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PushGitRefsRunResult):
            return

        self.pushed_refs = other.pushed_refs
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to push refs: {self.error}"
        return "Pushed refs: " + ", ".join(self.pushed_refs)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PushGitRefsAction(
    code_action.Action[
        PushGitRefsRunPayload,
        PushGitRefsRunContext,
        PushGitRefsRunResult,
    ]
):
    """Push git refs to a remote."""

    DESCRIPTION = "Push git refs to a remote."
    PAYLOAD_TYPE = PushGitRefsRunPayload
    RUN_CONTEXT_TYPE = PushGitRefsRunContext
    RESULT_TYPE = PushGitRefsRunResult
