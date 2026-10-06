# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class CreateGitTagRunPayload(code_action.RunActionPayload):
    tag: str
    message: str | None = None
    """None -> lightweight tag."""
    ref: str | None = None
    """None -> HEAD."""
    force: bool = False


class CreateGitTagRunContext(code_action.RunActionContext[CreateGitTagRunPayload]): ...


@dataclasses.dataclass
class CreateGitTagRunResult(code_action.RunActionResult):
    tag: str
    created: bool
    """False if the tag already existed (idempotent no-op) or git failed."""
    error: str | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CreateGitTagRunResult):
            return

        self.tag = other.tag
        self.created = other.created
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to create tag {self.tag}: {self.error}"
        if not self.created:
            return f"Tag {self.tag} already exists"
        return f"Created tag {self.tag}"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class CreateGitTagAction(
    code_action.Action[
        CreateGitTagRunPayload,
        CreateGitTagRunContext,
        CreateGitTagRunResult,
    ]
):
    """Create a git tag. Idempotent no-op if the
    tag already exists."""

    DESCRIPTION = "Create a git tag."
    PAYLOAD_TYPE = CreateGitTagRunPayload
    RUN_CONTEXT_TYPE = CreateGitTagRunContext
    RESULT_TYPE = CreateGitTagRunResult
